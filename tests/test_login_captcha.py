import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import TimeoutError

from modules.chapter_tests import ensure_chapter_login
from modules.login import complete_standard_login, wait_for_login_complete


def login_page():
    return SimpleNamespace(url='https://passport.zhihuishu.com/login', is_closed=lambda: False)


class LoginCaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def test_slider_starts_and_login_completes(self):
        page = login_page()
        modules = [object(), object()]
        notices = []
        async def solve(candidate, dependencies):
            self.assertIs(candidate, page)
            self.assertIs(dependencies, modules)
            candidate.url = 'https://www.zhihuishu.com/'
            return True
        with patch('modules.slider.slider_verify', AsyncMock(side_effect=solve)) as slider:
            destination = await wait_for_login_complete(page, timeout=1000, captcha_modules=modules, notify=notices.append)
        self.assertIs(destination, page)
        slider.assert_awaited_once()
        self.assertTrue(any('滑块验证已通过' in message for message in notices))

    async def test_no_slider_when_disabled_or_already_logged_in(self):
        page = login_page()
        with patch('modules.slider.slider_verify', AsyncMock()) as slider:
            with self.assertRaises(TimeoutError):
                await wait_for_login_complete(page, timeout=50, captcha_modules=[])
            page.url = 'https://www.zhihuishu.com/'
            await wait_for_login_complete(page, timeout=1000, captcha_modules=[object(), object()])
        slider.assert_not_called()

    async def test_slider_error_falls_back_to_manual_without_repeated_attempts(self):
        page = login_page()
        notices = []
        with patch('modules.slider.slider_verify', AsyncMock(side_effect=RuntimeError('fixture failure'))) as slider:
            task = asyncio.create_task(wait_for_login_complete(page, timeout=2000,
                captcha_modules=[object(), object()], notify=notices.append))
            await asyncio.sleep(0.1)
            self.assertFalse(task.done())
            self.assertTrue(any('手动完成验证' in message for message in notices))
            await asyncio.sleep(0.3)
            slider.assert_awaited_once()
            page.url = 'https://www.zhihuishu.com/'
            self.assertIs(await task, page)

    async def test_pending_slider_is_cleaned_up_on_success_timeout_and_stop(self):
        for outcome in ('success', 'timeout', 'stop'):
            with self.subTest(outcome=outcome):
                page = login_page()
                started, cleaned = asyncio.Event(), asyncio.Event()
                async def pending(*args):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cleaned.set()
                with patch('modules.slider.slider_verify', AsyncMock(side_effect=pending)):
                    task = asyncio.create_task(wait_for_login_complete(page, timeout=500,
                        captcha_modules=[object(), object()]))
                    await asyncio.wait_for(started.wait(), 1)
                    if outcome == 'success':
                        page.url = 'https://www.zhihuishu.com/'
                        self.assertIs(await task, page)
                    elif outcome == 'timeout':
                        with self.assertRaises(TimeoutError):
                            await task
                    else:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                self.assertTrue(cleaned.is_set())

    async def test_owned_login_popup_gets_a_slider_task(self):
        page, popup = login_page(), login_page()
        popup.url = 'https://login.zhihuishu.com/'
        cleaned = asyncio.Event()
        async def solve(candidate, dependencies):
            if candidate is popup:
                popup.url = 'https://www.zhihuishu.com/'
                return True
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        with patch('modules.slider.slider_verify', AsyncMock(side_effect=solve)) as slider:
            destination = await wait_for_login_complete(page, timeout=1000,
                captcha_modules=[object(), object()], page_candidates=lambda: [popup])
        self.assertIs(destination, popup)
        self.assertEqual(slider.await_count, 2)
        self.assertTrue(cleaned.is_set())

    async def test_standard_login_forwards_dependencies_after_filling_account(self):
        page = login_page()
        user, secret, submit = Mock(fill=AsyncMock()), Mock(fill=AsyncMock()), Mock(click=AsyncMock())
        page.wait_for_selector = AsyncMock(side_effect=[user, secret, submit])
        modules = [object(), object()]
        with patch('modules.login.accept_login_terms', AsyncMock()), \
                patch('modules.login.wait_for_login_complete', AsyncMock(return_value=page)) as wait:
            await complete_standard_login(page, 'fixture-user', 'fixture-password', captcha_modules=modules)
        user.fill.assert_awaited_once_with('fixture-user')
        secret.fill.assert_awaited_once_with('fixture-password')
        submit.click.assert_awaited_once()
        self.assertIs(wait.call_args.kwargs['captcha_modules'], modules)


class ChapterCaptchaTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, enabled, *, logged_in=False, dependency_error=None):
        page = login_page()
        page.goto = AsyncMock(return_value=None)
        context = SimpleNamespace(pages=[page], clear_cookies=AsyncMock(), cookies=AsyncMock(return_value=[]))
        session = SimpleNamespace(page=page, context=context, attached=False)
        config = SimpleNamespace(login_url=page.url, username='fixture', password='fixture', enableAutoCaptcha=enabled)
        modules = [object(), object()]
        notices = []
        with tempfile.TemporaryDirectory() as directory, \
                patch('modules.chapter_tests.wait_for_login_complete', AsyncMock(
                    return_value=page, side_effect=None if logged_in else TimeoutError('expired'))), \
                patch('modules.installer.start', Mock(return_value=modules, side_effect=dependency_error)) as prepare, \
                patch('modules.chapter_tests.complete_standard_login', AsyncMock(return_value=page)) as complete:
            await ensure_chapter_login(session, config, Mock(), Path(directory) / 'cookies.json', callback=notices.append)
        return prepare, complete, modules, notices

    async def test_reauth_uses_configured_slider_and_existing_dependency_loader(self):
        prepare, complete, modules, _ = await self.exercise(True)
        prepare.assert_called_once()
        self.assertIs(complete.call_args.kwargs['captcha_modules'], modules)

    async def test_disabled_or_valid_login_does_not_prepare_dependencies(self):
        prepare, complete, _, _ = await self.exercise(False)
        prepare.assert_not_called()
        self.assertEqual(complete.call_args.kwargs['captcha_modules'], [])
        prepare, complete, _, _ = await self.exercise(True, logged_in=True)
        prepare.assert_not_called()
        complete.assert_not_called()

    async def test_dependency_failure_keeps_manual_login_available(self):
        prepare, complete, _, notices = await self.exercise(True, dependency_error=RuntimeError('missing dependency'))
        prepare.assert_called_once()
        self.assertEqual(complete.call_args.kwargs['captcha_modules'], [])
        self.assertTrue(any('uv sync' in event['message'] for event in notices))


if __name__ == '__main__':
    unittest.main()
