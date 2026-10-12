import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace

from modules.course_playback import learn_lesson, review_lesson, wait_for_video_ready
from modules.lesson_navigation import WISDOM_CATALOG


class _Config:
    limitMaxTime = 0


class _Logger:
    def warn(self, *_args, **_kwargs):
        pass

    def debug(self, *_args, **_kwargs):
        pass

    def event(self, *_args, **_kwargs):
        pass

    def debug_throttled(self, *_args, **_kwargs):
        pass


class CoursePlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_video_wait_ignores_previous_lesson_and_unloaded_new_source(self):
        old = dict(elementId=1, generation=0, source="old", duration=960.2, readyState=4)
        loading = dict(old, source="new", readyState=0)
        loaded = dict(loading, duration=496.6, readyState=1)
        page = SimpleNamespace(is_closed=lambda: False)
        with patch("modules.course_playback.get_video_snapshot", AsyncMock(side_effect=[old, loading, loaded])), \
                patch("modules.course_playback.has_visible_verification", AsyncMock(return_value=False)), \
                patch("modules.course_playback.has_visible_element", AsyncMock(return_value=False)), \
                patch("modules.course_playback.asyncio.sleep", AsyncMock()):
            self.assertEqual(await wait_for_video_ready(page, old), (496.6, 0.0))

    async def test_review_refreshes_stale_duration_and_finishes_shorter_video(self):
        page = SimpleNamespace()
        with patch("modules.course_playback._wait_for_duration", AsyncMock(return_value=(960.2, 0.0))), \
                patch("modules.course_playback.get_video_attr", AsyncMock(side_effect=[496.6, 496.6])), \
                patch("modules.course_playback.run_on", AsyncMock()), \
                patch("modules.course_playback.has_visible_verification", AsyncMock(return_value=False)), \
                patch("modules.course_playback.has_visible_element", AsyncMock(return_value=False)):
            self.assertEqual(await review_lesson(page, 0, 0, _Config(), _Logger()), (0.0, True, False))

    async def test_learning_retries_end_using_actual_duration_not_previous_duration(self):
        page = SimpleNamespace(wait_for_timeout=AsyncMock(), evaluate=AsyncMock())
        logger = Mock()
        with patch("modules.course_playback._wait_for_duration", AsyncMock(return_value=(960.2, 0.0))), \
                patch("modules.course_playback.get_video_attr", AsyncMock(side_effect=[496.6, 0, 496.6, 496.6])), \
                patch("modules.course_playback.lesson_progress", AsyncMock(side_effect=[56, 56, 56, 100])), \
                patch("modules.course_playback.wait_for_lesson_completion", AsyncMock(return_value=True)), \
                patch("modules.course_playback.has_visible_verification", AsyncMock(return_value=False)), \
                patch("modules.course_playback.has_visible_element", AsyncMock(return_value=False)), \
                patch("modules.course_playback.asyncio.sleep", AsyncMock()), \
                patch("modules.course_playback.show_course_progress"):
            result = await learn_lesson(page, 0, 0, object(), WISDOM_CATALOG, _Config(), logger)
        self.assertEqual(result, (0.0, True, False))
        page.evaluate.assert_awaited_once()
        self.assertAlmostEqual(page.evaluate.await_args.args[1], 496.6 * 0.56)

    @patch(
        "modules.course_playback._wait_for_duration",
        new_callable=AsyncMock,
        return_value=(None, 7.5),
    )
    async def test_invalid_metadata_fails_learning_with_three_part_result(self, _wait):
        result = await learn_lesson(
            object(), 0, 0, object(), WISDOM_CATALOG, _Config(), _Logger()
        )
        self.assertEqual(result, (7.5, False, False))

    @patch(
        "modules.course_playback._wait_for_duration",
        new_callable=AsyncMock,
        return_value=(None, 7.5),
    )
    async def test_invalid_metadata_fails_review_with_three_part_result(self, _wait):
        result = await review_lesson(object(), 0, 0, _Config(), _Logger())
        self.assertEqual(result, (7.5, False, False))


if __name__ == "__main__":
    unittest.main()
