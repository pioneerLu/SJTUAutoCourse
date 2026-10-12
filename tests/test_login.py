import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from modules.login import is_login_page, wait_for_login_complete
from playwright.async_api import TimeoutError
from modules.slider import image_to_display_point
from AutoCourse import init_page, ensure_login
from modules.diagnostics import check_browser


class LoginUrlTests(unittest.TestCase):
    def test_recognizes_current_and_legacy_login_hosts(self):
        self.assertTrue(is_login_page("https://login.zhihuishu.com/?origin=zhs"))
        self.assertTrue(is_login_page("https://passport.zhihuishu.com/login"))

    def test_rejects_authenticated_destination_hosts(self):
        self.assertFalse(is_login_page("https://www.zhihuishu.com/"))
        self.assertFalse(is_login_page("https://onlineweb.zhihuishu.com/"))

    def test_recognizes_sso_intermediate_routes_without_matching_unrelated_api(self):
        self.assertTrue(is_login_page('https://cloudapi.polymas.com/oauth/login/ssoLogin'))
        self.assertTrue(is_login_page('https://studentexam-api.zhihuishu.com/studentExam/gateway/f/v1/gologin/login'))
        self.assertFalse(is_login_page('https://cloudapi.polymas.com/course/info'))
        self.assertFalse(is_login_page('https://studentexam-api.zhihuishu.com/answer/temporarySave'))

    def test_does_not_match_login_text_outside_the_hostname(self):
        self.assertFalse(is_login_page("https://example.com/login.zhihuishu.com"))

    def test_converts_opencv_point_to_display_coordinates(self):
        self.assertEqual(
            image_to_display_point((240, 120), 480, 240, 400, 200),
            (200, 100),
        )

    def test_browser_starts_maximized(self):
        args = init_page.__code__.co_consts
        self.assertIn("--start-maximized", args)
        self.assertIn("--window-position=0,0", args)
        self.assertIn(("viewport",), args)


class BrowserLoginCheckTests(unittest.IsolatedAsyncioTestCase):
    async def test_sso_intermediate_and_chrome_error_are_not_login_success(self):
        page = SimpleNamespace(url='https://cloudapi.polymas.com/oauth/login/ssoLogin',
                               is_closed=lambda: False)
        task = asyncio.create_task(wait_for_login_complete(page, timeout=2000))
        await asyncio.sleep(0.1)
        self.assertFalse(task.done())
        page.url = 'https://www.zhihuishu.com/'
        await task
        page.url = 'chrome-error://chromewebdata/'
        with self.assertRaises(TimeoutError):
            await wait_for_login_complete(page, timeout=100)

    async def test_failed_saved_login_clears_browser_cookies_before_reauth(self):
        page = SimpleNamespace(url='https://passport.zhihuishu.com/login', goto=AsyncMock())
        context = SimpleNamespace(clear_cookies=AsyncMock())
        config = SimpleNamespace(login_url=page.url, username='fixture', password='fixture')
        async def fresh_login(*args):
            context.clear_cookies.assert_awaited_once()
        with patch('AutoCourse.wait_for_login_complete', AsyncMock(side_effect=TimeoutError('expired'))), \
                patch('AutoCourse.auto_login', AsyncMock(side_effect=fresh_login)) as login, \
                patch('AutoCourse.clear_cookies'), patch('AutoCourse.remember_login_cookies'), \
                patch('AutoCourse.logger', Mock(), create=True):
            self.assertFalse(await ensure_login(context, page, [{'fixture': True}], config))
        login.assert_awaited_once()

    async def test_current_login_redirect_is_reported_as_unauthenticated(self):
        page = SimpleNamespace(
            url="https://login.zhihuishu.com/?service=course",
            goto=AsyncMock(),
            wait_for_timeout=AsyncMock(),
            title=AsyncMock(return_value="智慧树"),
        )
        session = SimpleNamespace(page=page, close=AsyncMock())
        config = SimpleNamespace(attach_existing_chrome=False)
        logger = Mock()
        with patch("modules.diagnostics.async_playwright") as playwright, \
                patch("modules.diagnostics.create_browser_session", AsyncMock(return_value=session)), \
                patch("modules.diagnostics.load_cookies", return_value=None):
            playwright.return_value.__aenter__ = AsyncMock(return_value=Mock())
            playwright.return_value.__aexit__ = AsyncMock(return_value=False)
            self.assertEqual(await check_browser(config, logger, "unused.json"), 1)
        session.close.assert_awaited_once()
        logger.warn.assert_called_once()
        page.title.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
