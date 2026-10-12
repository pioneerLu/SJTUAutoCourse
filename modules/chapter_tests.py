"""智慧树章节作业：导出题目、视觉 LLM 作答、按选项 ID 回填。"""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
import hashlib
import re
from pathlib import Path
from time import monotonic
from urllib.parse import urlparse

from playwright.async_api import async_playwright, TimeoutError, Error
from playwright._impl._errors import TargetClosedError

from modules.storage import read_json, read_json_safely, write_json
from modules.chapter_verification import wait_for_chapter_verification, VERIFICATION_RESTART_LIMIT
from modules.chapter_records import question_signature, content_signature, reusable_history, review_record_state
from modules.answer_policy import overwrite_enabled, selected_ids, keep_existing_choice, answer_for_submission

from modules.browser import create_browser_session
from modules.llm import LLMError, LLMSettings, VisionLLM, Question, Answer, answer_from_record
from modules.login import is_login_page, wait_for_login_complete, complete_standard_login, LoginFailure, LoginTimeout
from modules.tasks import has_visible_verification, VERIFICATION_SELECTORS
from modules.utils import load_cookies, save_cookies, normalize_zhihuishu_cookies


PAUSE_SCRIPT = """
(() => {
    if (window.__autovisorMediaPaused) return;
    window.__autovisorMediaPaused = true;
    HTMLMediaElement.prototype.play = function() { this.pause(); return Promise.resolve(); };
    addEventListener('play', event => event.target.pause(), true);
    document.querySelectorAll('video,audio').forEach(element => element.pause());
})();
"""

# 当前作业页用 Vue 的 onChecked 标记选项；隐藏 input.checked 不随点击更新。
OPTION_SELECTED_JS = """function(e) {
    const markers = Array.from(e.querySelectorAll('.ABCase,.node_detail'));
    if (markers.length) return markers.some(marker => marker.classList.contains('onChecked'));
    if (e.hasAttribute('aria-checked')) return e.getAttribute('aria-checked') === 'true';
    return !!e.querySelector('input')?.checked;
}"""
OPTION_STATES_JS = "els => els.map(e => ({id:e.querySelector('input')?.value, text:e.innerText.trim(), selected:(" + OPTION_SELECTED_JS + ")(e)}))"
SELECTION_CONFIRMED_JS = """({id,wanted}) => {
    const root = Array.from(document.querySelectorAll('.examPaper_subject')).find(e =>
        e.getBoundingClientRect().width > 0 && getComputedStyle(e).display !== 'none' &&
        e.querySelector('.subject_num [id]')?.id === id);
    if (!root) return false;
    const selected = Array.from(root.querySelectorAll('.subject_node .label'))
        .filter(e => (""" + OPTION_SELECTED_JS + """ )(e)).map(e => e.querySelector('input')?.value);
    return selected.length === wanted.length && selected.every(value => wanted.includes(value));
}"""


def emit(callback, kind, **data):
    if callback:
        callback({'kind': kind, **data})


@asynccontextmanager
async def track_chapter_run(directory, mode, model):
    metadata = {'started_at': datetime.now().isoformat(timespec='microseconds'),
                'mode': mode, 'model': model, 'status': 'running'}
    write_json(Path(directory) / 'run.json', metadata)
    try:
        yield
    except asyncio.CancelledError:
        metadata['status'] = 'cancelled'
        raise
    except BaseException:
        metadata['status'] = 'failed'
        raise
    else:
        metadata['status'] = 'completed'
    finally:
        metadata['finished_at'] = datetime.now().isoformat(timespec='microseconds')
        write_json(Path(directory) / 'run.json', metadata)


async def chapter_test_names(page):
    return await page.locator('.test-box').evaluate_all("""els=>els.map((e,i)=>
        e.closest('.el-collapse-item')?.querySelector('.chapter-name')?.innerText.trim() || '章节测试 '+(i+1))""")


async def guard_request(route, read_only=False):
    url = route.request.url.lower()
    progress = ('savedatabaseintervaltime', 'savecacheintervaltime', 'savestustudyrecord',
                'savelearningtime', 'savestudytime')
    writes = ('/save', '/submit', '/temporarysave', 'startexam', 'beginexam',
              'startpaper', 'beginanswer', 'startanswer', 'createexam')
    # 章节作业不需要播放视频。导出模式额外阻止作答写入。
    if route.request.resource_type == 'media' or any(s in url for s in progress) or (read_only and not is_login_page(url) and any(s in url for s in writes)):
        await route.abort()
    else:
        await route.fallback()


class ChapterBrowserScope:
    """只保护任务根页面及其弹窗，不改变共用 Chrome 的其他标签页。"""
    def __init__(self, session, read_only):
        self.session = session
        self.context = session.context
        self.root = session.page
        self.read_only = read_only
        self.pages = set()
        self.existing_pages = set(self.context.pages)
        self.preparations = {}
        self.adoptions = []
        self.cdp_sessions = {}
        self.pending_popups = []
        self.closed = False
        self.handler = self._route
        self.popup_handler = self._register
        self.page_handler = self._on_new_page

    def _register(self, page):
        if self.closed or page in self.pages:
            return
        self.pages.add(page)
        page.on('popup', self.popup_handler)
        self.preparations[page] = asyncio.create_task(self._prepare(page))

    async def _prepare(self, page):
        if page.is_closed():
            return
        try:
            # 原页面同步 window.close() 时，公开 opener() 会返回 None。
            # 只监听任务页面自己的 windowOpen，仍可识别它创建的新标签页。
            cdp = await self.context.new_cdp_session(page)
            self.cdp_sessions[page] = cdp
            cdp.on('Page.windowOpen', self._note_popup)
            await cdp.send('Page.enable')
        except Error:
            pass
        try:
            await page.add_init_script(PAUSE_SCRIPT)
            await page.evaluate(PAUSE_SCRIPT)
        except Error:
            # 重定向时当前文档可能销毁；init script 会保护下一份文档。
            pass

    def _note_popup(self, event):
        if self.closed:
            return
        url = event.get('url', '')
        if urlparse(url).scheme not in ('http', 'https'):
            return
        self.pending_popups.append((url, monotonic()))
        for page in self.context.pages:
            if page not in self.existing_pages:
                self._on_new_page(page)

    def _consume_popup(self, page):
        now = monotonic()
        self.pending_popups = [(url, when) for url, when in self.pending_popups if now - when < 30]
        for index, (url, _) in enumerate(self.pending_popups):
            if page.url == url:
                self.pending_popups.pop(index)
                return True
        return False

    def _on_new_page(self, page):
        if not self.closed:
            self.adoptions.append(asyncio.create_task(self._adopt(page)))

    async def _adopt(self, page):
        if self.closed or page.is_closed():
            return
        if page in self.pages:
            self._consume_popup(page)
            return
        try:
            opener = await page.opener()
            expected = self._consume_popup(page)
            if opener in self.pages or expected:
                self._register(page)
        except Error:
            pass

    async def prepare_page(self, page):
        self._register(page)
        task = self.preparations.get(page)
        if task:
            await task

    async def _route(self, route):
        try:
            page = route.request.frame.page
            if page not in self.pages:
                opener = await page.opener()
                while opener is not None and opener not in self.pages:
                    opener = await opener.opener()
                if opener in self.pages:
                    self._register(page)
        except Error:
            # Service Worker 和弹窗首个导航可能没有可关联的 Frame。
            await route.fallback()
            return
        if page in self.pages:
            await guard_request(route, self.read_only)
        else:
            await route.fallback()

    async def start(self):
        self.session._chapter_scope = self
        self.context.on('page', self.page_handler)
        self._register(self.root)
        await self.context.route('**/*', self.handler)
        await self.prepare_page(self.root)

    async def close(self):
        self.closed = True
        if getattr(self.session, '_chapter_scope', None) is self:
            del self.session._chapter_scope
        self.context.remove_listener('page', self.page_handler)
        try:
            await self.context.unroute('**/*', self.handler)
        except Error:
            pass
        tasks = list(self.preparations.values()) + self.adoptions
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for cdp in self.cdp_sessions.values():
            try:
                await cdp.detach()
            except Error:
                pass
        for page in self.pages:
            page.remove_listener('popup', self.popup_handler)
            if page is not self.root and not page.is_closed():
                try:
                    await page.close()
                except Error:
                    pass


async def go_to_question(page, index):
    await page.locator('.answerCard_list1 li').nth(index).click(timeout=5000)
    await page.wait_for_function("""number => Array.from(document.querySelectorAll('.subject_num [id]'))
        .some(e => e.getBoundingClientRect().height > 0 && Number.parseInt(e.innerText.trim(), 10) === number)""",
        arg=index + 1, timeout=10000)
    await page.evaluate('document.fonts.ready')


