"""解析公共函数与 51job 结构化字段测试(不访问平台)。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.platforms.common import deep_find_list, parse_salary_months, strip_html


class TestStripHtml(unittest.TestCase):
    def test_basic(self):
        html = "<p>职责一<br>职责二</p><p>&nbsp;负责&nbsp;Java&nbsp;开发</p>"
        text = strip_html(html)
        self.assertIn("职责一\n职责二", text)
        self.assertIn("负责 Java 开发", text)

    def test_collapse_newlines(self):
        self.assertEqual(strip_html("<div>a</div><div>b</div>"), "a\nb")


class TestSalaryMonths(unittest.TestCase):
    def test_13xin(self):
        self.assertEqual(parse_salary_months("1.5-2.5万·13薪"), 13)

    def test_16xin(self):
        self.assertEqual(parse_salary_months("15-25K×16薪"), 16)

    def test_no_month(self):
        self.assertIsNone(parse_salary_months("20-35万"))


class TestDeepFindList(unittest.TestCase):
    def test_direct_key(self):
        data = {"a": 1, "engine_search_result": [{"jobName": "Java", "jobId": 9}]}
        found = deep_find_list(data, ["engine_search_result", "engine_jds"])
        self.assertEqual(found[0]["jobName"], "Java")

    def test_nested(self):
        data = {"search": {"result": {"engine_jds": [{"jobName": "x", "jobId": 1}]}}}
        found = deep_find_list(data, ["engine_search_result", "engine_jds"])
        self.assertEqual(len(found), 1)

    def test_none(self):
        self.assertIsNone(deep_find_list({"a": {"b": 1}}, ["engine_jds"]))


class TestJob51Helpers(unittest.TestCase):
    def test_extract_job_id(self):
        from app.platforms.job51 import _extract_job_id

        self.assertEqual(_extract_job_id("https://jobs.51job.com/all/12345678.html"), "12345678")
        self.assertEqual(_extract_job_id("https://jobs.51job.com/shanghai/99999999.html"), "99999999")
        self.assertEqual(_extract_job_id("https://x.com/a?jobId=abc123"), "abc123")


if __name__ == "__main__":
    unittest.main()