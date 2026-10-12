import json
import asyncio
import configparser
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from playwright.async_api import async_playwright, Response, TimeoutError

from modules.chapter_tests import extract_questions, fill_answer, finish_exam, solve_exam, apply_review, write_json, open_chapter_test, guard_request, generate_answers, ChapterBrowserScope, ensure_chapter_login, SaveConfirmationError, chapter_page_status, run_chapter_tests, load_course_entries, track_chapter_run
from modules.login import LoginFailure, LoginTimeout, wait_for_login_complete
from modules.configs import Config
from modules.llm import Answer, LLMError, LLMSettings, VisionLLM
from modules.chapter_tests import selected_ids as selected_ids_for_test


def review_config(threshold=0.8):
    config = Config.__new__(Config)
    config._config = configparser.ConfigParser(interpolation=None)
    config._config.read_dict({'llm-option': {'minConfidence': str(threshold)}})
    return config


class ChapterRouteTests(unittest.TestCase):
    def test_save_status_accepts_only_integer_codes_or_exact_strings(self):
        from modules.chapter_tests import matches_status_code
        for value in (1, '1'):
            self.assertTrue(matches_status_code(value, (1,)))
        for value in (200, '200'):
            self.assertTrue(matches_status_code(value, (200,)))
        for value in (True, False, 1.0, 0, '0', '01', ' 1 ', '+1', '', None, [], {}):
            with self.subTest(value=value):
                self.assertFalse(matches_status_code(value, (1,)))
        for value in (200.0, True, '0200', '200.0', 500, '500'):
            with self.subTest(value=value):
                self.assertFalse(matches_status_code(value, (200,)))

    def test_completed_record_without_review_skips_without_opening_browser(self):
        for status in ('already_submitted', 'submitted'):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root:
                directory = Path(root) / 'course-2' / 'test-7'
                directory.mkdir(parents=True)
                write_json(directory / 'result.json', {'status': status})
                original = (directory / 'result.json').read_bytes()
                events = []
                with patch('modules.chapter_tests.create_browser_session', AsyncMock()) as create:
                    result = asyncio.run(apply_review(Mock(), Mock(), 'unused', directory, callback=events.append))
                self.assertEqual(result, 'already_submitted')
                create.assert_not_called()
                self.assertEqual((directory / 'result.json').read_bytes(), original)
                self.assertEqual(events[0]['course'], 2)
                self.assertEqual(events[0]['test'], 7)
                self.assertFalse((directory / 'review.json').exists())

    def test_missing_review_has_actionable_error_before_browser(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root)
            write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
            write_json(directory / 'questions.json', [])
            with patch('modules.chapter_tests.create_browser_session', AsyncMock()) as create:
                with self.assertRaisesRegex(LLMError, 'review.json.*生成答案预览'):
                    asyncio.run(apply_review(Mock(), Mock(), 'unused', directory))
            create.assert_not_called()

    def test_run_metadata_records_success_failure_and_cancellation(self):
        async def exercise(directory, error):
            async with track_chapter_run(directory, 'preview', 'fixture-model'):
                self.assertEqual(json.loads((directory / 'run.json').read_text(encoding='utf-8'))['status'], 'running')
                if error:
                    raise error
        for error, expected in ((None, 'completed'), (RuntimeError('fixture'), 'failed'),
                                (asyncio.CancelledError(), 'cancelled')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                try:
                    asyncio.run(exercise(directory, error))
                except (RuntimeError, asyncio.CancelledError):
                    pass
                metadata = json.loads((directory / 'run.json').read_text(encoding='utf-8'))
                self.assertEqual(metadata['status'], expected)
                self.assertEqual(metadata['model'], 'fixture-model')
                self.assertIn('finished_at', metadata)

    def test_result_route_is_distinct_from_editor_and_redirect_parameters(self):
        host = 'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html'
        self.assertEqual(chapter_page_status(host + '#/webExamList/checkHomework/fixture'), 'already_submitted')
        self.assertEqual(chapter_page_status(host + '#/webExamList/dohomework/fixture?redirect=checkHomework'), 'answer')
        self.assertEqual(chapter_page_status('https://login.zhihuishu.com/?return=checkHomework'), 'unknown')
        self.assertEqual(chapter_page_status(host + '#/newResult/fixture', '作业结果_在线学堂'), 'already_submitted')


class ChapterBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_review_skips_missing_failed_malformed_and_duplicate_answers(self):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
        valid = [{'question': q.to_dict(), 'status': 'answered',
            'answer': {'option_ids': ids, 'confidence': 0.99, 'explanation': 'fixture'}}
            for q, ids in zip(questions, [['11'], ['21'], ['31']])]
        session = SimpleNamespace(page=self.page, context=self.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.open_authenticated_course', AsyncMock()), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(return_value=(self.page, True))), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch.object(self.page, 'close', AsyncMock()):
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            for kind in ('failed', 'missing', 'malformed', 'duplicate'):
                with self.subTest(kind=kind):
                    records = [valid[0], valid[2]]
                    if kind == 'failed':
                        records.append(dict(valid[1], status='failed', error='fixture model failure'))
                    elif kind == 'malformed':
                        records.append(dict(valid[1], answer={'option_ids': [], 'confidence': 0.99}))
                    elif kind == 'duplicate':
                        records.extend([valid[1], valid[1]])
                    write_json(directory / 'review.json', records)
                    original = (directory / 'review.json').read_bytes()
                    await self.page.evaluate('window.forceUnanswered=true')
                    events = []
                    submit = kind == 'duplicate'
                    status = await apply_review(review_config(), Mock(), 'unused', directory,
                        auto_submit=submit, callback=events.append)
                    self.assertEqual(status, 'submitted' if submit else 'saved')
                    self.assertEqual(await self.page.locator('input:checked').evaluate_all('els=>els.map(e=>e.value)'), ['11', '31'])
                    result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
                    self.assertEqual([item['number'] for item in result['skipped_questions']], [2])
                    self.assertEqual(result['skipped_questions'][0]['kind'], 'invalid_answer')
                    self.assertEqual((directory / 'review.json').read_bytes(), original)
                    self.assertTrue(any(e['kind'] == 'answer_skipped' and e['number'] == 2 for e in events))

    async def test_new_run_continues_after_one_chapter_fails(self):
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(
            content_type='text/html', body='<div class="test-box">one</div><div class="test-box">two</div>'))
        exam = await self.context.new_page()
        await exam.set_content((Path(__file__).parent / 'fixtures/chapter_test.html').read_text(encoding='utf-8'))
        config = review_config()
        config.config_path = str(Path(self.directory.name) / 'config')
        config.course_urls = ['https://chapter.test/']
        session = SimpleNamespace(page=self.page, context=self.context, close=AsyncMock())
        events = []
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(side_effect=[
                    LoginTimeout('fixture login timeout', self.page), (exam, True)])) as opened:
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            with self.assertRaisesRegex(LLMError, '全部所选章节处理结束'):
                await run_chapter_tests(config, Mock(), 'unused', export_only=True, callback=events.append)
        self.assertEqual(opened.await_count, 2)
        root = Path(next(e['path'] for e in events if e['kind'] == 'artifact'))
        report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual([record['status'] for record in report], ['failed', 'exported'])
        self.assertEqual(json.loads((root / 'run.json').read_text(encoding='utf-8'))['status'], 'failed')

    async def automatic_save_recovery_fixture(self, saved):
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(
            content_type='text/html', body='<div class="test-box">one</div>'))
        fixture = (Path(__file__).parent / 'fixtures/chapter_test.html').read_text(encoding='utf-8')
        exam, reread = await self.context.new_page(), await self.context.new_page()
        expected = [['11'], [], ['31']]
        stored = expected if saved else [[], [], []]
        reread_fixture = fixture + '<script>const stored=' + json.dumps(stored) + ''';
            document.querySelectorAll('.examPaper_subject').forEach((box,index)=>
                box.querySelectorAll('input').forEach(e=>e.checked=stored[index].includes(e.value)));
            </script>'''
        def response_for(body):
            return lambda route: route.fulfill(content_type='text/html', body=body)
        for page, body in ((exam, fixture), (reread, reread_fixture)):
            await page.route('https://recovery.test/**', response_for(body))
            await page.goto('https://recovery.test/')
        config = review_config()
        config.config_path = str(Path(self.directory.name) / 'config')
        config.course_urls = ['https://chapter.test/']
        session = SimpleNamespace(page=self.page, context=self.context, close=AsyncMock())
        events, opens = [], []
        async def open_fixture(*args, scope, **kwargs):
            self.assertEqual(scope.read_only, bool(opens))
            opens.append(True)
            return (exam if len(opens) == 1 else reread), True
        async def generated(questions, directory, solver, **kwargs):
            records = [{'question': q.to_dict(), 'status': 'answered',
                'answer': {'option_ids': {1: ['11'], 2: ['21'], 3: ['31']}[q.number],
                           'confidence': 0.1 if q.number == 2 else 0.9, 'explanation': 'fixture'}} for q in questions]
            write_json(directory / 'review.json', records)
            return records
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch('modules.chapter_tests.VisionLLM', return_value=SimpleNamespace(settings=LLMSettings())), \
                patch('modules.chapter_tests.generate_answers', AsyncMock(side_effect=generated)), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(side_effect=open_fixture)), \
                patch('modules.chapter_tests.finish_exam', AsyncMock(side_effect=SaveConfirmationError('fixture lost confirmation'))) as finish, \
                patch('modules.chapter_tests.fill_answer', AsyncMock(wraps=fill_answer)) as fill:
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            if saved:
                await run_chapter_tests(config, Mock(), 'unused', mode='save', callback=events.append)
            else:
                with self.assertRaisesRegex(LLMError, '全部所选章节处理结束'):
                    await run_chapter_tests(config, Mock(), 'unused', mode='save', callback=events.append)
        self.assertEqual(finish.await_count, 1)
        self.assertEqual(fill.await_count, 3)
        self.assertEqual(len(opens), 2)
        root = Path(next(e['path'] for e in events if e['kind'] == 'artifact'))
        return root, root / 'course-1/test-1'

    async def test_start_selected_chapters_rechecks_lost_save_confirmation(self):
        root, directory = await self.automatic_save_recovery_fixture(saved=True)
        report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report[0]['status'], 'saved')
        result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['confirmation']['method'], 'reloaded_saved_choices')
        self.assertEqual(result['skipped_low_confidence'], [2])
        self.assertEqual(json.loads((root / 'run.json').read_text(encoding='utf-8'))['status'], 'completed')

    async def test_start_selected_chapters_does_not_claim_unsaved_choices_succeeded(self):
        root, directory = await self.automatic_save_recovery_fixture(saved=False)
        report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(report[0]['status'], 'failed')
        self.assertEqual(report[0]['stage'], '核对暂存结果')
        self.assertFalse((directory / 'result.json').exists())

    async def test_incremental_runs_reuse_history_retry_only_low_and_empty_then_allow_overwrite(self):
        from modules.chapter_tests import question_signature, content_signature
        await self.install_vue_choice_fixture()
        root = Path(self.directory.name) / 'chapter-tests'
        old = root / '20261009-100000' / 'course-1' / 'test-1'
        current = root / '20261009-110000' / 'course-1' / 'test-1'
        other = root / '20261009-103000' / 'course-1' / 'test-1'
        for directory in (old, current, other):
            directory.mkdir(parents=True)
            write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0, 'chapter_name': 'same chapter'})
        questions = await extract_questions(self.page, old)
        records = [{'question': q.to_dict(), 'signature': question_signature(q),
            'content_signature': content_signature(q), 'model': 'older-model',
            'status': 'failed' if q.number == 3 else 'answered',
            'answer': {'option_ids': ids, 'confidence': 0.1 if q.number == 2 else 0.99, 'explanation': 'fixture'}}
            for q, ids in zip(questions, [['11'], ['21'], ['31']])]
        write_json(old / 'review.json', records)
        original = (old / 'review.json').read_bytes()
        write_json(other / 'context.json', {'url': 'https://another-course.test/', 'index': 0})
        write_json(other / 'review.json', [dict(r, status='answered', answer=dict(r['answer'], confidence=0.99)) for r in records])
        await fill_answer(self.page, questions[0], Answer(['11'], 1, ''))
        await fill_answer(self.page, questions[1], Answer(['21'], 1, ''))
        calls = []
        async def answer(q, **kwargs):
            calls.append(q.number)
            return Answer({1: ['12'], 2: ['22'], 3: ['32']}[q.number], 0.99, 'new answer')
        solver = SimpleNamespace(settings=LLMSettings(model='new-model', request_interval=0), answer=answer)
        self.assertEqual(await solve_exam(self.page, current, solver), 'saved')
        self.assertEqual(calls, [2, 3])
        self.assertEqual((old / 'review.json').read_bytes(), original)
        reread = await extract_questions(self.page, current / 'verification')
        self.assertEqual([selected_ids_for_test(q) for q in reread], [['11'], ['22'], ['32']])
        result = json.loads((current / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['preserved_existing'], [1])
        self.assertEqual(result['filled_count'], 2)
        latest = json.loads((current / 'review.json').read_text(encoding='utf-8'))
        self.assertEqual(latest[0]['model'], 'older-model')
        self.assertEqual(latest[0]['source_directory'], str(old.resolve()))
        calls.clear()
        self.assertEqual(await solve_exam(self.page, current, solver, overwrite=True), 'saved')
        self.assertEqual(calls, [1, 2, 3])
        reread = await extract_questions(self.page, current / 'verification')
        self.assertEqual(selected_ids_for_test(reread[0]), ['12'])

    async def test_unknown_existing_web_answer_is_preserved_without_model_request(self):
        await self.page.locator('input[value="12"]').evaluate('e=>e.checked=true')
        calls = []
        async def answer(q, **kwargs):
            calls.append(q.number)
            return Answer({2: ['21'], 3: ['31']}[q.number], 0.99, '')
        solver = SimpleNamespace(settings=LLMSettings(model='fixture', request_interval=0), answer=answer)
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver), 'saved')
        self.assertEqual(calls, [2, 3])
        self.assertTrue(await self.page.locator('input[value="12"]').is_checked())
        review = json.loads((Path(self.directory.name) / 'review.json').read_text(encoding='utf-8'))
        self.assertEqual(review[0]['status'], 'manual')

    async def test_apply_review_defaults_to_preserve_but_can_overwrite_existing_high_answers(self):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
        write_json(directory / 'review.json', [{'question': q.to_dict(), 'status': 'answered',
            'answer': {'option_ids': ids, 'confidence': 0.99, 'explanation': 'fixture'}}
            for q, ids in zip(questions, [['11'], ['21'], ['31']])])
        await self.page.locator('input[value="12"]').evaluate('e=>e.checked=true')
        config = review_config()
        session = SimpleNamespace(page=self.page, context=self.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.open_authenticated_course', AsyncMock()), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(return_value=(self.page, True))), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch.object(self.page, 'close', AsyncMock()):
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            self.assertEqual(await apply_review(config, Mock(), 'unused', directory), 'saved')
            self.assertTrue(await self.page.locator('input[value="12"]').is_checked())
            config._config.set('llm-option', 'overwriteAnswers', 'True')
            self.assertEqual(await apply_review(config, Mock(), 'unused', directory), 'saved')
            self.assertTrue(await self.page.locator('input[value="11"]').is_checked())

    async def test_video_login_precedes_course_loading_and_is_reused_between_tests(self):
        course = '''<div class="test-box" style="padding:20px" onclick="window.open(
            'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/checkHomework/fixture')">test 1</div>
            <div class="test-box" style="padding:20px" onclick="window.open(
            'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/checkHomework/fixture')">test 2</div>'''
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(content_type='text/html', body=course))
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html', body='<title>作业结果</title>'))
        config = review_config()
        config.config_path = str(Path(self.directory.name) / 'config')
        config.course_urls = ['https://chapter.test/']
        calls = []
        async def confirmed(*args, **kwargs):
            calls.append('video-login')
        real_loader = load_course_entries
        async def loaded(*args, **kwargs):
            self.assertEqual(calls[-1], 'video-login')
            calls.append('course-load')
            return await real_loader(*args, **kwargs)
        session = SimpleNamespace(page=self.page, context=self.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.ensure_chapter_login', AsyncMock(side_effect=confirmed)), \
                patch('modules.chapter_tests.load_course_entries', AsyncMock(side_effect=loaded)):
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            self.assertEqual(await run_chapter_tests(config, Mock(), 'unused', export_only=True), 0)
        self.assertEqual(calls, ['video-login', 'course-load'])

    async def batch_review_fixture(self, *, submit=False, fail_first=True):
        from modules.chapter_batch import apply_review_batch
        questions = await extract_questions(self.page, self.directory.name)
        targets = {}
        for index in range(2):
            directory = Path(self.directory.name) / 'course-1' / f'test-{index + 1}'
            directory.mkdir(parents=True)
            write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': index})
            write_json(directory / 'questions.json', [q.to_dict() for q in questions])
            write_json(directory / 'review.json', [{'question': q.to_dict(), 'status': 'answered',
                'answer': {'option_ids': ids, 'confidence': 0.99, 'explanation': 'fixture'}}
                for q, ids in zip(questions, [['11'], ['21'], ['31']])])
            targets[directory] = None
        course = ''.join(f'''<div class="test-box" style="padding:20px" onclick="window.open(
            'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/{index}')">test</div>'''
            for index in range(2))
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(content_type='text/html', body=course))
        fixture = (Path(__file__).parent / 'fixtures/chapter_test.html').read_text(encoding='utf-8')
        writes = []
        def serve_exam(route):
            if route.request.method == 'GET':
                return route.fulfill(content_type='text/html', body=fixture)
            writes.append(route.request.url)
            return route.fulfill(content_type='application/json', body=json.dumps(
                {'status': '200', 'rt': {'statu': '-1' if fail_first and len(writes) == 1 else '1'}}))
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', serve_exam)
        foreign = await self.context.new_page()
        session = SimpleNamespace(browser=self.browser, page=self.page, context=self.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)) as created, \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch('modules.chapter_tests.ensure_chapter_login', AsyncMock()) as login, \
                patch('modules.chapter_tests.load_course_entries', AsyncMock(wraps=load_course_entries)) as loaded:
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await apply_review_batch(review_config(), Mock(), 'unused', targets, auto_submit=submit)
        self.assertEqual(result, {'batch': True, 'total': 2, 'completed': 1 if fail_first else 2,
                                 'failed': 1 if fail_first else 0, 'skipped': 0})
        self.assertEqual(created.await_count, 1)
        self.assertEqual(login.await_count, 1)
        self.assertEqual(loaded.await_count, 1)
        self.assertEqual(session.close.await_count, 1)
        self.assertEqual(len(writes), 2)
        self.assertTrue(all(url.endswith('/answer/submit' if submit else '/answer/temporarySave') for url in writes))
        self.assertFalse(self.page.is_closed())
        self.assertFalse(foreign.is_closed())
        self.assertEqual(set(self.context.pages), {self.page, foreign})
        if fail_first:
            self.assertFalse((next(iter(targets)) / 'result.json').exists())
        for directory in list(targets)[1:] if fail_first else targets:
            self.assertEqual(json.loads((directory / 'result.json').read_text(encoding='utf-8'))['status'],
                             'submitted' if submit else 'saved')
        await foreign.close()

    async def test_batch_review_uses_one_browser_and_course_load_after_chapter_failure(self):
        await self.batch_review_fixture()

    async def test_batch_submission_uses_one_browser_and_course_for_all_chapters(self):
        await self.batch_review_fixture(submit=True, fail_first=False)

    async def test_cached_course_reauthenticates_after_login_form_appears(self):
        from modules.chapter_tests import open_authenticated_course
        session = SimpleNamespace(page=self.page)
        async def load(*args, **kwargs):
            await self.page.set_content('<div class="test-box">test</div>')
            return 1
        with patch('modules.chapter_tests.ensure_chapter_login', AsyncMock()) as login, \
                patch('modules.chapter_tests.load_course_entries', AsyncMock(side_effect=load)) as loaded:
            for attempt in range(2):
                self.assertEqual(await open_authenticated_course(session, Mock(), 'https://chapter.test/', Mock(), 'unused'), 1)
                if attempt == 0:
                    await self.page.evaluate('''() => {
                        const input = document.createElement('input'); input.id = 'lUsername';
                        document.body.append(input);
                    }''')
        self.assertEqual(login.await_count, 2)
        self.assertEqual(loaded.await_count, 2)

    async def test_cached_course_rejects_changed_catalog_and_does_not_interrupt_verification(self):
        from modules.chapter_tests import open_authenticated_course
        session = SimpleNamespace(page=self.page)
        async def load(*args, **kwargs):
            await self.page.set_content('<div class="test-box">test</div>')
            return 1
        with patch('modules.chapter_tests.ensure_chapter_login', AsyncMock()) as login, \
                patch('modules.chapter_tests.load_course_entries', AsyncMock(side_effect=load)):
            await open_authenticated_course(session, Mock(), 'https://chapter.test/', Mock(), 'unused')
            with patch('modules.chapter_tests.has_visible_verification', AsyncMock(return_value=True)):
                with self.assertRaisesRegex(LLMError, '安全验证'):
                    await open_authenticated_course(session, Mock(), 'https://chapter.test/', Mock(), 'unused')
            await self.page.set_content('<div class="test-box">one</div><div class="test-box">two</div>')
            with self.assertRaisesRegex(LLMError, '章节列表发生变化'):
                await open_authenticated_course(session, Mock(), 'https://chapter.test/', Mock(), 'unused')
        self.assertEqual(login.await_count, 1)

    async def test_switching_courses_invalidates_course_cache(self):
        from modules.chapter_tests import open_authenticated_course
        session = SimpleNamespace(page=self.page)
        async def load(*args, **kwargs):
            await self.page.set_content('<div class="test-box">test</div>')
            return 1
        with patch('modules.chapter_tests.ensure_chapter_login', AsyncMock()) as login, \
                patch('modules.chapter_tests.load_course_entries', AsyncMock(side_effect=load)) as loaded:
            for url in ('https://chapter.test/?course=1', 'https://chapter.test/?course=1',
                        'https://chapter.test/?course=2', 'https://chapter.test/?course=2'):
                self.assertEqual(await open_authenticated_course(session, Mock(), url, Mock(), 'unused'), 1)
        self.assertEqual(login.await_count, 2)
        self.assertEqual(loaded.await_count, 2)

    async def show_verification_fixture(self):
        await self.page.evaluate('''() => {
            const popup = document.createElement('div'); popup.className = 'yidun_popup';
            popup.innerHTML = '<div class="yidun_modal" style="position:fixed;top:10px;left:10px;width:200px;height:80px;background:white">fixture verification loading</div>';
            document.body.append(popup);
        }''')

    async def test_review_waits_for_manual_verification_without_closing_or_writing(self):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
        write_json(directory / 'review.json', [{'question': q.to_dict(), 'status': 'answered',
            'answer': {'option_ids': ids, 'confidence': 0.99, 'explanation': 'fixture'}}
            for q, ids in zip(questions, [['11'], ['21'], ['31']])])
        course = await self.context.new_page()
        session = SimpleNamespace(browser=self.browser, page=course, context=self.context, close=AsyncMock())
        await self.show_verification_fixture()
        paused = asyncio.Event()
        events = []
        def notify(event):
            events.append(event)
            if event.get('state') == '等待验证':
                paused.set()
        async def opened(*args, scope, **kwargs):
            await scope.prepare_page(self.page)
            return self.page, True
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)) as create, \
                patch('modules.chapter_tests.open_authenticated_course', AsyncMock()), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(side_effect=opened)), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch('modules.chapter_tests.fill_answer', AsyncMock(wraps=fill_answer)) as fill, \
                patch('modules.chapter_tests.finish_exam', AsyncMock(wraps=finish_exam)) as finish:
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            job = asyncio.create_task(apply_review(review_config(), Mock(), 'unused', directory, callback=notify))
            try:
                await asyncio.wait_for(paused.wait(), timeout=5)
                self.assertFalse(job.done())
                self.assertFalse(self.page.is_closed())
                fill.assert_not_awaited()
                finish.assert_not_awaited()
                await self.page.evaluate("document.querySelector('.yidun_popup').remove()")
                self.assertEqual(await asyncio.wait_for(job, timeout=10), 'saved')
            finally:
                if not job.done():
                    job.cancel()
                await asyncio.gather(job, return_exceptions=True)
        create.assert_awaited_once()
        finish.assert_awaited_once()
        self.assertEqual(fill.await_count, 3)
        self.assertTrue(any(e.get('state') == '重新核对' for e in events))

    async def test_verification_mid_check_rechecks_earlier_questions(self):
        from modules.chapter_tests import verify_questions, go_to_question
        questions = await extract_questions(self.page, self.directory.name)
        visited = []
        async def navigate(page, index):
            visited.append(index)
            await go_to_question(page, index)
            if visited == [0, 1]:
                await self.show_verification_fixture()
                await self.page.evaluate('''() => setTimeout(() => {
                    document.querySelector('input[value="11"]').value = 'changed';
                    document.querySelector('.yidun_popup').remove();
                }, 200)''')
        with patch('modules.chapter_tests.go_to_question', AsyncMock(side_effect=navigate)):
            with self.assertRaisesRegex(LLMError, '内容发生变化'):
                await verify_questions(self.page, questions, Path(self.directory.name) / 'verification')
        self.assertEqual(visited, [0, 1, 0])
        self.assertIsNone(await self.page.evaluate('lastFinish'))

    async def test_extract_restarts_when_verification_resets_current_question(self):
        from modules.chapter_tests import read_question
        visited = []
        async def read(page, index, directory):
            visited.append(index)
            question = await read_question(page, index, directory)
            if visited == [0, 1]:
                await self.show_verification_fixture()
                await self.page.evaluate('''() => setTimeout(() => {
                    show(0); document.querySelector('.yidun_popup').remove();
                }, 200)''')
            return question
        with patch('modules.chapter_tests.read_question', AsyncMock(side_effect=read)):
            questions = await extract_questions(self.page, self.directory.name)
        self.assertEqual(visited, [0, 1, 0, 1, 2])
        self.assertEqual([q.number for q in questions], [1, 2, 3])
        self.assertEqual(len({q.question_id for q in questions}), 3)

    async def test_extract_navigation_logger_does_not_receive_question_events(self):
        messages = []
        await self.show_verification_fixture()
        await self.page.evaluate("setTimeout(() => document.querySelector('.yidun_popup').remove(), 200)")
        questions = await extract_questions(self.page, self.directory.name,
            navigation=lambda event: messages.append(event['message']))
        self.assertEqual(len(questions), 3)
        self.assertEqual(len(messages), 2)
        self.assertIn('已暂停', messages[0])
        self.assertIn('重新核对', messages[1])

    async def test_verification_before_submission_rejects_changed_selections(self):
        from modules.chapter_tests import check_after_verification
        questions = await extract_questions(self.page, self.directory.name)
        expected = {q.question_id: set() for q in questions}
        await self.show_verification_fixture()
        await self.page.evaluate('''() => setTimeout(() => {
            document.querySelector('input[value="11"]').checked = true;
            document.querySelector('.yidun_popup').remove();
        }, 200)''')
        with self.assertRaisesRegex(LLMError, '网页答案发生变化'):
            await check_after_verification(self.page, questions, expected, Path(self.directory.name) / 'verification')
        self.assertIsNone(await self.page.evaluate('lastFinish'))

    async def test_verification_timeout_leaves_waiting_page_and_does_not_write(self):
        from modules.chapter_tests import wait_for_chapter_verification
        await self.show_verification_fixture()
        with self.assertRaisesRegex(LLMError, '等待安全验证超时') as failed:
            await wait_for_chapter_verification(self.page, timeout=50)
        self.assertEqual(failed.exception.chapter_stage, '等待安全验证')
        self.assertFalse(self.page.is_closed())
        self.assertIsNone(await self.page.evaluate('lastFinish'))

    async def test_verification_wait_can_be_cancelled_and_handles_closed_page(self):
        from modules.chapter_tests import wait_for_chapter_verification
        await self.show_verification_fixture()
        paused = asyncio.Event()
        job = asyncio.create_task(wait_for_chapter_verification(self.page, lambda event: paused.set()))
        await asyncio.wait_for(paused.wait(), timeout=5)
        job.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await job
        self.assertIsNone(await self.page.evaluate('lastFinish'))
        await self.page.close()
        with self.assertRaisesRegex(LLMError, '窗口已关闭'):
            await wait_for_chapter_verification(self.page)

    async def test_video_page_login_form_blocks_test_entry_even_when_directory_exists(self):
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(content_type='text/html',
            body='<input id="lUsername"><input type="password"><div class="test-box">directory</div>'))
        with self.assertRaisesRegex(LoginFailure, '视频学习网站仍显示登录表单'):
            await load_course_entries(SimpleNamespace(page=self.page), SimpleNamespace(), 'https://chapter.test/',
                                      Mock(), 'unused', timeout=500, allow_login=False)

    async def test_test_redirect_to_video_platform_restores_login_before_reopening(self):
        authenticated = False
        await self.context.route('https://wisdom-mooc.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html', body='<p>video login needed</p>'))
        def exam(route):
            return route.fulfill(content_type='text/html', body='<meta charset="utf-8"><title>作业结果</title>' if authenticated else
                '<script>location.href="https://wisdom-mooc.zhihuishu.com/study/index"</script>')
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', exam)
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="window.open(
            'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/checkHomework/fixture')">test</div>''')
        # Start at an unclassified route so a delayed result route cannot be accepted before the video redirect.
        await self.page.evaluate("document.querySelector('.test-box').onclick=()=>window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList')")
        async def confirm():
            nonlocal authenticated
            authenticated = True
            return self.page
        auth = AsyncMock(side_effect=confirm)
        page, supported = await asyncio.wait_for(open_chapter_test(self.page, 0, authenticate=auth, sso_grace=0.1), 5)
        self.assertFalse(supported)
        self.assertEqual(chapter_page_status(page.url, await page.title()), 'already_submitted')
        auth.assert_awaited_once()
        await page.close()

    async def test_normal_sso_gateway_is_allowed_to_redirect_without_forcing_login(self):
        target = 'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture'
        fixture = (Path(__file__).parent / 'fixtures/chapter_test.html').read_text(encoding='utf-8')
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html', body=fixture))
        await self.context.route('https://studentexam-api.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html',
            body=f'<script>setTimeout(()=>location.href="{target}",600)</script>'))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            window.open('https://studentexam-api.zhihuishu.com/studentExam/gateway/f/v1/gologin/fixture')">test</div>''')
        auth = AsyncMock()
        page, supported = await asyncio.wait_for(open_chapter_test(self.page, 0, authenticate=auth, sso_grace=2), 5)
        self.assertTrue(supported)
        auth.assert_not_called()
        await page.close()

    async def test_http_400_login_stops_quickly_without_600_second_wait(self):
        await self.context.route('https://studentexam-api.zhihuishu.com/**', lambda route: route.fulfill(status=400,
            content_type='text/html', body='<div id="main-frame-error"><div class="error-code">HTTP ERROR 400</div></div>'))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            window.open('https://studentexam-api.zhihuishu.com/studentExam/gateway/f/v1/gologin/fixture')">test</div>''')
        with self.assertRaisesRegex(LoginFailure, '400') as failure:
            await asyncio.wait_for(open_chapter_test(self.page, 0, sso_grace=0.1), 4)
        self.assertFalse(failure.exception.page.is_closed())
        self.assertEqual(failure.exception.chapter_stage, '章节登录')

    async def test_auth_failure_keeps_both_actual_pages_for_diagnostics(self):
        await self.context.route('https://login.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html', body='<p>login</p>'))
        await self.page.set_content('''<div class="test-box" style="padding:20px"
            onclick="window.open('https://login.zhihuishu.com/')">test</div>''')
        auth = AsyncMock(side_effect=LoginTimeout('fixture login timeout', self.page))
        with self.assertRaises(LoginTimeout) as failure:
            await asyncio.wait_for(open_chapter_test(self.page, 0, authenticate=auth, sso_grace=0.1), 4)
        self.assertFalse(failure.exception.chapter_page.is_closed())
        self.assertIs(failure.exception.page, self.page)
        auth.assert_awaited_once()

    async def test_cookie_refresh_retries_one_400_then_preserves_foreign_state(self):
        hits = []
        def login(route):
            hits.append(route.request.url)
            return route.fulfill(status=400 if len(hits) == 1 else 200, content_type='text/html',
                body='<p>bad cookie</p>' if len(hits) == 1 else '<script>location.href="https://www.zhihuishu.com/"</script>')
        await self.context.route('https://passport.zhihuishu.com/**', login)
        await self.context.route('https://www.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html', body='<p>ready</p>'))
        await self.context.add_cookies([{'name': 'stale', 'value': 'fixture', 'domain': 'passport.zhihuishu.com', 'path': '/'},
                                       {'name': 'foreign', 'value': 'fixture', 'domain': 'outside.test', 'path': '/'}])
        session = SimpleNamespace(page=self.page, context=self.context, attached=False)
        await asyncio.wait_for(ensure_chapter_login(session, SimpleNamespace(login_url='https://passport.zhihuishu.com/login'),
            Mock(), Path(self.directory.name) / 'cookies.json', cookie_check_timeout=2000), 5)
        self.assertEqual(len(hits), 2)
        self.assertNotIn('stale', {c['name'] for c in await self.context.cookies()})
        self.assertIn('foreign', {c['name'] for c in await self.context.cookies()})

    async def test_login_observes_owned_destination_but_not_existing_unrelated_tab(self):
        await self.context.route('https://www.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html', body='<p>ready</p>'))
        foreign = await self.context.new_page()
        await foreign.goto('https://www.zhihuishu.com/')
        await self.context.route('https://passport.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html',
            body='<script>setTimeout(()=>window.open("https://www.zhihuishu.com/"),300)</script>'))
        session = SimpleNamespace(page=self.page, context=self.context, attached=False)
        scope = ChapterBrowserScope(session, True)
        await scope.start()
        try:
            await asyncio.wait_for(ensure_chapter_login(session, SimpleNamespace(login_url='https://passport.zhihuishu.com/login'),
                Mock(), Path(self.directory.name) / 'cookies.json', cookie_check_timeout=2000), 5)
            self.assertIsNot(session.page, foreign)
            self.assertIsNot(session.page, self.page)
            self.assertIn(session.page, scope.pages)
        finally:
            await scope.close()
        self.assertFalse(foreign.is_closed())

    async def test_login_native_error_page_is_not_authenticated_destination(self):
        await self.context.route('https://www.zhihuishu.com/**', lambda route: route.fulfill(content_type='text/html',
            body='<div id="main-frame-error"><div class="error-code">HTTP ERROR 400</div></div>'))
        await self.page.goto('https://www.zhihuishu.com/')
        with self.assertRaisesRegex(LoginFailure, '400'):
            await asyncio.wait_for(wait_for_login_complete(self.page, timeout=2000), 3)

    async def test_batch_auth_timeout_reports_login_stage_and_saves_actual_page(self):
        course = '<div class="test-box">test</div>'
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(content_type='text/html', body=course))
        config = review_config()
        config.config_path = str(Path(self.directory.name) / 'config')
        config.course_urls = ['https://chapter.test/']
        session = SimpleNamespace(page=self.page, context=self.context, close=AsyncMock())
        events = []
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(side_effect=LoginTimeout('fixture login timeout', self.page))):
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            with self.assertRaisesRegex(LLMError, '登录失败'):
                await run_chapter_tests(config, Mock(), 'unused', export_only=True, callback=events.append)
        root = Path(next(e['path'] for e in events if e['kind'] == 'artifact'))
        diagnostic = root / 'course-1/test-1/diagnostic.json'
        self.assertEqual(json.loads(diagnostic.read_text(encoding='utf-8'))['stage'], '章节登录')
        self.assertTrue((diagnostic.parent / 'page-info.json').exists())
        self.assertTrue((diagnostic.parent / 'error.png').exists())
        self.assertNotIn('页面读取或操作超时', json.dumps(events, ensure_ascii=False))

    async def test_invalid_model_response_skips_failed_questions_and_saves_valid_answer(self):
        calls = []
        def respond(request):
            data = json.loads(request.content)
            number = json.loads(data['messages'][1]['content'][0]['text'])['number']
            calls.append(number)
            answer = {'option_ids': ['11'] if number == 1 else [], 'confidence': 0.9,
                      'explanation': 'fixture'}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(answer)}}]})
        solver = VisionLLM(LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture', retry_delay=0,
                                      request_interval=0), httpx.MockTransport(respond))
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver), 'saved')
        self.assertEqual(calls, [1, 2, 2, 2, 3, 3, 3])
        records = json.loads((Path(self.directory.name) / 'review.json').read_text(encoding='utf-8'))
        self.assertEqual([r['status'] for r in records], ['answered', 'failed', 'failed'])
        self.assertEqual(len(records[1]['llm_diagnostics']), 3)
        self.assertEqual(await self.page.locator('input:checked').evaluate_all('els=>els.map(e=>e.value)'), ['11'])
        result = json.loads((Path(self.directory.name) / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual([item['number'] for item in result['skipped_questions']], [2, 3])

    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='chrome', headless=True)
        self.context = await self.browser.new_context()
        self.page = await self.context.new_page()
        self.directory = tempfile.TemporaryDirectory()
        fixture = Path(__file__).parent / 'fixtures' / 'chapter_test.html'
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(
            content_type='text/html', body=fixture.read_text(encoding='utf-8')) if route.request.method == 'GET'
            else route.fulfill(content_type='application/json', body=json.dumps({'status': 200, 'rt': {'statu': 1}})))
        await self.page.goto('https://chapter.test/')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()
        self.directory.cleanup()

    async def install_vue_choice_fixture(self, delay=0):
        await self.page.evaluate('''delay => {
            window.optionClicks = 0;
            document.querySelectorAll('.examPaper_subject').forEach(box => {
                box.querySelectorAll('.label').forEach(label => {
                    const input = label.querySelector('input');
                    const text = label.innerText;
                    const marker = document.createElement('span'); marker.className='ABCase';
                    marker.textContent = text.slice(0,2);
                    const detail = document.createElement('div'); detail.className='node_detail';
                    detail.textContent = text.slice(2).trim();
                    label.replaceChildren(input, marker, detail);
                    label.onclick=()=>{
                        window.optionClicks++;
                        const selected = input.type==='radio' || !marker.classList.contains('onChecked');
                        setTimeout(()=>{
                            if(input.type==='radio') box.querySelectorAll('.onChecked').forEach(e=>e.classList.remove('onChecked'));
                            marker.classList.toggle('onChecked',selected);
                            detail.classList.toggle('onChecked',selected);
                        },delay);
                    };
                });
            });
            const originalFinish = window.finish;
            window.finish = async function() {
                window.savedVueAnswers = Array.from(document.querySelectorAll('.examPaper_subject')).map(box=>
                    Array.from(box.querySelectorAll('.label')).filter(e=>e.querySelector('.ABCase.onChecked'))
                        .map(e=>e.querySelector('input').value));
                return originalFinish();
            };
        }''', delay)

    async def confirmation_recovery_fixture(self, saved, auto_submit=False):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        choices = [['11'], ['21'], ['31', '33']]
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
        write_json(directory / 'review.json', [{'question': q.to_dict(), 'status': 'answered',
            'meets_confidence': True, 'answer': {'option_ids': ids, 'confidence': 0.9, 'explanation': 'fixture'}}
            for q, ids in zip(questions, choices)])
        course = await self.context.new_page()
        reread = await self.context.new_page()
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        await course.route('https://chapter.test/**', lambda route: route.fulfill(
            content_type='text/html', body=fixture.replace('<body>', '<body><div class="test-box"></div>')))
        stored = choices if saved else [[], [], []]
        reread_body = fixture + '<script>const stored=' + json.dumps(stored) + ''';
            document.querySelectorAll('.examPaper_subject').forEach((box,index)=>
                box.querySelectorAll('input').forEach(e=>e.checked=stored[index].includes(e.value)));
            </script>'''
        await reread.route('https://chapter.test/**', lambda route: route.fulfill(content_type='text/html', body=reread_body))
        await reread.goto('https://chapter.test/')
        session = SimpleNamespace(page=course, context=self.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(side_effect=[(self.page, True), (reread, True)])) as opened, \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch('modules.chapter_tests.finish_exam', AsyncMock(side_effect=SaveConfirmationError('confirmation lost'))) as finish, \
                patch('modules.chapter_tests.fill_answer', AsyncMock(wraps=fill_answer)) as fill:
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            try:
                result = await apply_review(review_config(), Mock(), 'unused.json', directory, auto_submit=auto_submit)
            except SaveConfirmationError as error:
                result = error
            self.assertEqual(finish.await_count, 1)
            self.assertEqual(fill.await_count, 3)
            self.assertEqual(opened.await_count, 1 if auto_submit else 2)
        return result, directory

    async def test_popup_waits_for_delayed_navigation_and_spa_route(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        fixture += '<script>setTimeout(()=>location.hash="/webExamList/dohomework/fixture",250)</script>'
        await self.page.context.route('https://onlineexamh5new.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=fixture))
        course = '''<div class="test-box" style="padding:20px" onclick="
            const p=window.open('about:blank');
            setTimeout(()=>p.location.href='https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList',300)
            ">章节测试</div>'''
        await self.page.set_content(course)
        opened, supported = await open_chapter_test(self.page, 0)
        try:
            self.assertTrue(supported)
            self.assertIn('/dohomework/', opened.url)
        finally:
            await opened.close()

    async def test_submitted_result_does_not_wait_for_answer_controls(self):
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', lambda route: route.fulfill(
            content_type='text/html', body='<title>作业结果_在线学堂</title><p>fixture result</p>'))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/checkHomework/fixture')
            ">章节测试</div>''')
        opened, supported = await asyncio.wait_for(open_chapter_test(self.page, 0), 3)
        try:
            self.assertFalse(supported)
            self.assertEqual(chapter_page_status(opened.url), 'already_submitted')
        finally:
            await opened.close()

    async def test_delayed_result_route_ends_editor_wait(self):
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', lambda route: route.fulfill(
            content_type='text/html', body='<script>setTimeout(()=>location.hash="/webExamList/checkHomework/fixture",200)</script>'))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture')
            ">章节测试</div>''')
        opened, supported = await asyncio.wait_for(open_chapter_test(self.page, 0), 3)
        try:
            self.assertFalse(supported)
            self.assertIn('/checkHomework/', opened.url)
        finally:
            await opened.close()

    async def test_login_can_open_result_tab_without_closing_login_tab(self):
        target = 'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/checkHomework/fixture'
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', lambda route: route.fulfill(
            content_type='text/html', body='<title>作业结果_在线学堂</title>'))
        await self.context.route('https://login.zhihuishu.com/**', lambda route: route.fulfill(
            content_type='text/html', body=f'<script>setTimeout(()=>window.open("{target}"),400)</script>'))
        await self.page.set_content('''<div class="test-box" style="padding:20px"
            onclick="window.open('https://login.zhihuishu.com/')">章节测试</div>''')
        scope = ChapterBrowserScope(SimpleNamespace(page=self.page, context=self.context), True)
        await scope.start()
        try:
            opened, supported = await asyncio.wait_for(open_chapter_test(self.page, 0, scope=scope), 4)
            self.assertFalse(supported)
            self.assertEqual(opened.url, target)
        finally:
            await scope.close()

    async def test_batch_skips_submitted_chapter_and_continues_to_next(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        course = '''<div class="test-box" style="padding:20px" onclick="
            window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html?kind=done#/webExamList/checkHomework/first')">first</div>
            <div class="test-box" style="padding:20px" onclick="
            window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html?kind=answer#/webExamList/dohomework/second')">second</div>'''
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(content_type='text/html', body=course))
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', lambda route: route.fulfill(
            content_type='text/html', body='<title>作业结果_在线学堂</title>' if 'kind=done' in route.request.url else fixture))
        config = review_config()
        config.config_path = str(Path(self.directory.name) / 'config')
        config.course_urls = ['https://chapter.test/']
        session = SimpleNamespace(page=self.page, context=self.context, close=AsyncMock())
        events = []
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch('modules.chapter_tests.VisionLLM') as model:
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await asyncio.wait_for(run_chapter_tests(config, Mock(), 'unused.json',
                export_only=True, selected_tests={1: [0, 1]}, callback=events.append), 8)
        self.assertEqual(result, 0)
        model.assert_not_called()
        root = Path(next(e['path'] for e in events if e['kind'] == 'artifact'))
        report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual([r['status'] for r in report], ['already_submitted', 'exported'])
        self.assertEqual(json.loads((root / 'run.json').read_text(encoding='utf-8'))['status'], 'completed')
        self.assertTrue(json.loads((root / 'course-1/test-2/context.json').read_text(encoding='utf-8'))['chapter_name'])
        self.assertFalse((root / 'course-1/test-1/questions.json').exists())
        self.assertEqual(len(json.loads((root / 'course-1/test-2/questions.json').read_text(encoding='utf-8'))), 3)

    async def test_review_of_submitted_chapter_never_fills_or_submits_again(self):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
        write_json(directory / 'review.json', [{'question': q.to_dict(), 'status': 'answered',
            'answer': {'option_ids': ids, 'confidence': 0.9, 'explanation': 'fixture'}}
            for q, ids in zip(questions, [['11'], ['21'], ['31', '33']])])
        original = (directory / 'review.json').read_bytes()
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(
            content_type='text/html', body=fixture.replace('<body>', '<body><div class="test-box"></div>')))
        result_page = await self.context.new_page()
        await result_page.route('https://onlineexamh5new.zhihuishu.com/**', lambda route: route.fulfill(
            content_type='text/html', body='<title>作业结果_在线学堂</title>'))
        await result_page.goto('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/checkHomework/fixture')
        session = SimpleNamespace(page=self.page, context=self.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(return_value=(result_page, False))), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch('modules.chapter_tests.fill_answer', AsyncMock()) as fill, \
                patch('modules.chapter_tests.finish_exam', AsyncMock()) as finish:
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            self.assertEqual(await apply_review(review_config(), Mock(), 'unused.json', directory,
                                               auto_submit=True), 'already_submitted')
        fill.assert_not_called()
        finish.assert_not_called()
        self.assertEqual((directory / 'review.json').read_bytes(), original)
        self.assertEqual(json.loads((directory / 'result.json').read_text(encoding='utf-8'))['status'], 'already_submitted')

    async def test_chapter_authentication_refreshes_on_normal_login_page(self):
        await self.context.add_cookies([
            {'name': 'stale_login', 'value': 'fixture', 'domain': 'passport.zhihuishu.com', 'path': '/'},
            {'name': 'foreign', 'value': 'fixture', 'domain': 'outside.test', 'path': '/'},
        ])
        login = '''<input id="lUsername"><input id="lPassword" type="password">
            <button class="wall-sub-btn" onclick="
                if(document.querySelector('#lUsername').value==='fixture-user' &&
                   document.querySelector('#lPassword').value==='fixture-password') {
                    document.cookie='fresh_login=1; domain=.zhihuishu.com; path=/; Secure';
                    location.href='https://www.zhihuishu.com/';
                }">登录</button>'''
        await self.context.route('https://passport.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=login))
        await self.context.route('https://www.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body='<p>fixture destination</p>'))
        config = SimpleNamespace(login_url='https://passport.zhihuishu.com/login',
            username='fixture-user', password='fixture-password')
        session = SimpleNamespace(page=self.page, context=self.context, attached=False)
        path = Path(self.directory.name) / 'login-cookies.json'
        await asyncio.wait_for(ensure_chapter_login(session, config, Mock(), path,
                                                   cookie_check_timeout=100), 5)
        cookies = await self.context.cookies()
        self.assertIn('foreign', {c['name'] for c in cookies})
        self.assertNotIn('stale_login', {c['name'] for c in cookies})
        saved = json.loads(path.read_text(encoding='utf-8'))
        self.assertIn('fresh_login', {c['name'] for c in saved})
        self.assertNotIn('foreign', {c['name'] for c in saved})
        self.assertEqual(self.page.url, 'https://www.zhihuishu.com/')

    async def test_chapter_login_attempts_slider_after_account_submission(self):
        login = '''<input id="lUsername"><input id="lPassword" type="password">
            <button class="wall-sub-btn" onclick="document.querySelector('.yidun_bgimg').style.display='block'">登录</button>
            <div class="yidun_bgimg" style="display:none">fixture captcha
                <button id="fixture-accept" onclick="
                    document.cookie='fresh_login=1; domain=.zhihuishu.com; path=/; Secure';
                    location.href='https://www.zhihuishu.com/';">通过</button>
            </div>'''
        await self.context.route('https://passport.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=login))
        await self.context.route('https://www.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body='<p>fixture destination</p>'))
        session = SimpleNamespace(page=self.page, context=self.context, attached=False)
        config = SimpleNamespace(login_url='https://passport.zhihuishu.com/login',
            username='fixture-user', password='fixture-password', enableAutoCaptcha=True)
        dependencies = [object(), object()]
        async def fixture_slider(page, modules):
            self.assertIs(modules, dependencies)
            await page.locator('.yidun_bgimg').wait_for(state='visible', timeout=2000)
            self.assertEqual(await page.locator('#lUsername').input_value(), 'fixture-user')
            self.assertEqual(await page.locator('#lPassword').input_value(), 'fixture-password')
            await page.locator('#fixture-accept').click()
            return True
        events = []
        with patch('modules.installer.start', return_value=dependencies), \
                patch('modules.slider.slider_verify', AsyncMock(side_effect=fixture_slider)) as slider:
            await asyncio.wait_for(ensure_chapter_login(session, config, Mock(),
                Path(self.directory.name) / 'cookies.json', callback=events.append,
                cookie_check_timeout=100, login_timeout=3000), 8)
        slider.assert_awaited_once()
        self.assertEqual(session.page.url, 'https://www.zhihuishu.com/')
        saved = json.loads((Path(self.directory.name) / 'cookies.json').read_text(encoding='utf-8'))
        self.assertIn('fresh_login', {cookie['name'] for cookie in saved})
        self.assertTrue(any('自动滑块已启用' in event.get('message', '') for event in events))

    async def test_chapter_reuses_valid_login_without_clearing_context(self):
        await self.context.route('https://passport.zhihuishu.com/**', lambda route: route.fulfill(
            content_type='text/html', body='<script>setTimeout(()=>location.href="https://www.zhihuishu.com/",100)</script>'))
        await self.context.route('https://www.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body='<p>fixture destination</p>'))
        session = SimpleNamespace(page=self.page, context=self.context, attached=False)
        config = SimpleNamespace(login_url='https://passport.zhihuishu.com/login', username='', password='')
        with patch.object(self.context, 'clear_cookies', AsyncMock()) as clear, \
                patch('modules.chapter_tests.complete_standard_login', AsyncMock()) as login:
            await asyncio.wait_for(ensure_chapter_login(session, config, Mock(),
                Path(self.directory.name) / 'cookies.json', cookie_check_timeout=2000), 5)
        clear.assert_not_called()
        login.assert_not_called()

    async def test_course_directory_retries_once_and_accepts_collapsed_entries(self):
        hits = []
        def course(route):
            hits.append(route.request.url)
            return route.fulfill(content_type='text/html', body='<p>loading</p>' if len(hits) == 1
                                 else '<div class="test-box" style="display:none">chapter</div>')
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', course)
        events = []
        config = SimpleNamespace(config_path=str(Path(self.directory.name) / 'config'))
        with patch('modules.chapter_tests.ensure_chapter_login', AsyncMock()) as login:
            count = await asyncio.wait_for(load_course_entries(SimpleNamespace(page=self.page), config,
                'https://chapter.test/', Mock(), 'unused', events.append, timeout=400), 3)
        self.assertEqual(count, 1)
        self.assertEqual(len(hits), 2)
        self.assertTrue(any('重试' in e['message'] for e in events))
        login.assert_not_called()

    async def test_course_directory_observes_delayed_login_redirect(self):
        hits = []
        def course(route):
            hits.append(route.request.url)
            return route.fulfill(content_type='text/html', body='<script>setTimeout(()=>location.href="https://login.zhihuishu.com/",150)</script>'
                                 if len(hits) == 1 else '<div class="test-box"></div>')
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', course)
        await self.context.route('https://login.zhihuishu.com/**', lambda route: route.fulfill(
            content_type='text/html', body='<p>login</p>'))
        config = SimpleNamespace(login_url='https://login.zhihuishu.com/')
        with patch('modules.chapter_tests.ensure_chapter_login', AsyncMock()) as login:
            self.assertEqual(await asyncio.wait_for(load_course_entries(SimpleNamespace(page=self.page), config,
                'https://chapter.test/', Mock(), 'unused', timeout=2000), 5), 1)
        login.assert_awaited_once()
        self.assertEqual(len(hits), 2)

    async def test_course_directory_waits_for_manual_verification_without_refreshing(self):
        hits = []
        def course(route):
            hits.append(route.request.url)
            return route.fulfill(content_type='text/html', body='''<div class="yidun_popup"><div class="yidun_modal"
                style="width:100px;height:100px">verification</div></div><div class="test-box"></div>
                <script>setTimeout(()=>document.querySelector('.yidun_popup').remove(),350)</script>''')
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', course)
        events = []
        self.assertEqual(await asyncio.wait_for(load_course_entries(SimpleNamespace(page=self.page), SimpleNamespace(),
            'https://chapter.test/', Mock(), 'unused', events.append, timeout=250), 3), 1)
        self.assertEqual(len(hits), 1)
        self.assertTrue(any('验证码' in e['message'] for e in events))

    async def test_course_directory_does_not_refresh_unfinished_verification(self):
        hits = []
        def course(route):
            hits.append(route.request.url)
            return route.fulfill(content_type='text/html', body='''<div class="yidun_popup"><div class="yidun_modal"
                style="width:100px;height:100px">verification</div></div>''')
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', course)
        config = SimpleNamespace(config_path=str(Path(self.directory.name) / 'config'))
        with self.assertRaisesRegex(LLMError, '验证码尚未完成'):
            await asyncio.wait_for(load_course_entries(SimpleNamespace(page=self.page), config,
                'https://chapter.test/', Mock(), 'unused', timeout=500, verification_timeout=200), 3)
        self.assertEqual(len(hits), 1)

    async def test_course_directory_timeout_records_pending_api_without_secrets(self):
        await self.check_pending_course_api('https://studyservice-api.zhihuishu.com/gateway/t/v1/learning/videolist',
                                            '课程目录接口未返回')

    async def test_course_directory_timeout_identifies_pending_account_api(self):
        await self.check_pending_course_api('https://appcomm-user.zhihuishu.com/app-commserv-user/userInfo/getUserInfoAndAuthInfo',
                                            '账号状态接口未返回')

    async def check_pending_course_api(self, endpoint, reason):
        released = asyncio.Event()
        async def api(route):
            await released.wait()
            try:
                await route.fulfill(content_type='application/json', body='{}')
            except Exception:
                pass
        await self.context.route(endpoint + '**', api)
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(content_type='text/html',
            body='<script>fetch(' + json.dumps(endpoint + '?ticket=fixture-secret') + ')</script>'))
        config = SimpleNamespace(config_path=str(Path(self.directory.name) / 'config'))
        try:
            with self.assertRaisesRegex(LLMError, reason):
                await asyncio.wait_for(load_course_entries(SimpleNamespace(page=self.page), config,
                    'https://chapter.test/?ticket=fixture-secret', Mock(), 'unused', timeout=400, retries=0), 4)
            diagnostic = next((Path(self.directory.name) / '.autovisor/course-entry-errors').glob('*/diagnostic.json'))
            text = diagnostic.read_text(encoding='utf-8')
            self.assertNotIn('fixture-secret', text)
            self.assertEqual(json.loads(text)['attempts'], 1)
            self.assertIn(endpoint, text)
            self.assertTrue((diagnostic.parent / 'error.png').exists())
        finally:
            released.set()

    async def test_course_directory_missing_test_entries_has_clear_error_and_bounded_retry(self):
        hits = []
        def course(route):
            hits.append(route.request.url)
            return route.fulfill(content_type='text/html', body='<div class="el-collapse-item__header">chapter</div>')
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', course)
        config = SimpleNamespace(config_path=str(Path(self.directory.name) / 'config'))
        with self.assertRaisesRegex(LLMError, '没有章节测试入口'):
            await asyncio.wait_for(load_course_entries(SimpleNamespace(page=self.page), config,
                'https://chapter.test/', Mock(), 'unused', timeout=250), 4)
        self.assertEqual(len(hits), 2)

    async def test_exam_login_refreshes_account_then_reopens_course_entry(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        authenticated = False
        hits = []
        def exam_route(route):
            hits.append(route.request.url)
            body = fixture if authenticated else '<script>setTimeout(()=>location.href="https://login.zhihuishu.com/",100)</script>'
            return route.fulfill(content_type='text/html', body=body)
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**', exam_route)
        await self.context.route('https://login.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body='<p>fixture login</p>'))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture')
            ">章节测试</div>''')
        async def refresh():
            nonlocal authenticated
            authenticated = True
        refresh_login = AsyncMock(side_effect=refresh)
        scope = ChapterBrowserScope(SimpleNamespace(page=self.page, context=self.context), True)
        await scope.start()
        try:
            opened, supported = await asyncio.wait_for(open_chapter_test(self.page, 0,
                scope=scope, authenticate=refresh_login, sso_grace=0.1), 5)
            self.assertTrue(supported)
            self.assertFalse(opened.is_closed())
            refresh_login.assert_awaited_once()
            self.assertEqual(len(hits), 2)
        finally:
            await scope.close()

    async def test_exam_with_working_cookies_does_not_refresh_account(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        await self.context.route('https://onlineexamh5new.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=fixture))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture')
            ">章节测试</div>''')
        authenticate = AsyncMock()
        opened, supported = await open_chapter_test(self.page, 0, authenticate=authenticate)
        try:
            self.assertTrue(supported)
            authenticate.assert_not_called()
        finally:
            await opened.close()

    async def test_popup_waits_for_delayed_submit_button(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        fixture = fixture.replace('<button onclick="prepare(true)">提交作业</button>', '')
        fixture += '<script>setTimeout(()=>{const b=document.createElement("button");b.textContent="提交作业";document.body.append(b)},600)</script>'
        await self.page.context.route('https://onlineexamh5new.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=fixture))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture')
            ">章节测试</div>''')
        opened, supported = await open_chapter_test(self.page, 0)
        try:
            self.assertTrue(supported)
        finally:
            await opened.close()

    async def test_popup_login_redirect_continues_after_login(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        await self.page.context.route('https://onlineexamh5new.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=fixture))
        login = '''<p>登录中心</p><script>setTimeout(()=>location.href=
            'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture',400)</script>'''
        await self.page.context.route('https://login.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=login))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            const p=window.open('about:blank');
            setTimeout(()=>p.location.href='https://login.zhihuishu.com/',250)
            ">章节测试</div>''')
        events = []
        opened, supported = await open_chapter_test(self.page, 0, events.append)
        try:
            self.assertTrue(supported)
            self.assertTrue(any('手动登录' in e.get('message', '') for e in events))
        finally:
            await opened.close()

    async def test_login_http_failure_keeps_popup_open_for_retry(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        target = 'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture'
        await self.page.context.route('https://onlineexamh5new.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=fixture))
        await self.page.context.route('https://login.zhihuishu.com/**',
            lambda route: route.fulfill(status=503, content_type='text/html', body='<p>登录服务暂不可用</p>'))
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            const p=window.open('about:blank');
            setTimeout(()=>p.location.href='https://login.zhihuishu.com/',250)
            ">章节测试</div>''')
        events, retries = [], []
        def on_event(event):
            events.append(event)
            if '登录服务返回错误' in event.get('message', '') and not retries:
                async def retry():
                    await asyncio.sleep(0.25)
                    await self.page.context.pages[-1].goto(target)
                retries.append(asyncio.create_task(retry()))
        opened, supported = await asyncio.wait_for(open_chapter_test(self.page, 0, on_event), 8)
        try:
            self.assertTrue(supported)
            self.assertFalse(opened.is_closed())
            self.assertTrue(retries)
            await asyncio.gather(*retries)
        finally:
            await opened.close()

    async def test_login_replaces_popup_with_new_exam_tab(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        target = 'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture'
        await self.page.context.route('https://onlineexamh5new.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=fixture))
        login = f'''<p>登录中心</p><script>setTimeout(() => {{
            window.open('{target}'); window.close();
        }}, 400)</script>'''
        await self.page.context.route('https://login.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body=login))
        await self.page.set_content('''<div class="test-box" style="padding:20px"
            onclick="window.open('https://login.zhihuishu.com/')">章节测试</div>''')
        events = []
        scope = ChapterBrowserScope(SimpleNamespace(page=self.page, context=self.page.context), True)
        await scope.start()
        try:
            opened, supported = await asyncio.wait_for(open_chapter_test(self.page, 0, events.append, scope=scope), 8)
            self.assertTrue(supported, [(p.url, p.is_closed(), p in scope.pages) for p in self.page.context.pages])
            self.assertFalse(opened.is_closed())
            self.assertTrue(any('新标签页' in e.get('message', '') for e in events))
        finally:
            await scope.close()

    async def test_readonly_does_not_block_login_submission(self):
        route = Mock(request=SimpleNamespace(url='https://login.zhihuishu.com/submit', resource_type='xhr'),
                     abort=AsyncMock(), fallback=AsyncMock())
        await guard_request(route, True)
        route.fallback.assert_awaited_once()
        route.abort.assert_not_awaited()

    async def test_login_redirect_after_exam_route_is_observed(self):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        late_login = '<script>setTimeout(()=>location.href="https://login.zhihuishu.com/",250)</script>'
        await self.page.context.route('https://onlineexamh5new.zhihuishu.com/**', lambda route:
            route.fulfill(content_type='text/html', body=fixture if 'authorized=1' in route.request.url else late_login))
        await self.page.context.route('https://login.zhihuishu.com/**',
            lambda route: route.fulfill(content_type='text/html', body='<p>登录中心</p>'))
        target = 'https://onlineexamh5new.zhihuishu.com/stuExamWeb.html?authorized=1#/webExamList/dohomework/fixture'
        await self.page.set_content('''<div class="test-box" style="padding:20px" onclick="
            window.open('https://onlineexamh5new.zhihuishu.com/stuExamWeb.html#/webExamList/dohomework/fixture')
            ">章节测试</div>''')
        events, login_tasks = [], []
        def on_event(event):
            events.append(event)
            if '手动登录' in event.get('message', ''):
                login_tasks.append(asyncio.create_task(self.page.context.pages[-1].goto(target)))
        opened, supported = await open_chapter_test(self.page, 0, on_event)
        try:
            self.assertTrue(supported)
            self.assertTrue(login_tasks)
            await asyncio.gather(*login_tasks)
        finally:
            await opened.close()

    async def test_extract_closed_shadow_and_correctly_fill_shuffled_choices(self):
        questions = await extract_questions(self.page, self.directory.name)
        self.assertEqual(len(questions), 3)
        self.assertEqual(questions[0].stem_dom_text, '')
        self.assertTrue(Path(questions[0].stem_screenshot).is_file())
        self.assertEqual(questions[0].options[0]['id'], '12')
        await fill_answer(self.page, questions[0], Answer(['11'], 1, ''))
        await fill_answer(self.page, questions[1], Answer(['21'], 1, ''))
        await fill_answer(self.page, questions[2], Answer(['31', '32'], 1, ''))
        await fill_answer(self.page, questions[2], Answer(['33'], 1, ''))
        checked = await self.page.locator('input:checked').evaluate_all('els=>els.map(e=>e.value)')
        self.assertEqual(set(checked), {'11', '21', '33'})

    async def test_vue_choices_fill_and_save_without_hidden_input_checked(self):
        await self.install_vue_choice_fixture(delay=40)
        questions = await extract_questions(self.page, self.directory.name)
        await fill_answer(self.page, questions[0], Answer(['11'], 0.9, 'fixture'))
        await fill_answer(self.page, questions[1], Answer(['21'], 0.9, 'fixture'))
        await fill_answer(self.page, questions[2], Answer(['31', '33'], 0.9, 'fixture'))
        await fill_answer(self.page, questions[2], Answer(['32'], 0.9, 'fixture'))
        self.assertEqual(await self.page.locator('input:checked').count(), 0)
        reread = await extract_questions(self.page, self.directory.name)
        self.assertEqual([o['id'] for o in reread[0].options if o['selected']], ['11'])
        self.assertEqual([o['id'] for o in reread[2].options if o['selected']], ['32'])
        self.assertEqual(await finish_exam(self.page), 'saved')
        self.assertEqual(await self.page.evaluate('savedVueAnswers'), [['11'], ['21'], ['32']])
        self.assertEqual(await self.page.evaluate('lastFinish'), 'saved')

    async def test_vue_selected_marker_overrides_stale_native_checked_flag(self):
        await self.install_vue_choice_fixture()
        await self.page.locator('input[value="12"]').evaluate('e=>e.checked=true')
        await self.page.locator('input[value="11"]').evaluate('e=>e.parentElement.querySelector(".ABCase").classList.add("onChecked")')
        questions = await extract_questions(self.page, self.directory.name)
        self.assertEqual([o['id'] for o in questions[0].options if o['selected']], ['11'])
        await fill_answer(self.page, questions[0], Answer(['11'], 0.9, 'fixture'))
        self.assertEqual(await self.page.evaluate('optionClicks'), 0)

    async def test_stale_options_stop_before_filling(self):
        questions = await extract_questions(self.page, self.directory.name)
        await self.page.locator('input[value="11"]').evaluate('e=>e.value="changed"')
        with self.assertRaisesRegex(LLMError, '发生变化'):
            await fill_answer(self.page, questions[0], Answer(['11'], 1, ''))
        self.assertEqual(await self.page.locator('input:checked').count(), 0)

    async def test_question_number_in_zero_height_float_container(self):
        await self.page.add_style_tag(content='.subject_num {height:0;overflow:visible} .subject_num span {display:inline-block}')
        questions = await extract_questions(self.page, self.directory.name)
        self.assertEqual([q.number for q in questions], [1, 2, 3])

    async def test_complete_pipeline_with_mock_llm_and_save_then_submit(self):
        def respond(request):
            payload = json.loads(request.content)
            number = json.loads(payload['messages'][1]['content'][0]['text'])['number']
            ids = {1: ['11'], 2: ['21'], 3: ['31', '33']}[number]
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(
                {'option_ids': ids, 'confidence': 0.99, 'explanation': 'fixture'})}}]})
        solver = VisionLLM(LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture-model', request_interval=0, retry_delay=0), httpx.MockTransport(respond))
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver), 'saved')
        self.assertEqual(await self.page.evaluate('lastFinish'), 'saved')
        self.assertEqual(await finish_exam(self.page, auto_submit=True), 'submitted')
        self.assertEqual(await self.page.evaluate('lastFinish'), 'submitted')

    async def closing_save_fixture(self, delay=300):
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        fixture = fixture.replace('onclick="prepare(false)"', 'onclick="finish()"')
        fixture += '''<script>window.finish=async function(){
            const response=await fetch('/answer/temporarySave',{method:'POST'});
            await response.json(); setTimeout(()=>window.close(),300);
        };</script>'''.replace('close(),300', f'close(),{delay}')
        writes = []
        def route_request(route):
            if route.request.method == 'GET':
                return route.fulfill(content_type='text/html', body=fixture)
            writes.append(route.request.url)
            return route.fulfill(content_type='application/json', body='{"status":200,"rt":{"statu":1}}')
        await self.context.route('https://closing.test/**', route_request)
        async with self.page.expect_popup() as opened:
            await self.page.evaluate("window.open('https://closing.test/')")
        popup = await opened.value
        await popup.wait_for_load_state('domcontentloaded')
        return popup, writes

    async def test_finish_accepts_string_and_integer_success_statuses(self):
        reply = {}
        writes = []
        def respond(route):
            writes.append(route.request.url)
            return route.fulfill(content_type='application/json', body=json.dumps(reply))
        await self.page.route('**/answer/*', respond)
        cases = [(False, 200, 1), (False, '200', '1'), (False, 200, '1'),
                 (False, '200', 1), (True, '200', '1'), (True, '200', '0'), (True, 200, 0)]
        for submit, api_status, result_status in cases:
            with self.subTest(submit=submit, api_status=api_status, result_status=result_status):
                reply.update(status=api_status, rt={'statu': result_status})
                before = len(writes)
                self.assertEqual(await finish_exam(self.page, auto_submit=submit),
                                 'submitted' if submit else 'saved')
                self.assertEqual(len(writes), before + 1)
                self.assertEqual(self.page._autovisor_save_response['method'], 'api_response')
                self.assertEqual(self.page._autovisor_save_response['api_status'], api_status)
                self.assertEqual(self.page._autovisor_save_response['result_status'], result_status)

    async def test_finish_rejects_failure_statuses_and_malformed_success_codes(self):
        reply = {}
        http_status = 200
        writes = []
        def respond(route):
            writes.append(route.request.url)
            return route.fulfill(status=http_status, content_type='application/json', body=json.dumps(reply))
        await self.page.route('**/answer/*', respond)
        cases = [(200, {'status': '500', 'rt': {'statu': '1'}}),
                 (200, {'status': '200', 'rt': {'statu': '0'}}),
                 (200, {'status': '200', 'rt': {'statu': '-1'}}),
                 (200, {'status': '200', 'rt': {'statu': True}}),
                 (200, {'status': '200', 'rt': {'statu': 1.0}}),
                 (200, {'status': 200.0, 'rt': {'statu': '1'}}),
                 (200, {'status': '200', 'rt': {}}),
                 (200, {'status': '200', 'rt': '1'}),
                 (500, {'status': '200', 'rt': {'statu': '1'}})]
        for http_status, data in cases:
            with self.subTest(http_status=http_status, data=data):
                reply.clear()
                reply.update(data)
                before = len(writes)
                with self.assertRaisesRegex(LLMError, '网站未确认'):
                    await finish_exam(self.page)
                self.assertEqual(len(writes), before + 1)
                self.assertNotIn('method', self.page._autovisor_save_response)

    async def test_save_response_is_read_before_popup_closes(self):
        popup, writes = await self.closing_save_fixture()
        self.assertEqual(await finish_exam(popup), 'saved')
        self.assertEqual(len(writes), 1)
        self.assertTrue(popup.is_closed())
        self.assertEqual(popup._autovisor_save_response['method'], 'api_response')

    async def test_immediate_close_waits_for_slow_response_read_without_second_save(self):
        popup, writes = await self.closing_save_fixture(delay=0)
        original = Response.json
        async def delayed_json(response):
            if response.url.endswith('/answer/temporarySave'):
                await asyncio.sleep(3.4)
            return await original(response)
        with patch.object(Response, 'json', delayed_json):
            self.assertEqual(await finish_exam(popup), 'saved')
        self.assertTrue(popup.is_closed())
        self.assertEqual(len(writes), 1)

    async def test_close_override_is_removed_when_confirmation_wait_fails(self):
        original = await self.page.evaluate('window.close.toString()')
        with patch.object(self.page, 'expect_response', side_effect=TimeoutError('fixture timeout')):
            with self.assertRaises(SaveConfirmationError):
                await finish_exam(self.page)
        self.assertEqual(await self.page.evaluate('window.close.toString()'), original)
        self.assertFalse(await self.page.evaluate('!!window.__autovisorFinishClose'))

    async def test_missing_save_confirmation_recovers_from_stored_choices_without_rewriting(self):
        status, directory = await self.confirmation_recovery_fixture(saved=True)
        self.assertEqual(status, 'saved')
        result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['confirmation']['method'], 'reloaded_saved_choices')

    async def test_missing_save_confirmation_does_not_claim_unsaved_choices_succeeded(self):
        status, directory = await self.confirmation_recovery_fixture(saved=False)
        self.assertIsInstance(status, SaveConfirmationError)
        self.assertFalse((directory / 'result.json').is_file())
        error = next((directory / 'apply-errors').glob('*/diagnostic.json'))
        self.assertEqual(json.loads(error.read_text(encoding='utf-8'))['stage'], '核对暂存结果')

    async def test_saved_choices_do_not_prove_submission_when_confirmation_is_lost(self):
        status, directory = await self.confirmation_recovery_fixture(saved=True, auto_submit=True)
        self.assertIsInstance(status, SaveConfirmationError)
        self.assertFalse((directory / 'result.json').is_file())
        self.assertFalse((directory / 'saved-verification').exists())

    async def test_low_confidence_leaves_only_that_question_blank_and_submits(self):
        def respond(request):
            number = json.loads(json.loads(request.content)['messages'][1]['content'][0]['text'])['number']
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
                'option_ids': {1: ['11'], 2: ['21'], 3: ['31', '33']}[number],
                'confidence': 0.1 if number == 1 else 0.9})}}]})
        solver = VisionLLM(LLMSettings('https://llm.test/v1', 'fixture-key', 'fixture-model', request_interval=0, retry_delay=0), httpx.MockTransport(respond))
        await self.page.evaluate('window.forceUnanswered=true')
        events = []
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver, auto_submit=True, callback=events.append), 'submitted')
        self.assertEqual(await self.page.locator('input:checked').evaluate_all('els=>els.map(e=>e.value)'), ['21', '33', '31'])
        self.assertEqual(await self.page.evaluate('lastFinish'), 'submitted')
        result = json.loads((Path(self.directory.name) / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['skipped_low_confidence'], [1])
        self.assertEqual(result['filled_count'], 2)
        self.assertTrue(any(e['kind'] == 'answer_skipped' and e['number'] == 1 for e in events))

    async def test_unanswered_confirmation_is_cancelled(self):
        await self.page.evaluate('window.forceUnanswered=true')
        with self.assertRaisesRegex(LLMError, '未作答'):
            await finish_exam(self.page, auto_submit=True)
        self.assertIsNone(await self.page.evaluate('lastFinish'))

    async def test_model_service_error_reports_no_save(self):
        solver = VisionLLM(LLMSettings('https://llm.test/v1', 'key', 'fixture-model',
            request_interval=0, retry_delay=0), httpx.MockTransport(
            lambda _: httpx.Response(503)))
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver), 'skipped_no_answers')
        self.assertEqual(await self.page.locator('input:checked').count(), 0)
        self.assertIsNone(await self.page.evaluate('lastFinish'))

    async def test_single_question_retry_preserves_other_review_records(self):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        manual = {'question': questions[0].to_dict(), 'status': 'manual',
            'answer': {'option_ids': ['11'], 'confidence': 0, 'explanation': 'user choice'},
            'meets_confidence': True}
        write_json(directory / 'review.json', [manual])
        solver = VisionLLM(LLMSettings('https://llm.test/v1', 'key', 'fixture-model',
            request_interval=0, retry_delay=0), httpx.MockTransport(
            lambda _: httpx.Response(200, json={'choices': [{'message': {'content':
                '{"option_ids":["31","33"],"confidence":0.9,"explanation":"fixture"}'}}]})))
        result = await generate_answers([questions[2]], directory, solver, preview=True)
        self.assertEqual(result[0]['status'], 'answered')
        saved = json.loads((directory / 'review.json').read_text(encoding='utf-8'))
        self.assertEqual(len(saved), 2)
        self.assertEqual(next(r for r in saved if r['question']['number'] == 1)['status'], 'manual')

    async def test_cached_answers_obey_raised_confidence_threshold(self):
        calls = []
        def respond(request):
            number = json.loads(json.loads(request.content)['messages'][1]['content'][0]['text'])['number']
            calls.append(number)
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps({
                'option_ids': {1: ['11'], 2: ['21'], 3: ['31', '33']}[number],
                'confidence': 0.9, 'explanation': 'fixture'})}}]})
        solver = VisionLLM(LLMSettings('https://llm.test/v1', 'key', 'fixture-model', request_interval=0),
                           httpx.MockTransport(respond))
        await solve_exam(self.page, self.directory.name, solver, preview=True)
        solver.settings.min_confidence = 0.95
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver), 'saved')
        self.assertEqual(calls, [1, 2, 3, 1, 2, 3])
        self.assertEqual(await self.page.locator('input:checked').count(), 0)
        self.assertEqual(await self.page.evaluate('lastFinish'), 'saved')

    async def test_direct_solve_verifies_later_questions_before_filling(self):
        async def answer(question, **kwargs):
            if question.number == 3:
                await self.page.locator('.examPaper_subject').nth(1).locator('.label').first.evaluate('e=>e.append("changed")')
            return Answer({1: ['11'], 2: ['21'], 3: ['31', '33']}[question.number], 0.99, 'fixture')
        solver = SimpleNamespace(settings=LLMSettings(model='fixture-model', request_interval=0), answer=answer)
        with self.assertRaisesRegex(LLMError, '发生变化'):
            await solve_exam(self.page, self.directory.name, solver)
        self.assertEqual(await self.page.locator('input:checked').count(), 0)
        self.assertIsNone(await self.page.evaluate('lastFinish'))

    async def test_scope_only_guards_owned_pages_and_removes_its_routes(self):
        context = self.page.context
        await self.page.unroute('https://chapter.test/**')
        await context.route('https://chapter.test/**', lambda route: route.fulfill(
            content_type='application/json', body='{"ok":true}'))
        await context.route('https://outside.test/**', lambda route: route.fulfill(
            content_type='text/html' if route.request.method == 'GET' else 'application/json',
            body='<video></video>' if route.request.method == 'GET' else '{"ok":true}'))
        user_page = await context.new_page()
        await user_page.goto('https://outside.test/')
        native_play = await user_page.evaluate('HTMLMediaElement.prototype.play.toString()')
        scope = ChapterBrowserScope(SimpleNamespace(context=context, page=self.page), True)
        await scope.start()
        fetch = "fetch('/answer/submit',{method:'POST'}).then(()=>true).catch(()=>false)"
        try:
            self.assertFalse(await self.page.evaluate(fetch))
            self.assertTrue(await user_page.evaluate(fetch))
            await user_page.reload()
            self.assertEqual(await user_page.evaluate('HTMLMediaElement.prototype.play.toString()'), native_play)
        finally:
            await scope.close()
        self.assertTrue(await self.page.evaluate(fetch))
        await user_page.reload()
        self.assertEqual(await user_page.evaluate('HTMLMediaElement.prototype.play.toString()'), native_play)
        await user_page.close()

    async def test_scope_protects_popup_and_closes_only_owned_popup(self):
        context = self.page.context
        await context.route('https://owned.test/**', lambda route: route.fulfill(
            content_type='text/html', body='<video></video>'))
        user_page = await context.new_page()
        scope = ChapterBrowserScope(SimpleNamespace(context=context, page=self.page), True)
        await scope.start()
        await self.page.set_content('<button onclick="window.open(\'https://owned.test/\')">open</button>')
        async with self.page.expect_popup() as opened:
            await self.page.get_by_role('button', name='open').click()
        popup = await opened.value
        await scope.prepare_page(popup)
        self.assertTrue(await popup.evaluate('window.__autovisorMediaPaused'))
        self.assertFalse(await popup.evaluate("fetch('/answer/submit',{method:'POST'}).then(()=>true).catch(()=>false)"))
        await scope.close()
        self.assertTrue(popup.is_closed())
        self.assertFalse(user_page.is_closed())
        await user_page.close()

    async def test_preview_keeps_web_answers_untouched_and_retries_low_confidence(self):
        calls = []
        def respond(request):
            payload = json.loads(request.content)
            number = json.loads(payload['messages'][1]['content'][0]['text'])['number']
            calls.append(number)
            ids = {1: ['11'], 2: ['21'], 3: ['31', '33']}[number]
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(
                {'option_ids': ids, 'confidence': 0.2, 'explanation': 'preview fixture'})}}]})
        solver = VisionLLM(LLMSettings('https://llm.test/v1', 'key', 'preview-model', request_interval=0), httpx.MockTransport(respond))
        events = []
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver, preview=True, callback=events.append), 'preview')
        self.assertEqual(await self.page.locator('input:checked').count(), 0)
        self.assertIsNone(await self.page.evaluate('lastFinish'))
        self.assertEqual(len([e for e in events if e['kind'] == 'answer_ready']), 3)
        self.assertFalse(json.loads((Path(self.directory.name) / 'review.json').read_text(encoding='utf-8'))[0]['meets_confidence'])
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver, preview=True), 'preview')
        self.assertEqual(calls, [1, 2, 3, 1, 2, 3])

    async def test_apply_preview_matches_option_ids_after_letter_order_changes(self):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        answers = [['11'], ['21'], ['31', '33']]
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0, 'course': 1})
        write_json(directory / 'review.json', [{'question': q.to_dict(), 'status': 'answered',
            'meets_confidence': True, 'answer': {'option_ids': ids, 'confidence': 0.9, 'explanation': 'fixture'}}
            for q, ids in zip(questions, answers)])
        original = (directory / 'question-1.png').read_bytes()
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        shuffled = fixture.replace('records.forEach((q,index)=>{', 'records.forEach(q=>q.options.reverse()); records.forEach((q,index)=>{')
        shuffled = shuffled.replace('<body>', '<body><div class="test-box"></div>')
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(
            content_type='text/html', body=shuffled) if route.request.method == 'GET'
            else route.fulfill(content_type='application/json', body=json.dumps({'status': 200, 'rt': {'statu': 1}})))
        session = SimpleNamespace(page=self.page, context=self.page.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(return_value=(self.page, True))), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'):
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            status = await apply_review(review_config(), Mock(), 'unused.json', directory)
        self.assertEqual(status, 'saved')
        self.assertEqual((directory / 'question-1.png').read_bytes(), original)
        self.assertTrue((directory / 'verification' / 'question-1.png').is_file())

    async def test_review_recalculates_low_confidence_and_saves_blank(self):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
        write_json(directory / 'review.json', [{'question': q.to_dict(), 'status': 'answered',
            'meets_confidence': True, 'answer': {'option_ids': ids, 'confidence': 0.9, 'explanation': 'fixture'}}
            for q, ids in zip(questions, [['11'], ['21'], ['31', '33']])])
        session = SimpleNamespace(page=self.page, context=self.page.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.open_authenticated_course', AsyncMock()), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(return_value=(self.page, True))), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch.object(self.page, 'close', AsyncMock()):
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            self.assertEqual(await apply_review(review_config(0.95), Mock(), 'unused.json', directory), 'saved')
        self.assertEqual(await self.page.locator('input:checked').count(), 0)
        result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['skipped_low_confidence'], [1, 2, 3])

    async def test_apply_selected_questions_preserves_other_choices_and_manual_low_answer(self):
        await self.page.locator('input[value="22"]').evaluate('e=>e.checked=true')
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
        write_json(directory / 'review.json', [{'question': q.to_dict(),
            'status': 'manual' if q.number == 1 else 'answered',
            'answer': {'option_ids': ids, 'confidence': 0 if q.number == 1 else 0.9, 'explanation': 'fixture'}}
            for q, ids in zip(questions, [['11'], ['21'], ['31', '33']])])
        session = SimpleNamespace(page=self.page, context=self.page.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.open_authenticated_course', AsyncMock()), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(return_value=(self.page, True))), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch.object(self.page, 'close', AsyncMock()):
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            status = await apply_review(review_config(), Mock(), 'unused.json', directory,
                question_ids=[questions[0].question_id, questions[2].question_id])
            self.assertEqual(status, 'saved')
            self.assertEqual(await self.page.locator('input:checked').evaluate_all('els=>els.map(e=>e.value)'), ['11', '22', '33', '31'])
            records = json.loads((directory / 'review.json').read_text(encoding='utf-8'))
            records[2]['answer']['confidence'] = 0.1
            write_json(directory / 'review.json', records)
            await self.page.evaluate('window.forceUnanswered=true')
            status = await apply_review(review_config(), Mock(), 'unused.json', directory,
                auto_submit=True, question_ids=[questions[0].question_id])
            self.assertEqual(status, 'submitted')
            self.assertEqual(await self.page.locator('input:checked').evaluate_all('els=>els.map(e=>e.value)'), ['11', '22'])
            result = json.loads((directory / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(result['skipped_low_confidence'], [3])

    async def test_low_confidence_existing_checkbox_choices_are_cleared(self):
        questions = await extract_questions(self.page, self.directory.name)
        await fill_answer(self.page, questions[2], Answer(['31', '33'], 0.9, 'fixture'))
        async def answer(q, **kwargs):
            return Answer({1: ['11'], 2: ['21'], 3: ['31', '33']}[q.number],
                          0.1 if q.number == 3 else 0.9, '')
        solver = SimpleNamespace(settings=LLMSettings(model='fixture-model', request_interval=0), answer=answer)
        self.assertEqual(await solve_exam(self.page, self.directory.name, solver, auto_submit=True, overwrite=True), 'submitted')
        self.assertEqual(await self.page.locator('input:checked').evaluate_all('els=>els.map(e=>e.value)'), ['11', '21'])

    async def test_unclearable_existing_radio_stops_submission(self):
        questions = await extract_questions(self.page, self.directory.name)
        await fill_answer(self.page, questions[0], Answer(['11'], 0.9, 'fixture'))
        with self.assertRaisesRegex(LLMError, '无法清空'):
            await fill_answer(self.page, questions[0], Answer([], 0, ''), allow_empty=True)
        self.assertIsNone(await self.page.evaluate('lastFinish'))

    async def test_review_verifies_all_questions_before_any_fill(self):
        questions = await extract_questions(self.page, self.directory.name)
        directory = Path(self.directory.name)
        write_json(directory / 'context.json', {'url': 'https://chapter.test/', 'index': 0})
        write_json(directory / 'review.json', [{'question': q.to_dict(), 'status': 'manual',
            'meets_confidence': True, 'answer': {'option_ids': ids, 'confidence': 0, 'explanation': 'manual'}}
            for q, ids in zip(questions, [['11'], ['21'], ['31', '33']])])
        fixture = (Path(__file__).parent / 'fixtures' / 'chapter_test.html').read_text(encoding='utf-8')
        fixture = fixture.replace("['22','错']", "['22','changed']").replace('<body>', '<body><div class="test-box"></div>')
        await self.page.unroute('https://chapter.test/**')
        await self.page.route('https://chapter.test/**', lambda route: route.fulfill(content_type='text/html', body=fixture))
        session = SimpleNamespace(page=self.page, context=self.page.context, close=AsyncMock())
        with patch('modules.chapter_tests.async_playwright') as pw, \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.open_chapter_test', AsyncMock(return_value=(self.page, True))), \
                patch('modules.chapter_tests.load_cookies', return_value=None), \
                patch('modules.chapter_tests.save_cookies'), \
                patch.object(self.page, 'close', AsyncMock()):
            pw.return_value.__aenter__ = AsyncMock(return_value=Mock())
            pw.return_value.__aexit__ = AsyncMock(return_value=False)
            with self.assertRaisesRegex(LLMError, '发生变化'):
                await apply_review(review_config(), Mock(), 'unused.json', directory)
        self.assertEqual(await self.page.locator('input:checked').count(), 0)
        errors = list((directory / 'apply-errors').glob('*/diagnostic.json'))
        self.assertEqual(len(errors), 1)
        self.assertEqual(json.loads(errors[0].read_text(encoding='utf-8'))['stage'], '核对全部题目')
