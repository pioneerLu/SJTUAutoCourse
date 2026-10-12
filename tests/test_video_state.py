import math
import unittest

from modules.video_state import (
    has_valid_duration,
    tail_retry_time,
    time_for_percent,
    video_at_end,
    video_metadata_ready,
)


class VideoStateTests(unittest.TestCase):
    def test_valid_old_metadata_is_not_accepted_after_lesson_switch(self):
        old = dict(elementId=1, generation=0, source="old.mp4", duration=960.2, readyState=4)
        self.assertFalse(video_metadata_ready(old, old))
        loading = dict(old, source="new.mp4", readyState=0)
        self.assertFalse(video_metadata_ready(loading, old))
        loaded = dict(loading, duration=496.6, readyState=1)
        self.assertTrue(video_metadata_ready(loaded, old))

    def test_same_source_reload_and_replaced_video_are_detected(self):
        old = dict(elementId=1, generation=0, source="same.mp4", duration=10, readyState=4)
        self.assertTrue(video_metadata_ready(dict(old, generation=1), old))
        self.assertTrue(video_metadata_ready(dict(old, elementId=2), old))
        self.assertTrue(video_metadata_ready(old))

    def test_video_end_uses_small_tolerance(self):
        self.assertTrue(video_at_end(59.4, 60.0))
        self.assertFalse(video_at_end(58.0, 60.0))

    def test_invalid_duration_is_rejected(self):
        for value in (None, 0, -1, math.nan, math.inf):
            with self.subTest(value=value):
                self.assertFalse(has_valid_duration(value))

    def test_platform_percent_maps_back_to_video_time(self):
        self.assertEqual(time_for_percent(300, 25), 75)
        self.assertEqual(time_for_percent(300, 120), 300)
        self.assertEqual(time_for_percent(math.nan, 25), 0)

    def test_tail_retry_never_goes_negative(self):
        self.assertEqual(tail_retry_time(60), 55)
        self.assertEqual(tail_retry_time(3), 0)


if __name__ == "__main__":
    unittest.main()