async def read_question(page, index, directory) -> Question:
    root = page.locator('.examPaper_subject:visible').first
    kind = (await root.locator('.subject_type').inner_text(timeout=5000)).strip()
    if not any(s in kind for s in ('单选', '多选', '判断')):
        raise LLMError(f'第 {index + 1} 题题型暂不支持: {kind}')
    anchor = await root.locator('.subject_num [id]').get_attribute('id')
    options = await root.locator('.subject_node .label').evaluate_all(OPTION_STATES_JS)
    if not anchor or len(options) < 2 or any(not o['id'] or not o['text'] for o in options):
        raise LLMError(f'第 {index + 1} 题题目或选项结构不完整，已停止。')
    if len({o['id'] for o in options}) != len(options):
        raise LLMError('页面选项 ID 重复，已停止。')
    screenshot = Path(directory) / f'question-{index + 1}.png'
    # 整题截图包含 closed Shadow DOM 题干和可能的图片选项。
    await root.screenshot(path=str(screenshot))
    # 指纹排除选项选中样式与题号，避免暂存后截图变化导致整章重做。
    stem_image = screenshot.with_name(f'question-{index + 1}-stem.png')
    stem = root.locator('.subject_describe')
    content = {}
    try:
        bounds = await stem.bounding_box(timeout=3000)
        if bounds and bounds['height'] > 0 and bounds['width'] > 0:
            await stem.screenshot(path=str(stem_image), timeout=5000)
            option_content = []
            labels = root.locator('.subject_node .label')
            for position, option in enumerate(options):
                pictures = labels.nth(position).locator('img:visible, canvas:visible')
                image_hashes = [hashlib.sha256(await pictures.nth(i).screenshot(timeout=3000)).hexdigest()
                                for i in range(await pictures.count())]
                text = re.sub(r'^\s*[A-Z][.、．]\s*', '', option['text']).strip()
                option_content.append((option['id'], text, image_hashes))
            content = {'id': anchor, 'type': kind, 'stem_text': await stem.inner_text(),
                'stem_image': hashlib.sha256(stem_image.read_bytes()).hexdigest(), 'options': sorted(option_content)}
    except Error:
        pass  # 无法建立稳定指纹时，只复用整题截图完全一致的记录。
    write_json(screenshot.with_suffix('.content.json'), content)
    return Question(index + 1, anchor, kind,
        await root.locator('.subject_describe').inner_text(), str(screenshot), options)


async def extract_questions(page, directory, callback=None, *, navigation=None):
    navigation = navigation or callback
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    count = await page.locator('.answerCard_list1 li').count()
    if not count:
        raise LLMError('未找到可读取的题号。')
    questions = []
    index, restarts = 0, 0
    while index < count:
        resumed = await wait_for_chapter_verification(page, navigation)
        if not resumed:
            await go_to_question(page, index)
            resumed = await wait_for_chapter_verification(page, navigation)
        if not resumed:
            question = await read_question(page, index, directory)
            resumed = await wait_for_chapter_verification(page, navigation)
        if resumed:
            restarts += 1
            if restarts > VERIFICATION_RESTART_LIMIT:
                raise LLMError('安全验证反复出现，已停止读取题目；请完成验证后重试。')
            questions.clear()
            index = 0
            count = await page.locator('.answerCard_list1 li').count()
            if not count:
                raise LLMError('安全验证后未找到题号，请重新打开章节。')
            continue
        questions.append(question)
        emit(callback, 'question_read', question=questions[-1].to_dict(), total=count)
        index += 1
    write_json(directory / 'questions.json', [q.to_dict() for q in questions])
    return questions


async def fill_answer(page, question: Question, answer: Answer, *, allow_empty=False):
    await go_to_question(page, question.number - 1)
    root = page.locator('.examPaper_subject:visible').first
    current_id = await root.locator('.subject_num [id]').get_attribute('id')
    labels = root.locator('.subject_node .label')
    current_options = await labels.evaluate_all("""els => els.map(e => ({
        id:e.querySelector('input')?.value, text:e.innerText.trim()
    }))""")
    expected = {o['id']: o['text'] for o in question.options}
    current = {o['id']: o['text'] for o in current_options}
    if current_id != question.question_id or expected != current:
        raise LLMError('题目或选项在 LLM 请求期间发生变化，请重新读取后作答。')
    wanted = set(answer.option_ids)
    if not wanted <= set(current) or (not wanted and not allow_empty):
        raise LLMError('待回填的选项 ID 无效。')
    if wanted and '多选' not in question.type_and_score and len(wanted) != 1:
        raise LLMError('单选题或判断题不能回填多个答案。')
    for index, option in enumerate(current_options):
        selected = await labels.nth(index).evaluate(OPTION_SELECTED_JS)
        desired = option['id'] in wanted
        if (desired and not selected) or (selected and not desired and ('多选' in question.type_and_score or not wanted)):
            await labels.nth(index).click(timeout=5000)
    try:
        await page.wait_for_function(SELECTION_CONFIRMED_JS,
            arg={'id': question.question_id, 'wanted': list(wanted)}, timeout=5000)
    except TimeoutError:
        if not wanted:
            raise LLMError(f'第 {question.number} 题已有选择无法清空，请在网页手动清空后重试。') from None
        raise LLMError(f'第 {question.number} 题选项回填未确认成功，已停止。')


class SaveConfirmationError(LLMError):
    """保存结果无法确认；禁止自动再次写入。"""


def matches_status_code(value, accepted):
    """接口状态允许整数或对应字符串，拒绝布尔值和其他隐式转换。"""
    return type(value) in (int, str) and any(value == code or value == str(code) for code in accepted)


@asynccontextmanager
async def hold_exam_close(page):
    """暂缓网站主动关闭当前测试页，读完响应后恢复，最多保留 45 秒。"""
    try:
        await page.evaluate("""() => {
            if (window.__autovisorFinishClose) return;
            const original = window.close;
            const state = {requested:false};
            const deferred = () => {state.requested = true;};
            state.release = () => {
                clearTimeout(state.timer);
                if (window.close === deferred) window.close = original;
                delete window.__autovisorFinishClose;
                if (state.requested) original.call(window);
            };
            window.__autovisorFinishClose = state;
            window.close = deferred;
            state.timer = setTimeout(state.release, 45000);
        }""")
        yield
    finally:
        if not page.is_closed():
            try:
                requested = await page.evaluate("""() => {
                    const state = window.__autovisorFinishClose;
                    if (!state) return false;
                    const requested = state.requested;
                    state.release();
                    return requested;
                }""")
                if requested and not page.is_closed():
                    await page.wait_for_event('close', timeout=1000)
            except Error:
                pass  # 页面已关闭或跳转，新文档不保留该临时处理。


async def finish_exam(page, auto_submit=False, *, allow_unanswered=False):
    if await has_visible_verification(page):
        raise LLMError('页面出现安全验证，未执行保存或交卷。')
    suffix = '/answer/submit' if auto_submit else '/answer/temporarySave'
    button_name = '提交作业' if auto_submit else '暂存作业'
    capture = None
    try:
        async with hold_exam_close(page), page.expect_response(
            lambda r: urlparse(r.url).path.lower().endswith(suffix.lower()), timeout=30000
        ) as pending:
            async def read_confirmation():
                response = await pending.value
                page._autovisor_save_response = {'action': button_name, 'http_status': response.status}
                try:
                    return response, await response.json()
                except Error:
                    raise SaveConfirmationError('保存/交卷响应读取失败；程序不会自动重复发送。') from None
            # 网站暂存成功后会关闭弹窗页，必须立即读取响应，不能等确认框超时。
            capture = asyncio.create_task(read_confirmation())
            try:
                await page.get_by_role('button', name=button_name, exact=True).click(timeout=5000)
            except TargetClosedError:
                pass  # 只有下面读到成功响应，才能认定完成。
            # 网站自己的作答/暂存确认框，无额外的聊天确认流程。
            modal = page.locator('.el-message-box:visible')
            try:
                await modal.wait_for(state='visible', timeout=3000)
                text = await modal.inner_text()
                if ('未作答' in text or '未答' in text) and not allow_unanswered:
                    cancel = modal.get_by_role('button', name='取消', exact=True)
                    if await cancel.count():
                        await cancel.click()
                    raise LLMError('网站仍提示存在未作答题目，已停止交卷。')
                await modal.get_by_role('button', name='确定', exact=True).click(timeout=5000)
            except (TimeoutError, TargetClosedError):
                pass
            response, data = await asyncio.wait_for(capture, timeout=30)
    except (TimeoutError, TargetClosedError, asyncio.TimeoutError):
        raise SaveConfirmationError('未收到保存/交卷成功响应；程序不会自动重复发送。') from None
    except (ValueError, TypeError):
        raise SaveConfirmationError('保存/交卷返回了无法识别的响应。') from None
    finally:
        if capture is not None:
            if not capture.done():
                capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
    if not isinstance(data, dict):
        raise SaveConfirmationError('保存/交卷返回了无法识别的响应。')
    rt = data.get('rt')
    page._autovisor_save_response.update(api_status=data.get('status'),
        result_status=rt.get('statu') if isinstance(rt, dict) else None)
    accepted = (0, 1) if auto_submit else (1,)
    if (not response.ok or not matches_status_code(data.get('status'), (200,))
            or not isinstance(rt, dict) or not matches_status_code(rt.get('statu'), accepted)):
        raise LLMError('网站未确认保存/交卷成功，请查看浏览器页面。')
    page._autovisor_save_response['method'] = 'api_response'
    return 'submitted' if auto_submit else 'saved'


