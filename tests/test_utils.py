"""工具函数测试:相对时间解析。"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.utils import parse_relative_time


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


if __name__ == "__main__":
    unittest.main()