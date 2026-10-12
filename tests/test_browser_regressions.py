import base64
import io
import os
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import Mock, patch
import asyncio

from playwright.async_api import async_playwright

from modules.course_playback import get_video_snapshot, wait_for_video_ready
from modules.lesson_navigation import FUSION_CATALOG, get_lesson_title, lesson_progress
from modules.utils import get_filtered_class
from modules.course_runner import CourseOutcome, run_course


def audio_source(seconds):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 8000 * seconds)
    return "data:audio/wav;base64," + base64.b64encode(buffer.getvalue()).decode()


@unittest.skipUnless(os.environ.get("AUTOCOURSE_BROWSER_TESTS") == "1", "requires local Chrome")
class BrowserRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(channel="chrome", headless=True)
        self.page = await self.browser.new_page()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_mixed_directory_uses_leaf_progress_and_titles(self):
        await self.page.set_content('''
            <div class="chapter-item"><div class="item-name">Intro</div>
                <div class="el-progress" aria-valuenow="56"></div></div>
            <div class="chapter-item"><div class="item-name">Group</div>
                <div class="el-progress" aria-valuenow="100"></div>
                <div class="chapter-content-second"><span class="item-num">1.1</span>Child A
                    <div class="el-progress" aria-valuenow="20"></div></div>
                <div class="chapter-content-second">Child B<span class="finish-icon"></span></div>
            </div><div class="test-box">Test</div>
        ''')
        all_lessons = await get_filtered_class(self.page, FUSION_CATALOG, include_all=True)
        self.assertEqual(len(all_lessons), 3)
        pending = await get_filtered_class(self.page, FUSION_CATALOG)
        self.assertEqual([await get_lesson_title(self.page, x, FUSION_CATALOG) for x in pending],
                         ["Intro", "Child A"])
        self.assertEqual([await lesson_progress(x, FUSION_CATALOG) for x in pending], [56, 20])

    async def test_new_media_metadata_is_waited_for_before_playback(self):
        old_source, new_source = audio_source(2), audio_source(1)
        await self.page.set_content('<video preload="metadata"></video>')
        await self.page.locator('video').evaluate('(v,src)=>v.src=src', old_source)
        await self.page.wait_for_function('document.querySelector("video").readyState >= 1')
        old = await get_video_snapshot(self.page)
        self.assertAlmostEqual(old["duration"], 2, places=2)
        await self.page.evaluate('(src)=>setTimeout(()=>document.querySelector("video").src=src,200)', new_source)
        duration, _ = await wait_for_video_ready(self.page, old)
        self.assertAlmostEqual(duration, 1, places=2)
        self.assertTrue(await self.page.locator('video').evaluate('(v)=>v.paused'))

    async def test_course_runner_waits_for_delayed_media_after_active_class_changes(self):
        await self.page.set_content('''<video preload="metadata"></video>
            <div class="chapter-item current"><span class="item-name">First</span></div>
            <div class="chapter-item"><span class="item-name">Second</span></div>''')
        sources = [audio_source(2), audio_source(1)]
        await self.page.evaluate('''sources => {
            const video = document.querySelector('video');
            const lessons = Array.from(document.querySelectorAll('.chapter-item'));
            video.src = sources[0];
            lessons.forEach((lesson, index) => lesson.addEventListener('click', () => {
                lessons.forEach(el => el.classList.remove('current'));
                lesson.classList.add('current');
                if (index) setTimeout(() => video.src = sources[index], 1200);
            }));
        }''', sources)
        await self.page.wait_for_function('document.querySelector("video").readyState >= 1')
        seen = []
        async def learn(page, *_args):
            seen.append((await get_video_snapshot(page))["duration"])
            return 0, True, False
        enabled = asyncio.Event()
        with patch("modules.course_runner.learn_lesson", side_effect=learn):
            result = await run_course(self.page, FUSION_CATALOG, SimpleNamespace(limitMaxTime=0), Mock(), enabled)
        self.assertIs(result, CourseOutcome.COMPLETED)
        self.assertEqual(seen, [2, 1])