async def generate_answers(questions, directory, solver, *, preview=False, callback=None,
                           preserve_others=True, overwrite=False, history=None):
    """默认补空题及低置信度题；覆盖模式才重新请求已有有效答案。"""
    directory = Path(directory)
    path = directory / 'review.json'
    cached = read_json(path) if path.is_file() else []
    candidates = [record for record in cached + (history or []) if isinstance(record, dict)]
    results = []
    requests = 0
    for question in questions:
        signature = question_signature(question)
        stable = content_signature(question)
        previous = next((r for r in candidates if r.get('content_signature') == stable
                         or r.get('signature') == signature), None) if not overwrite else None
        if previous:
            previous = dict(previous, question=question.to_dict())
            previous.pop('preserved_existing', None)
            try:
                answer_from_record(previous, question, solver.settings.min_confidence, enforce_confidence=False)
            except LLMError:
                previous = None
        if keep_existing_choice(question, previous, solver.settings.min_confidence, overwrite):
            if previous is None or set(previous['answer']['option_ids']) != set(selected_ids(question)):
                previous = {'question': question.to_dict(), 'signature': signature, 'status': 'manual',
                    'model': '', 'answer': asdict(Answer(selected_ids(question), 1.0, '保留网页已有答案')),
                    'meets_confidence': True}
            previous['preserved_existing'] = True
        if previous and previous.get('meets_confidence'):
            previous.update(signature=signature, content_signature=stable)
            results.append(previous)
            emit(callback, 'answer_ready', result=previous, reused=True)
            if not preview:
                write_json(path, results + [r for r in cached if r.get('question', {}).get('question_id')
                    not in {x['question']['question_id'] for x in results}])
            continue
        if requests and solver.settings.request_interval:
            emit(callback, 'waiting', seconds=solver.settings.request_interval, message='等待请求间隔')
            await asyncio.sleep(solver.settings.request_interval)
        emit(callback, 'answer_started', number=question.number, total=len(questions), model=solver.settings.model)
        requests += 1
        record = {'question': question.to_dict(), 'signature': signature, 'content_signature': stable,
                  'model': solver.settings.model}
        try:
            answer = await solver.answer(question, enforce_confidence=False,
                on_retry=lambda info: emit(callback, 'retry', number=question.number, **info))
            record.update(status='answered', answer=asdict(answer),
                          meets_confidence=answer.confidence >= solver.settings.min_confidence)
            if isinstance(getattr(solver, 'last_trace', None), dict):
                record['relay'] = solver.last_trace
        except LLMError as exc:
            record.update(status='failed', error=str(exc))
            if getattr(exc, 'response_diagnostics', None):
                record['llm_diagnostics'] = exc.response_diagnostics
            if getattr(exc, 'relay_trace', None):
                record['relay'] = exc.relay_trace
        results.append(record)
        write_json(path, results + [r for r in cached if r.get('question', {}).get('question_id')
            not in {x['question']['question_id'] for x in results}])
        emit(callback, 'answer_ready', result=record, reused=False)
    if preserve_others:
        current_ids = {r['question']['question_id'] for r in results}
        write_json(path, results + [r for r in cached if r.get('question', {}).get('question_id') not in current_ids])
    else:
        write_json(path, results)
    return results


async def solve_exam(page, directory, solver, auto_submit=False, logger=None, *, preview=False, callback=None,
                     overwrite=False, save_recovery=None):
    navigation = callback or ((lambda event: logger.info(event['message'])) if logger else None)
    questions = await extract_questions(page, directory, callback, navigation=navigation)
    answers = []
    omissions = {}
    # 格式/题目校验仍先完成；低置信度只影响对应题目。
    try:
        results = await generate_answers(questions, directory, solver, preview=preview,
                                         callback=callback, preserve_others=False, overwrite=overwrite,
                                         history=reusable_history(directory) if not overwrite else None)
    except LLMError as exc:
        if preview:
            raise
        raise LLMError(f'模型答案未完成：{exc} 本次未回填或暂存；可重试失败题或切换模型。') from exc
    if preview:
        status = 'preview' if all(r['status'] in ('answered', 'manual') for r in results) else 'preview_partial'
        write_json(Path(directory) / 'result.json', {'status': status, 'question_count': len(questions)})
        return status
    for question, record in zip(questions, results):
        answer, omission = answer_for_submission(record, question, solver.settings.min_confidence)
        answers.append(answer)
        if omission:
            omissions[question.question_id] = omission
    write_json(Path(directory) / 'answers.json', [asdict(a) if a else None for a in answers])
    verified = await verify_questions(page, questions, Path(directory) / 'verification', callback=navigation)
    expected_choices = {q.question_id: set(selected_ids(q)) for q in verified}
    preserved = []
    for original, question, record, answer in zip(questions, verified, results, answers):
        await check_after_verification(page, questions, expected_choices,
            Path(directory) / 'verification', navigation)
        if not overwrite and selected_ids(question) and (
                record.get('preserved_existing') or not selected_ids(original)):
            preserved.append(question.number)
            emit(callback, 'answer_preserved', number=question.number)
            continue
        await fill_answer(page, question, answer or Answer([], 0, ''), allow_empty=answer is None)
        expected_choices[question.question_id] = set(answer.option_ids) if answer else set()
        emit(callback, 'answer_filled' if answer else 'answer_skipped', number=question.number,
             reason=omissions[question.question_id]['reason'] if answer is None else '')
    skipped = [item for item in omissions.values() if item['number'] not in preserved]
    if skipped and all(item['kind'] == 'invalid_answer' for item in skipped) and not any(answers) and not preserved:
        status = 'skipped_no_answers'
        emit(callback, 'test_navigation', message='本章节没有可用答案，已跳过保存或交卷，继续后续章节。')
    else:
        try:
            await check_after_verification(page, questions, expected_choices,
                Path(directory) / 'verification', navigation)
            status = await finish_exam(page, auto_submit, allow_unanswered=True)
        except SaveConfirmationError as error:
            if auto_submit or save_recovery is None:
                raise
            status, page = await save_recovery(questions, expected_choices, error)
    write_json(Path(directory) / 'result.json', {'status': status, 'question_count': len(questions),
        'skipped_low_confidence': [item['number'] for item in skipped if item['kind'] == 'low_confidence'],
        'skipped_questions': skipped, 'preserved_existing': preserved,
        'overwrite_answers': overwrite, 'filled_count': len(questions) - len(skipped) - len(preserved),
        'confirmation': getattr(page, '_autovisor_save_response', {})})
    return status


async def verify_questions(page, questions, directory, *, callback=None):
    """全部题目核对通过后，调用方才能开始回填。"""
    import re
    await wait_for_chapter_verification(page, callback)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    normalize = lambda text: re.sub(r'^\s*[A-Z][.、．]\s*', '', text).strip()
    verified = []
    index, restarts = 0, 0
    while index < len(questions):
        if await page.locator('.answerCard_list1 li').count() != len(questions):
            raise LLMError('题目数量发生变化，请重新读取题目。')
        question = questions[index]
        resumed = await wait_for_chapter_verification(page, callback)
        if not resumed:
            await go_to_question(page, question.number - 1)
            resumed = await wait_for_chapter_verification(page, callback)
        if not resumed:
            fresh = await read_question(page, question.number - 1, directory)
            resumed = await wait_for_chapter_verification(page, callback)
        if resumed:
            restarts += 1
            if restarts > VERIFICATION_RESTART_LIMIT:
                raise LLMError('安全验证反复出现，已停止当前章节；请确认验证完成后重试。')
            verified.clear()
            index = 0
            continue  # 验证可能引发页面刷新，之前已核对的题目也重新检查。
        old = {o['id']: normalize(o['text']) for o in question.options}
        new = {o['id']: normalize(o['text']) for o in fresh.options}
        if fresh.question_id != question.question_id or fresh.type_and_score != question.type_and_score or old != new:
            raise LLMError('题目或选项内容发生变化，请重新生成答案。')
        verified.append(fresh)
        index += 1
    return verified


