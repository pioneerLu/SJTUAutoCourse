import unittest
from unittest import mock

import AutoCourse


class _Logger:
    def __init__(self):
        self.events = []
        self.exceptions = []

    def event(self, name, interval=None, **fields):
        self.events.append((name, fields))

    def log_exception(self, msg, exc=None, shift=False):
        self.exceptions.append(msg)


class _Context:
    def __init__(self, cookies):
        self.cookies_payload = cookies

    async def cookies(self, urls):
        return self.cookies_payload


def _cookie(name, value="v1"):
    return {"name": name, "domain": ".zhihuishu.com", "path": "/", "value": value}


class CookiePersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logger = _Logger()
        self.logger_patch = mock.patch.object(
            AutoCourse, "logger", self.logger, create=True
        )
        self.save_patch = mock.patch.object(AutoCourse, "save_cookies")
        self.logger_patch.start()
        self.save_cookies = self.save_patch.start()
        self.addCleanup(self.logger_patch.stop)
        self.addCleanup(self.save_patch.stop)
        AutoCourse.remember_login_cookies(None)

    async def test_saves_on_first_change(self):
        await AutoCourse.persist_login_cookies(_Context([_cookie("a")]))
        self.assertEqual(self.save_cookies.call_count, 1)
        self.assertEqual(
            self.logger.events,
            [("保存登录凭证", {"条数": 1, "文件": AutoCourse.COOKIE_PATH})],
        )

    async def test_skips_unchanged_cookies(self):
        context = _Context([_cookie("a")])
        await AutoCourse.persist_login_cookies(context)
        await AutoCourse.persist_login_cookies(context)
        self.assertEqual(self.save_cookies.call_count, 1)

    async def test_saves_again_after_cookie_refresh(self):
        context = _Context([_cookie("a", "v1")])
        await AutoCourse.persist_login_cookies(context)
        context.cookies_payload = [_cookie("a", "v2")]
        await AutoCourse.persist_login_cookies(context)
        self.assertEqual(self.save_cookies.call_count, 2)

    async def test_remembers_loaded_cookies_without_rewrite(self):
        cookies = [_cookie("a")]
        AutoCourse.remember_login_cookies(cookies)
        await AutoCourse.persist_login_cookies(_Context(cookies))
        self.assertEqual(self.save_cookies.call_count, 0)

    async def test_ignores_empty_cookies(self):
        await AutoCourse.persist_login_cookies(_Context([]))
        await AutoCourse.persist_login_cookies(_Context(None))
        self.assertEqual(self.save_cookies.call_count, 0)
        self.assertEqual(self.logger.events, [])

    async def test_save_failure_is_logged_and_not_raised(self):
        self.save_cookies.side_effect = OSError("disk full")
        await AutoCourse.persist_login_cookies(_Context([_cookie("a")]))
        self.assertEqual(self.logger.exceptions, ["保存登录 Cookies 失败."])

    async def test_retries_after_save_failure(self):
        self.save_cookies.side_effect = [OSError("disk full"), None]
        context = _Context([_cookie("a")])
        await AutoCourse.persist_login_cookies(context)
        await AutoCourse.persist_login_cookies(context)
        self.assertEqual(self.save_cookies.call_count, 2)
        self.assertEqual(len(self.logger.events), 1)


if __name__ == "__main__":
    unittest.main()
