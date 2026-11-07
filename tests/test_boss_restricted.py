"""Boss 访问受限(403/code=32)判定与立即中止逻辑测试(不访问平台)。"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.platforms.base import AntiBot
from app.platforms.boss import BossPlatform


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _page(url: str = "", title: str = "", text: str = ""):
    """构造最小 page 桩:仅提供 url/title/evaluate。"""

    class _StubPage:
        async def title(self):
            return title

        async def evaluate(self, *_a, **_k):
            return text

    p = _StubPage()
    p.url = url
    return p


class TestRestrictedDetection(unittest.TestCase):
    def setUp(self):
        self.plat = BossPlatform()

    def test_url_403_hits(self):
        self.assertTrue(self.plat._is_restricted_url(
            "https://www.zhipin.com/web/passport/zp/403.html?code=32"))
        self.assertFalse(self.plat._is_restricted_url(
            "https://www.zhipin.com/job_detail/abc.html?securityId=x"))

    def test_raise_on_restricted_url(self):
        page = _page(url="https://www.zhipin.com/web/passport/zp/403.html?code=32")
        with self.assertRaises(AntiBot):
            _run(self.plat._raise_if_restricted(page))

    def test_raise_on_restricted_text(self):
        page = _page(url="https://www.zhipin.com/job_detail/abc.html", text="访问受限")
        with self.assertRaises(AntiBot):
            _run(self.plat._raise_if_restricted(page))

    def test_normal_detail_passes(self):
        page = _page(
            url="https://www.zhipin.com/job_detail/abc.html?securityId=x",
            title="Python工程师-公司名招聘",
            text="职位描述 负责后端开发",
        )
        _run(self.plat._raise_if_restricted(page))  # 不抛即通过


if __name__ == "__main__":
    unittest.main()