async def check_after_verification(page, questions, expected_choices, directory, callback=None):
    """验证后重新核对整章及选中项，避免写入刷新或修改后的页面。"""
    if await wait_for_chapter_verification(page, callback):
        fresh = await verify_questions(page, questions, directory, callback=callback)
        if any(set(selected_ids(q)) != expected_choices[q.question_id] for q in fresh):
            raise LLMError('安全验证后网页答案发生变化，未执行保存或交卷，请重新回填。')


async def ensure_chapter_login(session, config, logger, cookie_path, callback=None,
                               *, cookie_check_timeout=10000, login_timeout=10 * 60 * 1000):
    """先确认常规账号登录，再打开章节入口，避免在测试网关完成首次登录。"""
    login_url = getattr(config, 'login_url', None)
    if not login_url:
        return
    notify = lambda message: (emit(callback, 'test_navigation', message=message)
                              if callback else logger.info(message))
    scope = getattr(session, '_chapter_scope', None)
    auth_root = session.page
    existing_pages = set(session.context.pages) if scope else set()
    candidates = (lambda: [p for p in scope.pages if p is auth_root or p not in existing_pages]) if scope else None
    cookies_reset = False
    async def reset_login_cookies():
        nonlocal cookies_reset
        if not cookies_reset and not getattr(session, 'attached', False):
            await session.context.clear_cookies(domain=re.compile(r'(^|\.)zhihuishu\.com$'))
            cookies_reset = True
            return True
        return False
    async def open_login():
        try:
            response = await session.page.goto(login_url, wait_until='domcontentloaded', timeout=30000)
        except Error:
            raise LoginFailure('无法打开常规登录页，请检查 Chrome 中的登录服务或网络状态。', session.page) from None
        if response and response.status >= 400:
            if response.status in (400, 401, 403) and await reset_login_cookies():
                notify(f'常规登录页返回 HTTP {response.status}，清理当前程序会话的失效登录状态后重试一次…')
                return await open_login()
            raise LoginFailure(f'常规登录页返回 HTTP {response.status}，已停止等待。', session.page)
    notify('正在确认视频学习网站的账号登录状态，章节测试依赖此登录…')
    await open_login()
    try:
        destination = await wait_for_login_complete(session.page, timeout=cookie_check_timeout,
                                                     page_candidates=candidates, notify=notify)
    except TimeoutError:
        if await reset_login_cookies():
            # 失效的已导入状态不能继续参与重新登录；保留其他站点的 Cookies。
            await open_login()
        notify('章节登录状态需要刷新，正在使用常规登录页…')
        captcha_modules = []
        if getattr(config, 'enableAutoCaptcha', False) is True:
            # 只在确实需要重新登录时检查可选依赖，复用现有 venv 中的包。
            from modules import installer
            notify('正在检查章节登录的自动滑块依赖…')
            try:
                captcha_modules = installer.start(config)
            except (RuntimeError, OSError):
                notify('自动滑块依赖未就绪，请运行 uv sync --extra gui --extra captcha；本次可在 Chrome 中手动验证。')
        destination = await complete_standard_login(session.page, getattr(config, 'username', ''),
                                      getattr(config, 'password', ''), notify=notify,
                                      timeout=login_timeout, page_candidates=candidates,
                                      captcha_modules=captcha_modules)
    if destination is not None and destination is not session.page:
        session.page = destination
        if scope:
            await scope.prepare_page(destination)
    save_cookies(normalize_zhihuishu_cookies(await session.context.cookies()), cookie_path)
    notify('视频学习网站账号登录已确认，正在返回课程页…')


async def restore_course_login(session, config, course_url, logger, cookie_path, callback=None):
    session._chapter_course = None
    await ensure_chapter_login(session, config, logger, cookie_path, callback)
    count = await load_course_entries(session, config, course_url, logger, cookie_path, callback, allow_login=False)
    session._chapter_course = {'url': course_url, 'page': session.page, 'location': session.page.url,
                              'names': await chapter_test_names(session.page)}
    return count


async def open_authenticated_course(session, config, course_url, logger, cookie_path, callback=None):
    """复用当前可用的课程目录，页面失效或换课时才重新登录及加载。"""
    cached = getattr(session, '_chapter_course', None)
    page = session.page
    if (isinstance(cached, dict) and cached['url'] == course_url and cached['page'] is page
            and not page.is_closed() and page.url == cached['location'] and not is_login_page(page.url)):
        try:
            if await has_visible_verification(page):
                raise LLMError('课程页出现安全验证，请在 Chrome 中完成验证后重试。')
            login_visible = await page.locator('#lUsername,input[name="mobile"]').first.is_visible()
            names = await chapter_test_names(page)
            if names and not login_visible:
                if names != cached['names']:
                    session._chapter_course = None
                    raise LLMError('当前章节列表发生变化，请重新读取章节再运行。')
                emit(callback, 'test_navigation', message='复用当前课程页和登录会话，正在打开下一章节。')
                return len(names)
        except Error:
            pass  # 页面跳转中或已销毁，进入原有的有界登录恢复流程。
    count = await restore_course_login(session, config, course_url, logger, cookie_path, callback)
    emit(callback, 'test_navigation', message='视频学习网站登录和课程目录已就绪，可以打开章节测试。')
    return count


async def load_course_entries(session, config, course_url, logger, cookie_path, callback=None,
                              *, timeout=30000, retries=1, allow_login=True, verification_timeout=120000):
    """等待异步课程目录；仅重试目录读取，不重放回填、暂存或交卷。"""
    page = session.page
    notify = lambda message: (emit(callback, 'test_navigation', message=message)
                              if callback else logger.info(message))
    pending, errors = set(), []
    monitored_types = ('document', 'script', 'xhr', 'fetch')
    state = {}
    login_refreshed = False
    account_status = None
    verification_seen = False

    def location(url):
        parsed = urlparse(url)
        path = '/<login>' if is_login_page(url) else parsed.path
        return f'{parsed.scheme}://{parsed.hostname or ""}{path}'

    def requested(request):
        if request.resource_type in monitored_types:
            pending.add(request)

    def failed(request):
        pending.discard(request)
        if request.resource_type not in monitored_types:
            return
        errors.append({'location': location(request.url), 'failure': request.failure,
                       'type': request.resource_type})
        del errors[:-20]

    def responded(response):
        nonlocal account_status
        if response.status in (401, 403) and urlparse(response.url).hostname in ('appcomm-user.zhihuishu.com', 'studyservice-api.zhihuishu.com'):
            account_status = response.status
        if response.status >= 400 and response.request.resource_type in monitored_types:
            errors.append({'location': location(response.url), 'http': response.status})
            del errors[:-20]

    def finished(request):
        pending.discard(request)

    listeners = {'request': requested, 'requestfailed': failed,
                 'requestfinished': finished, 'response': responded}
    for event, handler in listeners.items():
        page.on(event, handler)
    try:
        for attempt in range(retries + 1):
            if page.is_closed():
                break
            pending.clear()
            state = {}
            verification_seen = False
            account_status = None
            notify('正在加载课程目录…' if not attempt else f'课程目录未加载完成，正在重新打开课程（重试 {attempt} 次）…')
            deadline = monotonic() + timeout / 1000
            try:
                # commit 不等待图片、字体或异步接口；随后观察目录和登录状态。
                await page.goto(course_url, wait_until='commit', timeout=min(timeout, 10000))
            except Error:
                pass  # 跳转失败也进入有界观察，最终输出去敏诊断。
            while monotonic() < deadline and not page.is_closed():
                if is_login_page(page.url) or account_status:
                    if allow_login and not login_refreshed and getattr(config, 'login_url', None):
                        login_refreshed = True
                        notify('课程页要求重新登录，正在刷新账号状态…')
                        await ensure_chapter_login(session, config, logger, cookie_path, callback)
                        return await load_course_entries(session, config, course_url, logger, cookie_path, callback,
                            timeout=timeout, retries=retries, allow_login=False, verification_timeout=verification_timeout)
                    break
                try:
                    state = await asyncio.wait_for(page.evaluate('''selectors => {
                        const visible = e => {const r=e.getBoundingClientRect(),s=getComputedStyle(e);
                            return r.width>0 && r.height>0 && s.display!=='none' &&
                                s.visibility!=='hidden' && Number(s.opacity || 1)>0.05;};
                        return {title:document.title,
                            test_count:document.querySelectorAll('.test-box').length,
                            chapter_count:document.querySelectorAll('.el-collapse-item__header').length,
                            login_form:Array.from(document.querySelectorAll('#lUsername,input[name="mobile"]')).some(visible) &&
                                Array.from(document.querySelectorAll('input[type="password"]')).some(visible),
                            verification:selectors.some(s=>Array.from(document.querySelectorAll(s)).some(visible))};
                    }''', VERIFICATION_SELECTORS), timeout=2)
                except (Error, asyncio.TimeoutError):
                    await asyncio.sleep(0.25)
                    continue
                if state['verification']:
                    if not verification_seen:
                        verification_seen = True
                        notify(f'课程页出现验证码，请在 Chrome 中完成验证；程序会继续等待，最多 {verification_timeout / 1000:g} 秒。')
                        deadline = monotonic() + verification_timeout / 1000
                elif state['login_form']:
                    if allow_login and not login_refreshed and getattr(config, 'login_url', None):
                        login_refreshed = True
                        notify('视频学习页面仍要求登录，先完成网站登录再打开测试…')
                        await ensure_chapter_login(session, config, logger, cookie_path, callback)
                        return await load_course_entries(session, config, course_url, logger, cookie_path, callback,
                            timeout=timeout, retries=retries, allow_login=False, verification_timeout=verification_timeout)
                    raise LoginFailure('视频学习网站仍显示登录表单，尚不能打开章节测试。请先完成网站登录。', page)
                elif state['test_count']:
                    return state['test_count']
                await asyncio.sleep(0.25)
            if page.is_closed() or is_login_page(page.url) or state.get('verification') or account_status:
                break  # 不通过刷新打断用户登录或验证码。

        catalog_pending = any(urlparse(r.url).hostname == 'studyservice-api.zhihuishu.com'
                              and any(name in urlparse(r.url).path for name in ('videolist', 'queryCourseDispMode'))
                              for r in pending)
        account_pending = any(urlparse(r.url).hostname == 'appcomm-user.zhihuishu.com'
                              and any(name in urlparse(r.url).path for name in ('getUserInfoAndAuthInfo', '/c/has'))
                              for r in pending)
        if page.is_closed():
            reason = '课程窗口已关闭，请重新运行。'
        elif is_login_page(page.url):
            reason = '课程页登录尚未完成，请检查 Chrome 中的登录页面。'
        elif account_status:
            reason = f'视频学习网站账号或课程接口返回 HTTP {account_status}，当前登录或访问状态尚未就绪。'
        elif state.get('verification'):
            reason = '课程页验证码尚未完成，请完成验证后重试。'
        elif account_pending:
            reason = '账号状态接口未返回，课程目录尚未加载，请检查网络后重试。'
        elif catalog_pending:
            reason = '课程目录接口未返回，暂时无法读取章节测试入口，请检查网络后重试。'
        elif state.get('chapter_count'):
            reason = '课程目录中没有章节测试入口，可能尚未开放或页面结构已变化。'
        else:
            reason = '课程目录没有加载完成，请检查 Chrome 中的网页及网络后重试。'
        directory = Path(getattr(config, 'config_path', 'config')).resolve().parent / '.autovisor' / 'course-entry-errors' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        directory.mkdir(parents=True, exist_ok=True)
        write_json(directory / 'diagnostic.json', {'stage': '加载课程目录', 'reason': reason,
            'attempts': attempt + 1, 'location': location(page.url), 'page': state,
            'pending': sorted(({'location': location(r.url), 'type': r.resource_type} for r in pending),
                              key=lambda r: r['location'])[:20], 'network_errors': errors})
        if not page.is_closed():
            try:
                await page.screenshot(path=str(directory / 'error.png'), full_page=True, timeout=5000)
            except Error:
                pass
        raise LLMError(f'课程目录加载失败：{reason} 诊断记录：{directory}') from None
    finally:
        for event, handler in listeners.items():
            page.remove_listener(event, handler)


