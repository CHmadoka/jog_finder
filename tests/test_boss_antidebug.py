"""反调试免疫与原生导航的单元测试(不访问任何平台)。

覆盖两处实测修复:
1. _arm_antidebug 只对招聘站点 reload,且已带 __jfArm 标记的文档不再重刷
   (旧实现每次连接 CDP 都把用户所有标签页 reload 一遍,等于反复请求平台);
2. navigate_native 优先走 CDP Page.navigate(page.goto 会被 Boss 反调试识别
   并自杀式清空文档),拿不到 CDP 会话时回退 Playwright 导航。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser.manager import BrowserManager, _needs_arm  # noqa: E402


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class _StubPage:
    def __init__(self, url: str, armed: object = False):
        self.url = url
        self._armed = armed
        self.reloads = 0
        self.close_handlers: list = []

    def on(self, _event, handler):
        self.close_handlers.append(handler)

    async def evaluate(self, *_a, **_k):
        if isinstance(self._armed, Exception):
            raise self._armed
        return self._armed

    async def reload(self, **_k):
        self.reloads += 1


class _StubCtx:
    def __init__(self, pages):
        self.pages = pages
        self.init_scripts: list[str] = []

    async def add_init_script(self, script):
        self.init_scripts.append(script)

    def on(self, _event, _handler):
        pass


class TestNeedsArm(unittest.TestCase):
    def test_recruit_sites_armed(self):
        for u in (
            "https://www.zhipin.com/wuhan/",
            "https://we.51job.com/pc/search",
            "https://www.liepin.com/zhaopin/",
        ):
            self.assertTrue(_needs_arm(u), u)

    def test_other_pages_untouched(self):
        for u in ("about:blank", "", "chrome://newtab/", "https://example.com/"):
            self.assertFalse(_needs_arm(u), u)


class TestArmAntidebug(unittest.TestCase):
    def test_reloads_only_recruit_sites(self):
        boss = _StubPage("https://www.zhipin.com/wuhan/")
        other = _StubPage("https://example.com/read")
        blank = _StubPage("about:blank")
        ctx = _StubCtx([boss, other, blank])
        _run(BrowserManager._arm_antidebug(ctx))
        self.assertEqual(boss.reloads, 1)      # 需要补丁 → reload 一次
        self.assertEqual(other.reloads, 0)     # 用户自己的页面不动
        self.assertEqual(blank.reloads, 0)
        self.assertEqual(len(ctx.init_scripts), 1)

    def test_skips_already_armed(self):
        """已带 __jfArm 标记:不再 reload(重连不等于刷新平台页面)。"""
        p = _StubPage("https://www.liepin.com/zhaopin/", armed=True)
        ctx = _StubCtx([p])
        _run(BrowserManager._arm_antidebug(ctx))
        self.assertEqual(p.reloads, 0)

    def test_evaluate_failure_falls_back_to_reload(self):
        """标记读取失败(导航中/崩溃页)按未受保护处理,仍 reload 保命。"""
        p = _StubPage("https://www.zhipin.com/x", armed=RuntimeError("nav in progress"))
        ctx = _StubCtx([p])
        _run(BrowserManager._arm_antidebug(ctx))
        self.assertEqual(p.reloads, 1)


class _StubCdp:
    def __init__(self):
        self.sent: list[tuple[str, dict]] = []
        self.detached = False

    async def send(self, method, params=None):
        self.sent.append((method, params or {}))
        return {}

    async def detach(self):
        self.detached = True


class _StubNavPage:
    """可切换"CDP 会话可用/不可用"的页面桩。"""

    def __init__(self, cdp_ok: bool = True):
        self.cdp = _StubCdp()
        self.cdp_ok = cdp_ok
        self.goto_calls: list[dict] = []
        self.context = self

    async def new_cdp_session(self, _page=None):
        if not self.cdp_ok:
            raise RuntimeError("no session")
        return self.cdp

    async def goto(self, url, **kw):
        self.goto_calls.append({"url": url, **kw})


class TestNavigateNative(unittest.TestCase):
    def setUp(self):
        self.bm = BrowserManager()

    def test_uses_cdp_and_forwards_referrer(self):
        page = _StubNavPage()
        _run(self.bm.navigate_native(page, "https://x/job_detail/1.html",
                                    referrer="https://x/list"))
        self.assertEqual(len(page.cdp.sent), 1)
        method, params = page.cdp.sent[0]
        self.assertEqual(method, "Page.navigate")
        self.assertEqual(params["url"], "https://x/job_detail/1.html")
        self.assertEqual(params["referrer"], "https://x/list")
        self.assertEqual(params["transitionType"], "link")
        self.assertEqual(page.goto_calls, [])          # 绝不用 goto
        self.assertTrue(page.cdp.detached)

    def test_falls_back_to_goto_without_session(self):
        page = _StubNavPage(cdp_ok=False)
        _run(self.bm.navigate_native(page, "https://x/job_detail/1.html"))
        self.assertEqual(len(page.goto_calls), 1)      # 会话不可用时保功能
        self.assertEqual(page.goto_calls[0]["url"], "https://x/job_detail/1.html")


if __name__ == "__main__":
    unittest.main()
