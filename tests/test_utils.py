"""工具函数测试:相对时间解析、人类化间隔分布、会话预算。"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils import SessionBudget, SessionPacer, _human_gap, parse_relative_time


class TestRelativeTime(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 6, 16, 0, 0)

    def test_just_now(self):
        self.assertAlmostEqual(
            parse_relative_time("刚刚", self.now), self.now, delta=timedelta(seconds=1)
        )

    def test_minutes(self):
        self.assertEqual(
            parse_relative_time("3分钟前", self.now), self.now - timedelta(minutes=3)
        )

    def test_hours(self):
        self.assertEqual(
            parse_relative_time("2小时前", self.now), self.now - timedelta(hours=2)
        )

    def test_yesterday(self):
        self.assertEqual(
            parse_relative_time("昨天", self.now), self.now - timedelta(days=1)
        )

    def test_md_this_year(self):
        dt = parse_relative_time("11-01", self.now)
        self.assertEqual(dt, datetime(2026, 11, 1))

    def test_md_future_rolls_back(self):
        dt = parse_relative_time("12-01", self.now)
        self.assertEqual(dt, datetime(2025, 12, 1))

    def test_full_date(self):
        self.assertEqual(
            parse_relative_time("2026-09-28 10:20", self.now),
            datetime(2026, 9, 28, 10, 20),
        )

    def test_empty(self):
        self.assertIsNone(parse_relative_time("", self.now))
        self.assertIsNone(parse_relative_time("未知格式", self.now))


class TestHumanGap(unittest.TestCase):
    """人类化间隔采样:对数正态(重尾)+ 偶发长尾,替代 uniform 平顶分布。"""

    def test_bounds(self):
        gaps = [_human_gap(3, 8) for _ in range(3000)]
        self.assertGreaterEqual(min(gaps), 3.0)          # 硬下限不破
        self.assertLessEqual(max(gaps), 90.0)            # 长尾钳制 90s
        in_range = sum(1 for g in gaps if g <= 8.0)
        self.assertGreater(in_range / len(gaps), 0.85)   # 绝大多数落在配置区间

    def test_not_flat_uniform(self):
        # 均匀分布下 [3,3.5) 与 [5.5,6) 的样本量应几乎相等;
        # 对数正态应显著偏向下限(快速连续操作),两者差距要大
        gaps = [_human_gap(3, 8) for _ in range(4000)]
        low = sum(1 for g in gaps if g < 3.5)
        mid = sum(1 for g in gaps if 5.5 <= g < 6.0)
        self.assertGreater(low, mid * 2)

    def test_degenerate_range(self):
        for _ in range(50):
            g = _human_gap(5.0, 5.0)
            self.assertGreaterEqual(g, 5.0)
            self.assertLess(g, 5.3)


class TestSessionBudget(unittest.TestCase):
    def test_take_until_exhausted(self):
        b = SessionBudget(pages=3, details=5)
        self.assertEqual(b.pages_left, 3)
        self.assertTrue(all(b.take_page() for _ in range(3)))
        self.assertFalse(b.take_page())
        self.assertEqual(b.pages_left, 0)
        self.assertTrue(all(b.take_detail() for _ in range(5)))
        self.assertFalse(b.take_detail())

    def test_min_clamp(self):
        b = SessionBudget(pages=0, details=-2)
        self.assertEqual((b.pages, b.details), (1, 1))


class TestSessionPacerHazard(unittest.TestCase):
    def test_hazard_from_batch(self):
        # 危险率 = 1/平均批长(无记忆的几何触发,替代固定批长元周期)
        self.assertAlmostEqual(SessionPacer(batch=(2, 4))._hazard, 1 / 3.0)
        self.assertAlmostEqual(SessionPacer(batch=(8, 15))._hazard, 1 / 11.5)
        self.assertEqual(SessionPacer(batch=(1, 1))._hazard, 1.0)


if __name__ == "__main__":
    unittest.main()