def chapter_page_status(url, title=''):
    """作答页和已提交的结果页必须分别处理，不能通过查询参数猜测路由。"""
    parsed = urlparse(url)
    if parsed.hostname != 'onlineexamh5new.zhihuishu.com':
        return 'unknown'
    route = parsed.fragment.split('?', 1)[0].strip('/').split('/')
    if len(route) >= 2 and route[0].lower() == 'webexamlist':
        if route[1].lower() == 'checkhomework':
            return 'already_submitted'
        if route[1].lower() == 'dohomework':
            return 'answer'
    if title.startswith('作业结果'):
        return 'already_submitted'
    return 'unknown'


async def is_submitted_test_page(page):
    if page.is_closed():
        return False
    status = chapter_page_status(page.url)
    if status == 'already_submitted':
        return True
    if status == 'answer' or urlparse(page.url).hostname != 'onlineexamh5new.zhihuishu.com':
        return False
    try:
        return chapter_page_status(page.url, await page.title()) == 'already_submitted'
    except Error:
        return False


async def test_page_state(page):
    if page.is_closed():
        return {'closed': True, 'location': '测试窗口已关闭'}
    parsed = urlparse(page.url)
    # 诊断地址不记录 CAS ticket 或其他 URL 查询参数。
    path = '/<login>' if is_login_page(page.url) else parsed.path
    location = f'{parsed.scheme}://{parsed.hostname or ""}{path}' if parsed.hostname else page.url.split('?')[0]
    base = {'closed': False, 'location': location, 'login': is_login_page(page.url),
            'navigation_error': getattr(page, '_chapter_navigation_error', '')}
    try:
        title, count, submit = await asyncio.wait_for(asyncio.gather(page.title(),
            page.locator('.answerCard_list1 li').count(),
            page.get_by_role('button', name='提交作业', exact=True).is_visible()), timeout=3)
    except (Error, asyncio.TimeoutError):
        return {**base, 'title': '', 'page_status': chapter_page_status(page.url),
                'question_count': 0, 'has_submit': False, 'dom_unavailable': True}
    return {'closed': False, 'location': location,
            'route': '/'.join(parsed.fragment.split('?', 1)[0].split('/')[:3]),
            'title': title, 'page_status': chapter_page_status(page.url, title),
            'question_count': count,
            'has_submit': submit,
            'login': is_login_page(page.url),
            'navigation_error': getattr(page, '_chapter_navigation_error', '')}


def chapter_error_text(error, config=None):
    text = re.sub(r'https?://[^\s\"\x27<>]+', '[URL]', str(error))
    for name in ('username', 'password'):
        secret = getattr(config, name, '') if config is not None else ''
        if isinstance(secret, str) and secret:
            text = text.replace(secret, '[REDACTED]')
    return text


async def capture_chapter_failure(directory, session, scope, error, stage, exam_page=None, config=None):
    pages = []
    for candidate in (getattr(error, 'page', None), getattr(error, 'chapter_page', None),
                      exam_page, session.page, *scope.pages):
        if candidate is not None and all(candidate is not p for p in pages):
            pages.append(candidate)
    pages.sort(key=lambda p: p.is_closed())
    snapshots = []
    for index, page in enumerate(pages):
        try:
            state = await test_page_state(page)
            snapshots.append(state)
            if index == 0:
                write_json(Path(directory) / 'page-info.json', state)
            if not page.is_closed():
                await page.screenshot(path=str(Path(directory) / ('error.png' if index == 0 else f'error-page-{index + 1}.png')),
                                      full_page=True, timeout=5000)
        except Exception:
            pass
    write_json(Path(directory) / 'diagnostic.json', {'stage': stage, 'error': chapter_error_text(error, config),
        'pages': snapshots, 'save_response': getattr(error, 'save_response',
            getattr(exam_page, '_autovisor_save_response', {}))})


async def record_submitted_test(page, directory):
    state = await test_page_state(page)
    write_json(Path(directory) / 'page-info.json', state)
    write_json(Path(directory) / 'result.json', {'status': 'already_submitted', 'page': state})
    return 'already_submitted'


async def test_page_problem(page):
    state = await test_page_state(page)
    if state['closed']:
        return '章节测试窗口已关闭，请重新运行。'
    if state['page_status'] == 'already_submitted':
        return '该章节作业已提交，当前为结果/解析页，无法再次回填。'
    if page.url in ('about:blank', ''):
        return '章节测试窗口仍为空白，跳转未完成或被阻止，请重试。'
    if state['login']:
        return '章节测试页要求重新登录，请在 Chrome 中完成登录后重试。' + (
            f' 登录跳转错误：{state["navigation_error"]}。' if state['navigation_error'] else '')
    if state['question_count'] and not state['has_submit']:
        reason = '测试页已显示题目，但没有可用的“提交作业”按钮，无法确认可作答状态。'
    else:
        reason = '测试页未完整加载到作答界面，请检查网页状态后重试。'
    error = f' 登录跳转错误：{state["navigation_error"]}。' if state['navigation_error'] else ''
    return f'{reason} 当前页面：{state["location"]}；题号数：{state["question_count"]}。{error}'


