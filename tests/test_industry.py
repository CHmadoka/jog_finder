"""行业偏好与备注生成测试(参考 boss-search 实例的信号词)。"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.filter import evaluate_job
from app.platforms.base import JobDraft


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class TestIndustryPreference(unittest.TestCase):
    def _draft(self, industry: str) -> JobDraft:
        now = __import__("datetime").datetime.now()
        return JobDraft(
            platform_job_id="x",
            title="数据处理专员",
            description="职责:数据录入与报表整理",
            hr_active_at=now,
            industry=industry,
        )

    def test_avoid_industry_flagged(self):
        res = _run(evaluate_job(
            self._draft("电子商务"), "数据处理", ["数据处理"],
            extras={"industries_avoid": ["电商", "金融"]},
        ))
        self.assertFalse(res.dropped)
        joined = ";".join(res.reasons)
        self.assertIn("行业不符", joined)
        self.assertIn("电商", joined)

    def test_prefer_industry_marked(self):
        res = _run(evaluate_job(
            self._draft("医疗服务"), "数据处理", ["数据处理"],
            extras={"industries_prefer": ["医疗"]},
        ))
        joined = ";".join(res.pluses)
        self.assertIn("行业契合", joined)
        # 非优先提示不应出现
        self.assertFalse(any("非优先" in r for r in res.reasons))

    def test_prefer_without_hit(self):
        res = _run(evaluate_job(
            self._draft("餐饮"), "数据处理", ["数据处理"],
            extras={"industries_prefer": ["医疗"]},
        ))
        self.assertTrue(any("行业非优先列表" in r for r in res.reasons))

    def test_remark_format(self):
        res = _run(evaluate_job(
            self._draft("电子商务"), "数据处理", ["数据处理"],
            extras={"industries_avoid": ["电商"]},
        ))
        # avoid 命中时备注应为"不符: 行业不符"
        self.assertIn("不符:", res.remark)
        self.assertIn("行业不符", res.remark)

    def test_dianzishangwu_hit(self):
        # "电商" 需弹性命中 "电子商务"(间隔1字)
        res = _run(evaluate_job(
            self._draft("电子商务"), "数据处理", ["数据处理"],
            extras={"industries_avoid": ["电商"]},
        ))
        self.assertTrue(any("行业不符" in r for r in res.reasons))


class TestSalesSignal(unittest.TestCase):
    def test_sales_risk(self):
        from app.filter.rules import rule_engine

        d = JobDraft(platform_job_id="1", title="数据专员",
                     description="底薪3000加提成,完成业绩指标")
        joined = ";".join(rule_engine.match(d))
        self.assertIn("销售/业绩考核", joined)

    def test_part_time_title(self):
        from app.filter.rules import rule_engine

        d = JobDraft(platform_job_id="2", title="兼职数据处理",
                     description="简单录入")
        joined = ";".join(rule_engine.match(d))
        self.assertIn("兼职", joined)


if __name__ == "__main__":
    unittest.main()