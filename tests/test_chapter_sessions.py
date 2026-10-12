import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.chapter_tests import ChapterReviewBrowser


class ChapterSessionTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self):
        browser = Mock(is_connected=Mock(return_value=True))
        page = Mock(is_closed=Mock(return_value=False))
        session = SimpleNamespace(browser=browser, page=page,
            context=Mock(new_page=AsyncMock()), close=AsyncMock())
        scope = Mock(start=AsyncMock(), close=AsyncMock())
        manager = Mock(__aenter__=AsyncMock(return_value=Mock()), __aexit__=AsyncMock(return_value=False))
        return session, scope, manager

    async def test_lazy_context_does_not_launch_without_valid_targets(self):
        with patch('modules.chapter_tests.async_playwright') as playwright:
            async with ChapterReviewBrowser(Mock(), Mock(), 'unused'):
                pass
        playwright.assert_not_called()

    async def test_repeated_acquire_reuses_session_and_closes_once(self):
        session, scope, manager = self.fixture()
        with patch('modules.chapter_tests.async_playwright', return_value=manager), \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)) as create, \
                patch('modules.chapter_tests.ChapterBrowserScope', return_value=scope), \
                patch('modules.chapter_tests.load_cookies', return_value=[]):
            async with ChapterReviewBrowser(Mock(), Mock(), 'unused') as runtime:
                self.assertEqual(await runtime.acquire(), (session, scope))
                self.assertEqual(await runtime.acquire(), (session, scope))
                session.close.assert_not_awaited()
        self.assertEqual(create.await_count, 1)
        self.assertEqual(scope.start.await_count, 1)
        self.assertEqual(scope.close.await_count, 1)
        self.assertEqual(session.close.await_count, 1)
        self.assertEqual(manager.__aexit__.await_count, 1)

    async def test_cancellation_closes_batch_resources(self):
        session, scope, manager = self.fixture()
        with patch('modules.chapter_tests.async_playwright', return_value=manager), \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)), \
                patch('modules.chapter_tests.ChapterBrowserScope', return_value=scope), \
                patch('modules.chapter_tests.load_cookies', return_value=[]):
            with self.assertRaises(asyncio.CancelledError):
                async with ChapterReviewBrowser(Mock(), Mock(), 'unused') as runtime:
                    await runtime.acquire()
                    raise asyncio.CancelledError()
        session.close.assert_awaited_once()
        scope.close.assert_awaited_once()
        manager.__aexit__.assert_awaited_once()

    async def test_disconnected_browser_restarts_for_next_chapter(self):
        first, first_scope, first_manager = self.fixture()
        second, second_scope, second_manager = self.fixture()
        with patch('modules.chapter_tests.async_playwright', side_effect=[first_manager, second_manager]), \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(side_effect=[first, second])) as create, \
                patch('modules.chapter_tests.ChapterBrowserScope', side_effect=[first_scope, second_scope]), \
                patch('modules.chapter_tests.load_cookies', return_value=[]):
            async with ChapterReviewBrowser(Mock(), Mock(), 'unused') as runtime:
                await runtime.acquire()
                first.browser.is_connected.return_value = False
                self.assertEqual(await runtime.acquire(), (second, second_scope))
                first.close.assert_awaited_once()
                second.close.assert_not_awaited()
        self.assertEqual(create.await_count, 2)
        second.close.assert_awaited_once()

    async def test_closed_course_tab_rebuilds_page_in_existing_context(self):
        session, first_scope, manager = self.fixture()
        next_scope = Mock(start=AsyncMock(), close=AsyncMock())
        original = session.page
        replacement = Mock(is_closed=Mock(return_value=False))
        session.context.new_page.return_value = replacement
        with patch('modules.chapter_tests.async_playwright', return_value=manager), \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(return_value=session)) as create, \
                patch('modules.chapter_tests.ChapterBrowserScope', side_effect=[first_scope, next_scope]), \
                patch('modules.chapter_tests.load_cookies', return_value=[]):
            async with ChapterReviewBrowser(Mock(), Mock(), 'unused') as runtime:
                await runtime.acquire()
                original.is_closed.return_value = True
                self.assertEqual(await runtime.acquire(), (session, next_scope))
                self.assertIs(session.page, replacement)
                session.close.assert_not_awaited()
        create.assert_awaited_once()
        first_scope.close.assert_awaited_once()
        next_scope.close.assert_awaited_once()
        session.close.assert_awaited_once()

    async def test_scope_start_failure_releases_resources_and_allows_retry(self):
        first, first_scope, first_manager = self.fixture()
        second, second_scope, second_manager = self.fixture()
        first_scope.start.side_effect = RuntimeError('fixture initialization failure')
        with patch('modules.chapter_tests.async_playwright', side_effect=[first_manager, second_manager]), \
                patch('modules.chapter_tests.create_browser_session', AsyncMock(side_effect=[first, second])), \
                patch('modules.chapter_tests.ChapterBrowserScope', side_effect=[first_scope, second_scope]), \
                patch('modules.chapter_tests.load_cookies', return_value=[]):
            async with ChapterReviewBrowser(Mock(), Mock(), 'unused') as runtime:
                with self.assertRaisesRegex(RuntimeError, 'initialization failure'):
                    await runtime.acquire()
                first.close.assert_awaited_once()
                self.assertEqual(await runtime.acquire(), (second, second_scope))
        second.close.assert_awaited_once()
