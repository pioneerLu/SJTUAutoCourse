import unittest
from unittest.mock import AsyncMock, Mock, patch

from modules.fusion_adapter import FusionAdapter


class FusionExerciseTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_options_are_not_reported_as_submitted(self):
        page = Mock(evaluate=AsyncMock(return_value=False))
        with patch.object(FusionAdapter, "ai_exercise_visible", AsyncMock(return_value=True)), \
                patch.object(FusionAdapter, "resume_video", AsyncMock()) as resume, \
                patch("modules.fusion_adapter.logger") as logger:
            self.assertFalse(await FusionAdapter.skip_ai_exercise(page))
        page.evaluate.assert_awaited_once()
        resume.assert_not_awaited()
        self.assertFalse(any("已自动提交关闭" in str(call) for call in logger.info.call_args_list))

    async def test_submission_with_visible_dialog_is_not_reported_as_closed(self):
        page = Mock(evaluate=AsyncMock(side_effect=[True, True]), wait_for_timeout=AsyncMock())
        with patch.object(FusionAdapter, "ai_exercise_visible", AsyncMock(return_value=True)), \
                patch.object(FusionAdapter, "resume_video", AsyncMock()) as resume, \
                patch("modules.fusion_adapter.logger") as logger:
            self.assertFalse(await FusionAdapter.skip_ai_exercise(page))
        resume.assert_not_awaited()
        self.assertFalse(any("已自动提交关闭" in str(call) for call in logger.info.call_args_list))

    async def test_closed_dialog_resumes_playback(self):
        page = Mock(evaluate=AsyncMock(side_effect=[True, True]), wait_for_timeout=AsyncMock())
        with patch.object(FusionAdapter, "ai_exercise_visible", AsyncMock(side_effect=[True, False])), \
                patch.object(FusionAdapter, "resume_video", AsyncMock()) as resume, \
                patch("modules.fusion_adapter.logger"):
            self.assertTrue(await FusionAdapter.skip_ai_exercise(page))
        resume.assert_awaited_once_with(page)
