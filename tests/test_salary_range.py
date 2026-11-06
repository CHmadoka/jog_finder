"""薪资范围解析与范围过滤测试(不访问平台)。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.platforms.common import parse_salary_k, parse_salary_range, salary_in_range


class TestParseSalaryK(unittest.TestCase):
    def test_range_k(self):
        self.assertEqual(parse_salary_k("15-25K·16薪"), (15.0, 25.0))

    def test_range_wan(self):
        self.assertEqual(parse_salary_k("1.5-2.5万"), (15.0, 25.0))

    def test_single_k(self):
        self.assertEqual(parse_salary_k("20K"), (20.0, 20.0))

    def test_annual_wan(self):
        # 30-60万/年 → 约 25-50K/月
        self.assertEqual(parse_salary_k("30-60万/年"), (25.0, 50.0))

    def test_negotiable(self):
        self.assertIsNone(parse_salary_k("面议"))
        self.assertIsNone(parse_salary_k(""))

    def test_daily_ignored(self):
        self.assertIsNone(parse_salary_k("200-300元/天"))


class TestParseSalaryRange(unittest.TestCase):
    def test_comma_both(self):
        self.assertEqual(parse_salary_range("10,20"), (10.0, 20.0))

    def test_dash(self):
        self.assertEqual(parse_salary_range("10-20"), (10.0, 20.0))

    def test_left_only(self):
        self.assertEqual(parse_salary_range("10,"), (10.0, None))
        self.assertEqual(parse_salary_range("10"), (10.0, None))

    def test_right_only(self):
        self.assertEqual(parse_salary_range(",20"), (None, 20.0))

    def test_chinese_comma(self):
        self.assertEqual(parse_salary_range("10，20"), (10.0, 20.0))

    def test_swapped(self):
        self.assertEqual(parse_salary_range("20-10"), (10.0, 20.0))

    def test_invalid(self):
        self.assertIsNone(parse_salary_range(""))
        self.assertIsNone(parse_salary_range("abc"))
        self.assertIsNone(parse_salary_range(",,"))
        self.assertIsNone(parse_salary_range("10,20,30"))


class TestSalaryInRange(unittest.TestCase):
    def test_overlap(self):
        self.assertTrue(salary_in_range("15-25K·16薪", "10-20"))

    def test_below(self):
        self.assertFalse(salary_in_range("5-8K", "10,"))

    def test_above(self):
        self.assertFalse(salary_in_range("30-40K", ",25"))

    def test_unparseable_job(self):
        self.assertIsNone(salary_in_range("面议", "10-20"))

    def test_empty_range(self):
        self.assertIsNone(salary_in_range("15-25K", ""))


if __name__ == "__main__":
    unittest.main()