async def open_chapter_test(course_page, index, callback=None, *, scope=None, authenticate=None, sso_grace=15):
    context = course_page.context
    existing_pages = set(context.pages)
    entry = course_page.locator('.test-box').nth(index)
    if not await entry.is_visible():
        chapter = entry.locator("xpath=ancestor::div[contains(concat(' ',normalize-space(@class),' '),' el-collapse-item ')][1]")
        await chapter.locator('.el-collapse-item__header').click(timeout=5000)
    async with course_page.expect_popup(timeout=15000) as opened:
        await entry.click(timeout=5000)
    page = await opened.value
    if scope:
        await scope.prepare_page(page)
    emit(callback, 'test_navigation', message='正在等待章节测试页完成跳转…')
    # SSO 会经过数个域名，其中一次导航可能返回 HTTP 错误。等待 URL 的导航承诺
    # 会直接抛错并关闭浏览器；观察当前页面可让用户在 Chrome 中重试登录。
    is_exam = lambda url: chapter_page_status(str(url)) == 'answer'
    is_test_target = lambda url: chapter_page_status(str(url)) in ('answer', 'already_submitted')
    login_hosts = {'login.zhihuishu.com', 'passport.zhihuishu.com', 'cloudapi.polymas.com'}
    login_seen = False
    login_since = None
    authenticating = False
    waiting_for_controls = False
    video_return_seen = False
    deadline = monotonic() + 45

    def note_navigation_error(url, detail):
        nonlocal login_seen, deadline
        parsed = urlparse(url)
        if parsed.hostname in login_hosts or (parsed.hostname == 'studentexam-api.zhihuishu.com' and '/gologin/' in parsed.path):
            path = '/<login>'
            page._chapter_navigation_error = f'{parsed.hostname}{path} {detail}'
            if not login_seen:
                deadline = monotonic() + 10 * 60
                login_seen = True
            emit(callback, 'test_navigation', message='登录服务返回错误；请在 Chrome 窗口重试登录，程序会继续等待。')

    def on_response(response):
        try:
            main = response.request.frame == response.request.frame.page.main_frame
        except Error:
            main = False
        if response.status >= 400 and response.request.is_navigation_request() and main:
            if is_login_page(response.url):
                page._chapter_navigation_http_status = response.status
            note_navigation_error(response.url, f'HTTP {response.status}')

    def on_request_failed(request):
        if request.is_navigation_request():
            note_navigation_error(request.url, request.failure or '请求失败')

    watched_pages = []
    def watch_page(candidate):
        candidate.set_default_timeout(15000)
        candidate.on('response', on_response)
        candidate.on('requestfailed', on_request_failed)
        watched_pages.append(candidate)

    watch_page(page)
    closed_at = None
    try:
        while monotonic() < deadline:
            # 部分 SSO 登录会新开作答标签页并关闭原登录页。
            candidates = [p for p in context.pages if p not in existing_pages and p not in watched_pages and not p.is_closed()
                          and (scope is None or p in scope.pages)]
            exam_page = next((p for p in reversed(candidates) if is_test_target(p.url)), None)
            replacement = exam_page or (candidates[-1] if page.is_closed() and candidates else None)
            if replacement is not None:
                page = replacement
                if page not in watched_pages:
                    watch_page(page)
                emit(callback, 'test_navigation', message='登录页已跳转到新标签页，正在读取章节测试…')
            if page.is_closed():
                if closed_at is None:
                    closed_at = monotonic()
                if monotonic() - closed_at >= 5:
                    return page, False
                await asyncio.sleep(0.25)
                continue
            closed_at = None
            current = page.url
            if await is_submitted_test_page(page):
                emit(callback, 'test_navigation', message='该章节作业已提交，当前为结果页，跳过作答控件等待。')
                return page, False
            returned_to_video = urlparse(current).hostname in ('wisdom-mooc.zhihuishu.com', 'onlineweb.zhihuishu.com')
            if returned_to_video and not video_return_seen:
                video_return_seen = True
                emit(callback, 'test_navigation', message='测试入口返回了视频学习网站，需要先确认视频平台登录，再重新打开测试。')
            in_login = is_login_page(current) or returned_to_video or (login_seen and current.startswith('chrome-error://'))
            if in_login and login_since is None:
                login_since = monotonic()
            elif not in_login:
                login_since = None
            persistent_login = in_login and monotonic() - login_since >= sso_grace
            if authenticate and persistent_login:
                emit(callback, 'test_navigation', message='测试入口要求重新认证，正在通过常规账号登录后重新打开章节…')
                authenticating = True
                new_course_page = await authenticate()
                authenticating = False
                if page is not new_course_page and not page.is_closed():
                    await page.close()
                # 最多自动刷新一次，不反复重走失败的测试网关登录回跳。
                return await open_chapter_test(new_course_page or course_page, index, callback, scope=scope, sso_grace=sso_grace)
            if persistent_login:
                if returned_to_video:
                    raise LoginFailure('测试入口仍返回视频学习网站，视频平台登录或测试跳转尚未完成。请先确认视频平台登录。', page)
                status = getattr(page, '_chapter_navigation_http_status', None)
                if status:
                    raise LoginFailure(f'章节登录网关返回 HTTP {status}，已停止等待。请检查登录服务或重新打开课程入口。', page)
                from modules.login import login_page_error
                problem = await login_page_error(page)
                if problem:
                    raise LoginFailure(problem, page)
            if is_login_page(current):
                if not login_seen:
                    emit(callback, 'test_navigation', message='正在等待章节登录跳转；如停留在登录或验证码页，请在 Chrome 中手动登录，程序会继续等待。')
                    deadline = monotonic() + 10 * 60
                    login_seen = True
            elif is_exam(current):
                try:
                    ready = await page.evaluate("""() =>
                        !!document.querySelector('.answerCard_list1 li') &&
                        Array.from(document.querySelectorAll('button')).some(e => {
                            const r=e.getBoundingClientRect();
                            return e.innerText.trim()==='提交作业' && r.width>0 && r.height>0 &&
                                getComputedStyle(e).visibility!=='hidden';
                        })""")
                except Error:
                    ready = False  # 文档正在重定向，下一轮再读取。
                if ready:
                    await asyncio.sleep(0.5)  # 给页面的异步登录检查留出重定向时间。
                    if is_exam(page.url):
                        return page, True
                elif not waiting_for_controls:
                    emit(callback, 'test_navigation', message='正在等待题号及作答控件加载…')
                    waiting_for_controls = True
            await asyncio.sleep(0.25)
        return page, False
    except Exception as error:
        error.chapter_page = page
        if not getattr(error, 'chapter_stage', None):
            error.chapter_stage = '章节登录' if authenticating or is_login_page(page.url) else '打开章节测试'
        raise
    finally:
        for watched in watched_pages:
            watched.remove_listener('response', on_response)
            watched.remove_listener('requestfailed', on_request_failed)


async def recheck_saved_exam(session, scope, config, logger, cookie_path, directory, questions,
                            expected_choices, original_error, *, page=None, callback=None):
    """丢失暂存确认时只重新读取已保存选项；绝不再次回填、暂存或交卷。"""
    directory = Path(directory)
    context = read_json(directory / 'context.json')
    observed = getattr(page, '_autovisor_save_response', {})
    original_error.chapter_stage = '核对暂存结果'
    original_error.save_response = observed
    emit(callback, 'test_navigation', message='暂存确认未读到，正在重新读取保存结果；不会重复暂存。')
    if page and not page.is_closed():
        await page.close()
    previous_read_only = scope.read_only
    scope.read_only = True
    try:
        await open_authenticated_course(session, config, context['url'], logger, cookie_path, callback)
        async def authenticate():
            await restore_course_login(session, config, context['url'], logger, cookie_path, callback)
            return session.page
        navigation = callback or (lambda event: logger.info(event['message']))
        page, supported = await open_chapter_test(session.page, context['index'], navigation, scope=scope,
            authenticate=authenticate if getattr(config, 'login_url', None) else None)
        if not supported:
            if await is_submitted_test_page(page):
                return await record_submitted_test(page, directory), page
            raise original_error
        saved = await verify_questions(page, questions, directory / 'saved-verification', callback=navigation)
        if any(set(selected_ids(q)) != expected_choices[q.question_id] for q in saved):
            raise original_error
        page._autovisor_save_response = {'method': 'reloaded_saved_choices',
            'question_count': len(saved), 'observed_response': observed}
        return 'saved', page
    except Exception as error:
        error.chapter_page = page
        if not getattr(error, 'chapter_stage', None):
            error.chapter_stage = '核对暂存结果'
        raise
    finally:
        scope.read_only = previous_read_only


