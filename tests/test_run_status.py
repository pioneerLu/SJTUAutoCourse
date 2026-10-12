from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import AutoCourse
from modules.course_runner import CourseOutcome
from modules.lesson_navigation import FUSION_CATALOG


class RunStatusTests(unittest.IsolatedAsyncioTestCase):
    def test_optional_captcha_failure_falls_back_to_manual_login(self):
        config = SimpleNamespace(enableAutoCaptcha=True)
        with patch.object(AutoCourse, 'logger', Mock(), create=True) as logger, \
                patch('AutoCourse.installer.start', side_effect=RuntimeError('fixture dependency mismatch')):
            self.assertEqual(AutoCourse.prepare_captcha_modules(config), [])
        self.assertIn('手动完成验证', logger.warn.call_args.args[0])

    async def test_failed_course_is_not_reported_as_all_complete(self):
        page = Mock(url="https://wisdom-mooc.zhihuishu.com/study/index",
                    goto=AsyncMock(), wait_for_timeout=AsyncMock())
        context = Mock(browser=Mock(close=AsyncMock()))
        config = SimpleNamespace(enableAutoCaptcha=False, enableHideWindow=False,
                                 course_urls=[page.url], showDonateCode=False)
        logger = Mock()
        patches = {
            "init_page": AsyncMock(return_value=(page, context)),
            "ensure_login": AsyncMock(return_value=True),
            "persist_login_cookies": AsyncMock(), "load_cookies": Mock(return_value=None),
            "wait_for_verify": AsyncMock(), "video_optimize": AsyncMock(),
            "skip_questions": AsyncMock(), "play_video": AsyncMock(),
            "task_monitor": AsyncMock(), "optimize_page": AsyncMock(),
            "detect_catalog_after_verification": AsyncMock(return_value=FUSION_CATALOG),
            "run_course": AsyncMock(return_value=CourseOutcome.FAILED),
        }
        with patch.object(AutoCourse, "logger", logger, create=True), \
                patch.object(AutoCourse, "async_playwright") as playwright, \
                patch.multiple(AutoCourse, **patches):
            playwright.return_value.__aenter__ = AsyncMock(return_value=Mock())
            playwright.return_value.__aexit__ = AsyncMock(return_value=False)
            self.assertFalse(await AutoCourse.main(config))
        results = [call.kwargs for call in logger.event.call_args_list if call.args[0] == "运行结果"]
        self.assertEqual(results, [{"正常退出": False, "全部完成": False}])
