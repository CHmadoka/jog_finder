"""前程无忧/猎聘适配器解析逻辑单元测试(2026-11 实测接口结构)。"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.platforms import get_platform  # noqa: E402
from app.platforms.common import parse_salary_k, salary_in_range  # noqa: E402
from app.utils import active_estimate_from_text  # noqa: E402


class SalaryMixedUnitTests(unittest.TestCase):
    """混合单位薪资解析("9千-1.5万" = 9K~15K,旧实现误算为 15~90)。"""

    def test_qian_to_wan(self):
        self.assertEqual(parse_salary_k("9千-1.5万"), (9.0, 15.0))

    def test_qian_to_qian(self):
        self.assertEqual(parse_salary_k("5千-8千"), (5.0, 8.0))

    def test_k_range(self):
        self.assertEqual(parse_salary_k("15-25K·16薪"), (15.0, 25.0))

    def test_wan_range(self):
        self.assertEqual(parse_salary_k("1.5-2.5万"), (15.0, 25.0))

    def test_single_k(self):
        self.assertEqual(parse_salary_k("20K"), (20.0, 20.0))

    def test_yearly(self):
        lo, hi = parse_salary_k("30-60万/年")
        self.assertAlmostEqual(lo, 25.0, places=1)
        self.assertAlmostEqual(hi, 50.0, places=1)

    def test_unparseable(self):
        self.assertIsNone(parse_salary_k("面议"))
        self.assertIsNone(parse_salary_k("200元/天"))

    def test_range_overlap(self):
        # 部分重叠算通过
        self.assertTrue(salary_in_range("15-25K", "10-20"))
        self.assertFalse(salary_in_range("5-8K", "10-20"))
        # 无法解析不误杀
        self.assertIsNone(salary_in_range("面议", "10-20"))


class ActiveTextTests(unittest.TestCase):
    """猎聘活跃文案("N天前在线"等)→ 统一估算规则。"""

    def test_days_ago_online(self):
        now = datetime(2026, 11, 7, 12, 0)
        est = active_estimate_from_text("1天前在线", now)
        self.assertEqual(est, "2026-11-06 12:00")

    def test_weeks_ago_online(self):
        now = datetime(2026, 11, 7, 12, 0)
        est = active_estimate_from_text("2周前在线", now)
        self.assertEqual(est, "2026-10-24 12:00")

    def test_months_ago_online(self):
        now = datetime(2026, 11, 7, 12, 0)
        est = active_estimate_from_text("3个月前在线", now)
        self.assertEqual(est, "2026-08-09 12:00")  # 90 天前

    def test_today_online(self):
        now = datetime(2026, 11, 7, 12, 0)
        est = active_estimate_from_text("今日在线", now)
        self.assertEqual(est, "2026-11-07 活跃")

    def test_just_online(self):
        now = datetime(2026, 11, 7, 12, 0)
        est = active_estimate_from_text("刚刚在线", now)
        self.assertEqual(est, "2026-11-07 12:00")

    def test_current_online(self):
        """猎聘"当前在线"文案 = 刚刚活跃。"""
        now = datetime(2026, 11, 7, 12, 0)
        est = active_estimate_from_text("当前在线", now)
        self.assertEqual(est, "2026-11-07 12:00")


class Job51Tests(unittest.TestCase):
    def setUp(self):
        self.plat = get_platform("job51")

    def test_extract_job_id(self):
        from app.platforms.job51 import _extract_job_id

        self.assertEqual(
            _extract_job_id("https://jobs.51job.com/shanghai-hpq/174149954.html?s=sou"),
            "174149954",
        )
        self.assertEqual(
            _extract_job_id("https://jobs.51job.com/all/174148178.html"),
            "174148178",
        )

    def test_api_qs(self):
        qs = self.plat._api_qs("Java", "020000", 2)
        self.assertIn("keyword=Java", qs)
        self.assertIn("jobArea=020000", qs)
        self.assertIn("pageNum=2", qs)
        self.assertIn("pageSize=20", qs)  # 与页面自身搜索请求对齐(防翻页裂缝)

    def test_salary_from_ld(self):
        from app.platforms.job51 import _salary_from_ld

        # 元/月
        self.assertEqual(_salary_from_ld({"value": {"minValue": 15000, "maxValue": 25000}, "unitText": "MONTH"}), "15-25K")
        # 元/年 → 月
        self.assertEqual(_salary_from_ld({"value": {"minValue": 180000, "maxValue": 300000}, "unitText": "YEAR"}), "15-25K")
        # 无单位大数值
        self.assertEqual(_salary_from_ld({"value": {"minValue": 9000, "maxValue": 15000}}), "9-15K")
        # 缺失
        self.assertEqual(_salary_from_ld(None), "")
        self.assertEqual(_salary_from_ld({"value": {}}), "")

    def test_draft_from_api(self):
        j = {
            "jobId": "174148178",
            "jobName": "中级测试工程师",
            "jobHref": "https://jobs.51job.com/shanghai/174148178.html?s=sou",
            "provideSalaryString": "2-2.2万·14薪",
            "issueDateString": "2026-11-06 14:27:11",
            "jobAreaLevelDetail": {"cityString": "上海", "districtString": "黄浦区"},
            "workYearString": "5-8年",
            "degreeString": "本科",
            "companyName": "三井住友海上火灾保险（中国）",
            "coIndustryText": "保险",
            "companySizeString": "150-500人",
            "hrName": "顾女士",
            "hrPosition": "人事部招聘专员",
            "hrIsOnline": False,
            "jobTags": ["5-8年", "本科", "java", "sql", "五险一金"],
            "jobDescribe": "岗位职责：1.测试",
            "isExpire": False,
            "jobSalaryMin": "20000",
            "jobSalaryMax": "22000",
        }
        draft, warns = self.plat._draft_from_api(j, "上海")
        self.assertEqual(warns, [])
        self.assertEqual(draft.platform_job_id, "174148178")
        self.assertEqual(draft.title, "中级测试工程师")
        self.assertEqual(draft.salary, "2-2.2万·14薪")
        self.assertEqual(draft.salary_months, 14)
        self.assertEqual(draft.city, "上海")
        self.assertEqual(draft.district, "黄浦区")
        self.assertEqual(draft.industry, "保险")
        self.assertEqual(draft.company_scale, "150-500人")
        self.assertEqual(draft.hr_name, "顾女士")
        self.assertFalse(draft.hr_online)
        self.assertEqual(draft.published_at, datetime(2026, 11, 6, 14, 27, 11))
        # 描述包含原文与技能标签(剔除与经验/学历重复的项)
        self.assertIn("岗位职责", draft.description)
        self.assertIn("java", draft.description)
        self.assertIn("五险一金", draft.description)
        self.assertNotIn("5-8年", draft.description.split("技能/福利标签")[1])
        # 数值薪资存 raw(供过滤)
        self.assertEqual(draft.raw["salaryMinK"], 20.0)
        self.assertEqual(draft.raw["salaryMaxK"], 22.0)

    def test_draft_from_api_expired(self):
        draft, warns = self.plat._draft_from_api({"jobId": "1", "jobName": "x", "isExpire": True}, "上海")
        self.assertIsNone(draft)
        self.assertTrue(any("过期" in w for w in warns))

    def test_draft_from_api_missing(self):
        draft, warns = self.plat._draft_from_api({"jobId": "", "jobName": ""}, "上海")
        self.assertIsNone(draft)
        self.assertEqual(len(warns), 2)

    def test_search_url(self):
        u = self.plat._search_url("上海", "Java", 1, {})
        self.assertIn("we.51job.com/pc/search", u)
        self.assertIn("jobArea=020000", u)


class LiepinTests(unittest.TestCase):
    def setUp(self):
        self.plat = get_platform("liepin")

    def test_job_id_from_url(self):
        from app.platforms.liepin import _job_id_from_url

        self.assertEqual(_job_id_from_url("https://www.liepin.com/job/1970662049.shtml?x=1"), "1970662049")
        self.assertEqual(_job_id_from_url(""), "")

    def test_parse_compact_dt(self):
        from app.platforms.liepin import _parse_compact_dt

        self.assertEqual(_parse_compact_dt("20251103160956"), datetime(2025, 11, 3, 16, 9, 56))
        self.assertEqual(_parse_compact_dt("20261030"), datetime(2026, 10, 30))
        self.assertIsNone(_parse_compact_dt("bad"))
        self.assertIsNone(_parse_compact_dt(""))

    def test_search_url(self):
        u = self.plat._search_url("上海", "Java", 2)
        self.assertIn("liepin.com/zhaopin/", u)
        self.assertIn("key=Java", u)
        self.assertIn("city=020", u)
        self.assertIn("currentPage=2", u)
        # 全国:不携带 city 参数
        u0 = self.plat._search_url("", "Java", 1)
        self.assertNotIn("city=", u0)

    def test_city_codes_validated(self):
        """实测 hotCities 城市码:全国=410(旧表 000 是错的)。"""
        from app.platforms.selectors import LIEPIN

        self.assertEqual(LIEPIN["city_codes"]["全国"], "410")
        self.assertEqual(LIEPIN["city_codes"]["武汉"], "170020")
        self.assertEqual(LIEPIN["city_codes"]["成都"], "280020")
        self.assertTrue(
            {"北京", "上海", "广州", "深圳", "杭州"} <= self.plat.supported_cities()
        )

    def test_parse_list_json(self):
        import asyncio

        data = {
            "flag": 1,
            "data": {
                "data": {
                    "jobCardList": [
                        {
                            "job": {
                                "jobId": "70662049",
                                "title": "初级Java开发工程师",
                                "dq": "福州-鼓楼区",
                                "salary": "9-20k",
                                "requireWorkYears": "1-3年",
                                "requireEduLevel": "本科",
                                "refreshTime": "20251103160956",
                                "link": "https://www.liepin.com/job/1970662049.shtml",
                            },
                            "comp": {
                                "compName": "顶点",
                                "compIndustry": "计算机软件",
                                "compScale": "1000-2000人",
                                "compStage": "沪深A股上市",
                            },
                            "recruiter": {
                                "recruiterName": "陈女士",
                                "recruiterTitle": "招聘专员",
                                "imShowText": "1天前在线",
                            },
                        }
                    ]
                }
            },
        }
        drafts = asyncio.run(self.plat._parse_list_json(data, "上海", None))
        self.assertEqual(len(drafts), 1)
        d = drafts[0]
        self.assertEqual(d.platform_job_id, "1970662049")
        self.assertEqual(d.title, "初级Java开发工程师")
        self.assertEqual(d.city, "福州")       # 平台混入推荐岗位,如实记录
        self.assertEqual(d.district, "鼓楼区")
        self.assertEqual(d.salary, "9-20k")
        self.assertEqual(d.experience, "1-3年")
        self.assertEqual(d.education, "本科")
        self.assertEqual(d.company, "顶点")
        self.assertEqual(d.industry, "计算机软件")
        self.assertEqual(d.company_stage, "沪深A股上市")
        self.assertEqual(d.company_scale, "1000-2000人")
        self.assertEqual(d.hr_name, "陈女士")
        self.assertEqual(d.hr_title, "招聘专员")
        self.assertEqual(d.hr_active_text, "1天前在线")
        # 活跃估算:1天前在线 → 昨天的时刻字符串
        from app.utils import parse_dt_loose

        est = parse_dt_loose(d.hr_active_at)
        self.assertIsNotNone(est)
        self.assertEqual(est.date(), datetime.now().date() - timedelta(days=1))
        self.assertEqual(d.published_at, datetime(2025, 11, 3, 16, 9, 56))

    def test_parse_list_json_missing_title(self):
        import asyncio

        async def warn(msgs, url):
            self.warned = (msgs, url)

        self.warned = None
        data = {"flag": 1, "data": {"data": {"jobCardList": [{"job": {}, "comp": {}, "recruiter": {}}]}}}
        drafts = asyncio.run(self.plat._parse_list_json(data, "上海", warn))
        self.assertEqual(drafts, [])
        self.assertIsNotNone(self.warned)


if __name__ == "__main__":
    unittest.main()