async def run_chapter_tests(config, logger, cookie_path, *, export_only=False, test_index=0,
                            mode=None, selected_tests=None, callback=None, model_profile=None):
    if test_index < 0:
        raise LLMError('章节测试序号不能小于 0，0 表示全部。')
    settings = LLMSettings.from_config(config, model_profile)
    mode = mode or ('export' if export_only else ('submit' if settings.auto_submit else 'save'))
    if mode not in ('export', 'preview', 'save', 'submit'):
        raise LLMError('未知章节测试运行模式。')
    export_only = mode == 'export'
    overwrite = overwrite_enabled(config)
    solver = None if export_only else VisionLLM(settings)
    root = Path(config.config_path).resolve().parent / '.autovisor' / 'chapter-tests' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    root.mkdir(parents=True, exist_ok=True)
    report = []
    async with track_chapter_run(root, mode, settings.model if not export_only else ''), async_playwright() as pw:
        emit(callback, 'artifact', path=str(root))
        session = await create_browser_session(pw, config, load_cookies(cookie_path), logger)
        scope = ChapterBrowserScope(session, mode in ('export', 'preview'))
        try:
            session.page.set_default_timeout(15000)
            await scope.start()
            for course_number, course_url in enumerate(config.course_urls, 1):
                try:
                    count = await open_authenticated_course(session, config, course_url, logger, cookie_path, callback)
                except Exception as error:
                    failed = root / f'course-{course_number}' / 'course-entry'
                    failed.mkdir(parents=True, exist_ok=True)
                    stage = getattr(error, 'chapter_stage', '确认视频学习网站登录及课程目录')
                    await capture_chapter_failure(failed, session, scope, error, stage, config=config)
                    raise LLMError(f'课程 {course_number} 视频平台登录或目录加载失败：{chapter_error_text(error, config)} 诊断记录：{failed}') from error
                names = await chapter_test_names(session.page)
                async def authenticate():
                    await restore_course_login(session, config, course_url, logger, cookie_path, callback)
                    return session.page
                if test_index > count:
                    raise LLMError(f'该课程只有 {count} 个章节测试。')
                indexes = selected_tests.get(course_number, []) if selected_tests is not None else ([test_index - 1] if test_index else range(count))
                if any(i < 0 or i >= count for i in indexes):
                    raise LLMError('所选章节序号已变化，请重新读取章节列表。')
                for ordinal, index in enumerate(indexes):
                    directory = root / f'course-{course_number}' / f'test-{index + 1}'
                    directory.mkdir(parents=True, exist_ok=True)
                    name = names[index]
                    write_json(directory / 'context.json', {'url': course_url, 'index': index, 'course': course_number,
                                                           'chapter_name': name, 'overwrite_answers': overwrite})
                    exam_page = None
                    stage = '打开章节测试'
                    emit(callback, 'test_started', course=course_number, test=index + 1, directory=str(directory), name=name)
                    try:
                        if ordinal:
                            stage = '确认视频学习网站登录'
                            refreshed = await open_authenticated_course(session, config, course_url, logger, cookie_path, callback)
                            if refreshed != count or await chapter_test_names(session.page) != names:
                                raise LLMError('重新登录后章节列表发生变化，请重新读取章节再运行。')
                        stage = '打开章节测试'
                        navigation = callback or (lambda event: logger.info(event['message']))
                        exam_page, supported = await open_chapter_test(session.page, index, navigation, scope=scope,
                            authenticate=authenticate if getattr(config, 'login_url', None) else None)
                        completed = not supported and await is_submitted_test_page(exam_page)
                        if not supported and not completed:
                            raise LLMError(await test_page_problem(exam_page))
                        if completed:
                            status = await record_submitted_test(exam_page, directory)
                            logger.info(f'章节 {index + 1}: 已提交，跳过结果页并继续下一章节。')
                        else:
                            save_cookies(normalize_zhihuishu_cookies(await session.context.cookies()), cookie_path)
                            stage = '读取题目' if export_only else '读取题目或作答'
                            if export_only:
                                questions = await extract_questions(exam_page, directory, callback, navigation=navigation)
                                status = 'exported'
                                logger.info(f'章节 {index + 1}: 已导出 {len(questions)} 题。')
                            else:
                                async def recover_save(questions, expected, error):
                                    nonlocal exam_page
                                    try:
                                        result, exam_page = await recheck_saved_exam(session, scope, config,
                                            logger, cookie_path, directory, questions, expected, error,
                                            page=exam_page, callback=callback)
                                        return result, exam_page
                                    except Exception as failed:
                                        exam_page = getattr(failed, 'chapter_page', exam_page)
                                        raise
                                status = await solve_exam(exam_page, directory, solver, mode == 'submit', logger,
                                    preview=mode == 'preview', callback=callback, overwrite=overwrite,
                                    save_recovery=recover_save)
                                labels = {'submitted': '已交卷', 'saved': '已暂存，尚未交卷',
                                          'preview': '答案预览已生成', 'preview_partial': '部分答案失败，可重试',
                                          'skipped_no_answers': '无可用答案，已跳过',
                                          'already_submitted': '已提交，跳过保存'}
                                logger.info(f'章节 {index + 1}: {labels[status]}。')
                        report.append({'course': course_number, 'test': index + 1, 'status': status})
                        emit(callback, 'test_finished', course=course_number, test=index + 1, status=status, directory=str(directory))
                    except Exception as exc:
                        stage = getattr(exc, 'chapter_stage', stage)
                        detail = chapter_error_text(exc, config)
                        if isinstance(exc, (LoginFailure, LoginTimeout)) or stage == '章节登录':
                            message = f'章节 {index + 1} 登录失败：{detail} 诊断记录：{directory}'
                        elif isinstance(exc, TimeoutError):
                            message = f'章节 {index + 1} {stage}超时：{detail} 诊断记录：{directory}'
                        else:
                            message = detail
                        report.append({'course': course_number, 'test': index + 1, 'status': 'failed',
                                       'stage': stage, 'error': message})
                        emit(callback, 'test_finished', course=course_number, test=index + 1, status='failed', directory=str(directory), error=message)
                        write_json(root / 'report.json', report)
                        await capture_chapter_failure(directory, session, scope, exc, stage, exam_page, config)
                        logger.error(f'本章节失败，继续处理后续章节：{message}')
                    finally:
                        if exam_page and not exam_page.is_closed():
                            await exam_page.close()
                    write_json(root / 'report.json', report)
        finally:
            try:
                await scope.close()
            finally:
                await session.close()
        failures = [entry for entry in report if entry['status'] == 'failed']
        if failures:
            raise LLMError(f'全部所选章节处理结束，其中 {len(failures)} 个章节失败；其余章节已继续处理。'
                           f'首个失败：{failures[0]["error"]}；完整记录：{root / "report.json"}')
    logger.info(f'章节测试记录: {root}')
    return 0


async def list_chapter_tests(config, logger, cookie_path, callback=None):
    chapters = []
    async with async_playwright() as pw:
        session = await create_browser_session(pw, config, load_cookies(cookie_path), logger)
        scope = ChapterBrowserScope(session, True)
        try:
            session.page.set_default_timeout(15000)
            await scope.start()
            for course, url in enumerate(config.course_urls, 1):
                await open_authenticated_course(session, config, url, logger, cookie_path, callback)
                names = await chapter_test_names(session.page)
                for index, name in enumerate(names):
                    entry = {'course': course, 'index': index, 'name': name, 'url': url}
                    chapters.append(entry)
                    emit(callback, 'chapter', chapter=entry)
        finally:
            try:
                await scope.close()
            finally:
                await session.close()
    return chapters


class ChapterReviewBrowser:
    """批量回填拥有一个浏览器；本地无效/已提交记录不触发启动。"""
    def __init__(self, config, logger, cookie_path):
        self.config, self.logger, self.cookie_path = config, logger, cookie_path
        self.manager = self.playwright = self.session = self.scope = None

    async def __aenter__(self):
        return self

    async def acquire(self):
        if self.session is not None:
            browser = getattr(self.session, 'browser', None)
            if browser is not None and not browser.is_connected():
                self.logger.info('Chrome 连接已断开，为下一章节重新建立会话；不会重发上一章节。')
                await self.close()
            elif not self.session.page.is_closed():
                return self.session, self.scope
            else:
                # 课程标签被关闭时只重建任务标签，保留浏览器及登录上下文。
                try:
                    await self.scope.close()
                    self.session._chapter_course = None
                    self.session.page = await self.session.context.new_page()
                    self.session.page.set_default_timeout(15000)
                    self.scope = ChapterBrowserScope(self.session, False)
                    await self.scope.start()
                    return self.session, self.scope
                except BaseException:
                    await self.close()
                    raise
        try:
            self.manager = async_playwright()
            self.playwright = await self.manager.__aenter__()
            self.session = await create_browser_session(self.playwright, self.config,
                load_cookies(self.cookie_path), self.logger)
            self.session.page.set_default_timeout(15000)
            self.scope = ChapterBrowserScope(self.session, False)
            await self.scope.start()
            return self.session, self.scope
        except BaseException:
            await self.close()
            raise

    async def close(self):
        manager, session, scope = self.manager, self.session, self.scope
        self.manager = self.playwright = self.session = self.scope = None
        try:
            if scope is not None:
                await scope.close()
        finally:
            try:
                if session is not None:
                    await session.close()
            finally:
                if manager is not None:
                    await manager.__aexit__(None, None, None)

    async def __aexit__(self, *exc):
        await self.close()


