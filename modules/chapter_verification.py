"""章节测试页的人工安全验证等待；不操作验证控件或作答接口。"""
import asyncio
from time import monotonic

from playwright.async_api import Error

from modules.llm_types import LLMError
from modules.tasks import has_visible_verification


VERIFICATION_TIMEOUT_MS = 120000
VERIFICATION_RESTART_LIMIT = 3


def _notify(callback, *, state, message):
    if callback:
        callback({'kind': 'test_navigation', 'state': state, 'message': message})


async def wait_for_chapter_verification(page, callback=None, *, timeout=VERIFICATION_TIMEOUT_MS):
    """保留当前页面等待用户完成安全验证；不操作验证组件或发送作答请求。"""
    if page.is_closed():
        raise LLMError('测试窗口已关闭，无法继续等待安全验证。')
    if not await has_visible_verification(page):
        return False
    _notify(callback, state='等待验证', message=f'检测到安全验证，已暂停；请在浏览器完成验证，最多等待 {timeout / 1000:g} 秒。')
    try:
        await page.bring_to_front()
    except Error:
        pass
    deadline = monotonic() + timeout / 1000
    hidden_since = None
    while True:
        if page.is_closed():
            raise LLMError('等待安全验证时测试窗口被关闭，已停止当前章节。')
        visible = await has_visible_verification(page)
        now = monotonic()
        if visible:
            hidden_since = None
        elif hidden_since is None:
            hidden_since = now
        elif now - hidden_since >= 0.5:
            _notify(callback, state='重新核对', message='安全验证已完成，正在重新核对页面后继续。')
            return True
        if now >= deadline:
            error = LLMError('等待安全验证超时；本次未继续回填或提交，请完成验证后重试。')
            error.chapter_stage = '等待安全验证'
            raise error
        await asyncio.sleep(min(0.25, max(0, deadline - now)))
