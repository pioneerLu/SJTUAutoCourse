import asyncio
from time import monotonic
from urllib.parse import urlparse

from playwright.async_api import Page, TimeoutError, Error


LOGIN_HOSTS = {"login.zhihuishu.com", "passport.zhihuishu.com"}
LOGIN_USERNAME_SELECTOR = "#lUsername, input[name='mobile']"
LOGIN_PASSWORD_SELECTOR = "#lPassword, input[type='password']"
LOGIN_SUBMIT_SELECTOR = ".wall-sub-btn, .btn-block__grandient_login, button[type='submit']"


class LoginFailure(RuntimeError):
    def __init__(self, message, page=None):
        super().__init__(message)
        self.page = page
        self.chapter_stage = '章节登录'


class LoginTimeout(TimeoutError):
    def __init__(self, message, page=None):
        super().__init__(message)
        self.page = page
        self.chapter_stage = '章节登录'


async def login_page_error(page):
    if not hasattr(page, 'evaluate') or page.is_closed():
        return ''
    try:
        code = await asyncio.wait_for(page.evaluate("""() => {
            const root=document.querySelector('#main-frame-error');
            return root?.querySelector('.error-code')?.textContent.trim() || '';
        }"""), timeout=2)
        if code:
            return f'登录页面显示 {code}，登录回跳失败。请重新打开课程入口或检查登录服务。'
    except (Error, asyncio.TimeoutError):
        pass
    return ''


def is_login_page(url: str) -> bool:
    """判断 URL 是否仍处于智慧树登录流程。"""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    return (hostname in LOGIN_HOSTS
            or (hostname == 'cloudapi.polymas.com' and parsed.path.startswith('/oauth/login/'))
            or (hostname == 'studentexam-api.zhihuishu.com' and '/gologin/' in parsed.path))


async def wait_for_login_complete(
    page: Page, timeout: float = 24 * 3600 * 1000, *, page_candidates=None, notify=None,
    captcha_modules=None
) -> Page:
    """等待完成整条 SSO 跳转；浏览器错误页不代表已登录。"""
    def destination(url):
        hostname = urlparse(url).hostname or ''
        return (hostname == 'zhihuishu.com' or hostname.endswith('.zhihuishu.com')) and not is_login_page(url)

    watched, failures, captcha_tasks = {}, {}, {}

    async def verify_slider(candidate):
        from modules.slider import slider_verify
        try:
            passed = await slider_verify(candidate, captcha_modules)
            if notify and passed:
                notify('登录滑块验证已通过，正在等待登录跳转…')
            elif notify and not candidate.is_closed() and is_login_page(candidate.url):
                notify('自动滑块尝试未通过，请在浏览器中手动完成验证；完成后程序会继续。')
        except Exception:
            # 自动验证失败不终止登录，保留手动处理的机会；不输出认证 URL。
            if notify and not candidate.is_closed() and is_login_page(candidate.url):
                notify('自动滑块处理失败，请在浏览器中手动完成验证；完成后程序会继续。')

    def watch(candidate):
        if id(candidate) in watched or not hasattr(candidate, 'on'):
            return
        def response_received(response):
            try:
                if response.status >= 400 and response.request.is_navigation_request() and response.request.frame == candidate.main_frame:
                    failures[id(candidate)] = f'登录服务返回 HTTP {response.status}，登录回跳失败。请重新打开课程入口或检查登录服务。'
            except Error:
                pass
        candidate.on('response', response_received)
        watched[id(candidate)] = (candidate, response_received)
    deadline = monotonic() + timeout / 1000
    last_notice, closed_at = monotonic(), None
    try:
        while monotonic() < deadline:
            candidates = [page, *(page_candidates() if page_candidates else [])]
            live = []
            for candidate in candidates:
                if not candidate.is_closed() and all(candidate is not existing for existing in live):
                    watch(candidate)
                    live.append(candidate)
                    if captcha_modules and is_login_page(candidate.url) and id(candidate) not in captcha_tasks:
                        if notify:
                            notify('自动滑块已启用，正在等待登录验证码…')
                        captcha_tasks[id(candidate)] = asyncio.create_task(verify_slider(candidate))
            # 登录可能打开新的目标标签页；只检查调用方明确提供的所属页面。
            errors = []
            for candidate in reversed(live):
                problem = failures.get(id(candidate)) or await login_page_error(candidate)
                if problem:
                    errors.append((candidate, problem))
                elif destination(candidate.url):
                    return candidate
            if errors:
                candidate, problem = errors[0]
                raise LoginFailure(problem, candidate)
            if not live:
                if closed_at is None:
                    closed_at = monotonic()
                if not page_candidates or monotonic() - closed_at >= 2:
                    raise LoginFailure('登录窗口已关闭，请重新运行。', page)
            else:
                closed_at = None
            if notify and monotonic() - last_notice >= 30:
                notify('登录仍未完成，请检查浏览器中的验证码或登录服务状态；完成后程序会继续。')
                last_notice = monotonic()
            await asyncio.sleep(0.25)
        raise LoginTimeout('登录等待超时，请检查浏览器中的验证码或登录网关错误。', page)
    finally:
        for task in captcha_tasks.values():
            if not task.done():
                task.cancel()
        if captcha_tasks:
            await asyncio.gather(*captcha_tasks.values(), return_exceptions=True)
        for candidate, handler in watched.values():
            candidate.remove_listener('response', handler)


async def accept_login_terms(page: Page) -> None:
    """新版登录页存在协议复选框时自动勾选。"""
    checkbox = page.locator("input.el-checkbox__original").first
    if not await checkbox.count():
        return

    if not await checkbox.is_checked():
        # Element UI 的原始 checkbox 通常被隐藏，不能使用 Playwright check。
        await checkbox.evaluate("element => element.click()")


async def complete_standard_login(page, username='', password='', *, notify=None,
                                  timeout=10 * 60 * 1000, page_candidates=None, captcha_modules=None):
    """复用配置账号；提供可选依赖时自动尝试登录滑块，失败后等待手动验证。"""
    if not is_login_page(page.url):
        return await wait_for_login_complete(page, timeout=timeout, page_candidates=page_candidates,
                                            notify=notify, captcha_modules=captcha_modules)
    problem = await login_page_error(page)
    if problem:
        raise LoginFailure(problem, page)
    if username and password:
        try:
            user = await page.wait_for_selector(LOGIN_USERNAME_SELECTOR, state='visible', timeout=30000)
            secret = await page.wait_for_selector(LOGIN_PASSWORD_SELECTOR, state='visible', timeout=30000)
            await user.fill(username)
            await secret.fill(password)
            await accept_login_terms(page)
            submit = await page.wait_for_selector(LOGIN_SUBMIT_SELECTOR, state='visible', timeout=30000)
            await submit.click()
            if notify:
                notify('已使用配置账号登录；自动滑块会尝试验证，失败时请在浏览器中完成验证。'
                       if captcha_modules else '已使用配置账号登录；如出现验证码，请在浏览器中完成验证。')
        except TimeoutError:
            if notify:
                notify('未找到自动登录控件，请在浏览器中手动完成登录。')
        except Error:
            problem = await login_page_error(page)
            raise LoginFailure(problem or '登录控件操作或登录跳转失败，请检查浏览器中的登录服务状态。', page) from None
    elif notify:
        notify('请在浏览器的常规登录页手动完成登录。')
    return await wait_for_login_complete(page, timeout=timeout, page_candidates=page_candidates,
                                        notify=notify, captcha_modules=captcha_modules)