@asynccontextmanager
async def review_browser(config, logger, cookie_path, runtime=None):
    if runtime is not None:
        yield await runtime.acquire()
    else:
        async with ChapterReviewBrowser(config, logger, cookie_path) as owned:
            yield await owned.acquire()


async def apply_review(config, logger, cookie_path, directory, *, auto_submit=False, callback=None,
                       question_ids=None, runtime=None):
    """核对当前页面后使用已预览/手动编辑的答案，不再次调用模型。"""
    directory = Path(directory)
    state = review_record_state(directory)
    if state == 'already_submitted':
        context = read_json_safely(directory / 'context.json', {})
        test_match = re.fullmatch(r'test-(\d+)', directory.name)
        course_match = re.fullmatch(r'course-(\d+)', directory.parent.name)
        index = context.get('index')
        index = index if isinstance(index, int) else (int(test_match[1]) - 1 if test_match else 0)
        course = context.get('course', int(course_match[1]) if course_match else 1)
        logger.info('该历史记录已交卷，跳过本次回填与保存。')
        emit(callback, 'test_finished', course=course, test=index + 1,
             status='already_submitted', directory=str(directory))
        return 'already_submitted'
    if state != 'ready':
        name = state.removeprefix('missing_') + '.json'
        raise LLMError(f'该历史记录缺少 {name}，没有可回填的完整答案。请先重新运行“生成答案预览”。')
    try:
        context = read_json(directory / 'context.json')
        records = read_json(directory / 'review.json')
        raw_questions = read_json(directory / 'questions.json')
        if not isinstance(context, dict) or not isinstance(records, list) or not isinstance(raw_questions, list):
            raise ValueError()
        if not context.get('url') or not isinstance(context.get('index'), int) or context['index'] < 0:
            raise ValueError()
        questions = [Question(**q) for q in raw_questions]
        mapping = {}
        duplicates = set()
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get('question'), dict):
                continue
            qid = record['question'].get('question_id')
            if not isinstance(qid, str) or not qid:
                continue
            if qid in mapping:
                duplicates.add(qid)
            mapping[qid] = record
        for qid in duplicates:
            mapping[qid] = None
    except (OSError, ValueError, TypeError, KeyError):
        raise LLMError('本地章节关联、题目或答案文件无法读取，请重新生成答案预览。') from None
    question_set = {q.question_id for q in questions}
    if len(question_set) != len(questions):
        raise LLMError('本地题目 ID 重复，请重新读取题目。')
    threshold = LLMSettings.from_config(config).min_confidence
    overwrite = overwrite_enabled(config)
    selected = set(question_ids) if question_ids is not None else {q.question_id for q in questions}
    if not selected or not selected <= question_set:
        raise LLMError('所选题目不属于当前章节，请重新选择。')
    answers = {}
    omissions = {}
    for q in questions:
        record = mapping.get(q.question_id)
        answer, omission = answer_for_submission(record, q, threshold)
        if q.question_id not in selected:
            # Submission covers the whole chapter, including unselected low-confidence rows.
            if auto_submit and omission:
                answers[q.question_id] = None
                omissions[q.question_id] = omission
            continue
        answers[q.question_id] = answer
        if omission:
            omissions[q.question_id] = omission
    async with review_browser(config, logger, cookie_path, runtime) as (session, scope):
        page = None
        stage = '打开课程'
        try:
            await open_authenticated_course(session, config, context['url'], logger, cookie_path, callback)
            async def authenticate():
                await restore_course_login(session, config, context['url'], logger, cookie_path, callback)
                return session.page
            navigation = callback or (lambda event: logger.info(event['message']))
            stage = '打开章节测试'
            page, supported = await open_chapter_test(session.page, context['index'], navigation, scope=scope,
                authenticate=authenticate if getattr(config, 'login_url', None) else None)
            if not supported:
                if await is_submitted_test_page(page):
                    status = await record_submitted_test(page, directory)
                    logger.info('该章节已提交，跳过本次回填与保存。')
                    emit(callback, 'test_finished', course=context.get('course', 1), test=context['index'] + 1,
                         status=status, directory=str(directory))
                    return status
                raise LLMError(await test_page_problem(page))
            save_cookies(normalize_zhihuishu_cookies(await session.context.cookies()), cookie_path)
            stage = '核对全部题目'
            verified = await verify_questions(page, questions, directory / 'verification', callback=navigation)
            expected_choices = {q.question_id: {o['id'] for o in q.options if o['selected']} for q in verified}
            preserved = []
            for fresh in verified:
                if fresh.question_id not in answers:
                    continue
                if keep_existing_choice(fresh, mapping.get(fresh.question_id), threshold, overwrite):
                    preserved.append(fresh.number)
                    emit(callback, 'answer_preserved', number=fresh.number)
                    continue
                answer = answers[fresh.question_id]
                stage = f'回填第 {fresh.number} 题'
                await check_after_verification(page, questions, expected_choices,
                    directory / 'verification', navigation)
                await fill_answer(page, fresh, answer or Answer([], 0, ''), allow_empty=answer is None)
                expected_choices[fresh.question_id] = set(answer.option_ids) if answer else set()
                emit(callback, 'answer_filled' if answer else 'answer_skipped', number=fresh.number,
                     reason=omissions[fresh.question_id]['reason'] if answer is None else '')
            stage = '交卷' if auto_submit else '暂存'
            skipped = [item for item in omissions.values() if item['number'] not in preserved]
            try:
                if skipped and all(item['kind'] == 'invalid_answer' for item in skipped) and not any(expected_choices.values()):
                    status = 'skipped_no_answers'
                    emit(callback, 'test_navigation', message='本章节没有可用答案，已跳过保存或交卷，继续后续章节。')
                else:
                    await check_after_verification(page, questions, expected_choices,
                        directory / 'verification', navigation)
                    status = await finish_exam(page, auto_submit, allow_unanswered=True)
            except SaveConfirmationError as original_error:
                if auto_submit:
                    raise  # 已选中的答案不能用来推断已经交卷。
                stage = '核对暂存结果'
                try:
                    status, page = await recheck_saved_exam(session, scope, config, logger, cookie_path,
                        directory, questions, expected_choices, original_error, page=page, callback=callback)
                except Exception as failed:
                    page = getattr(failed, 'chapter_page', page)
                    raise
            if status != 'already_submitted':
                write_json(directory / 'result.json', {'status': status, 'question_count': len(questions),
                    'filled_count': len(answers) - len(skipped) - len(preserved),
                    'skipped_low_confidence': [item['number'] for item in skipped if item['kind'] == 'low_confidence'],
                    'skipped_questions': skipped,
                    'preserved_existing': preserved, 'overwrite_answers': overwrite,
                    'selected_question_ids': [q.question_id for q in questions if q.question_id in selected],
                    'confirmation': getattr(page, '_autovisor_save_response', {})})
            emit(callback, 'test_finished', course=context.get('course', 1), test=context['index'] + 1,
                 status=status, directory=str(directory))
            return status
        except Exception as exc:
            # 不覆盖题目、手动答案或已有预览；每次失败留下独立诊断。
            page = getattr(exc, 'chapter_page', page)
            stage = getattr(exc, 'chapter_stage', stage)
            failed = directory / 'apply-errors' / datetime.now().strftime('%Y%m%d-%H%M%S-%f')
            failed.mkdir(parents=True, exist_ok=True)
            error = re.sub(r'https?://[^\s\"\x27<>]+', '[URL]', str(exc))
            for secret in (getattr(config, 'username', ''), getattr(config, 'password', '')):
                if isinstance(secret, str) and secret:
                    error = error.replace(secret, '[REDACTED]')
            diagnostic = {'stage': stage, 'error': error}
            if page:
                diagnostic['save_response'] = getattr(page, '_autovisor_save_response', {})
            if page and not page.is_closed():
                try:
                    diagnostic['page'] = await test_page_state(page)
                    await page.screenshot(path=str(failed / 'error.png'), full_page=True, timeout=5000)
                except Exception:
                    pass
            write_json(failed / 'diagnostic.json', diagnostic)
            logger.error(f'{stage}失败：{error}；诊断记录：{failed}')
            emit(callback, 'test_finished', course=context.get('course', 1), test=context['index'] + 1,
                 status='failed', directory=str(failed), source_directory=str(directory), error=f'{stage}失败：{error}')
            raise
        finally:
            # SSO 可能留下中转弹窗。逐章仅保留当前课程根页，不重启浏览器。
            chapter_pages = set(scope.pages)
            if page is not None:
                chapter_pages.add(page)
            for owned_page in chapter_pages:
                if owned_page is session.page or owned_page.is_closed():
                    continue
                try:
                    await owned_page.close()
                except Error:
                    pass  # 浏览器已断开时，下一章节由共享会话恢复。
