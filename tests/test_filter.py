"""规则引擎与活跃度过滤测试。"""
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.filter.activity import within_active_window
from app.filter.rules import rule_engine
from app.platforms.base import JobDraft


class TestRules(unittest.TestCase):
    def test_overtime_signal(self):
        d = JobDraft(platform_job_id="1", title="Java开发", description="公司实行996工作制,抗压能力强")
        reasons = rule_engine.match(d)
        joined = ";".join(reasons)
        self.assertIn("加班", joined)
        self.assertIn("压力", joined)

    def test_travel_and_dispatch(self):
        d = JobDraft(platform_job_id="2", title="运维工程师", description="需要长期出差,可能外派驻场")
        reasons = rule_engine.match(d)
        joined = ";".join(reasons)
        self.assertIn("出差", joined)
        self.assertIn("驻场", joined)

    def test_clean_description(self):
        d = JobDraft(platform_job_id="3", title="Java开发", description="双休,五险一金,弹性工作,团队氛围好")
        self.assertEqual(rule_engine.match(d), [])


class TestActivity(unittest.TestCase):
    """阈值显式传入,不依赖 .env 配置。"""

    def setUp(self):
        self.now = datetime.now()

    def test_recent_hr_active(self):
        ok, why = within_active_window(self.now - timedelta(hours=3), None, hr_days=7)
        self.assertTrue(ok)

    def test_stale_hr_active(self):
        ok, why = within_active_window(self.now - timedelta(days=30), None, hr_days=7)
        self.assertFalse(ok)
        self.assertIn("超过7天", why)

    def test_recent_published(self):
        ok, why = within_active_window(None, self.now - timedelta(days=2), pub_days=7)
        self.assertTrue(ok)

    def test_stale_published(self):
        ok, why = within_active_window(None, self.now - timedelta(days=15), pub_days=7)
        self.assertFalse(ok)
        self.assertIn("发布", why)

    def test_no_info_kept(self):
        ok, why = within_active_window(None, None)
        self.assertTrue(ok)

    def test_suffix_after_within_window(self):
        """'…后'方向:真实活跃 ≥ 估算时刻,est 在窗口内即通过。"""
        est = (self.now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M") + "后"
        ok, _ = within_active_window(est, None, hr_days=7)
        self.assertTrue(ok)

    def test_suffix_after_outside_window(self):
        est = (self.now - timedelta(days=14)).strftime("%Y-%m-%d %H:%M") + "后"
        ok, why = within_active_window(est, None, hr_days=7)
        self.assertFalse(ok)
        self.assertIn("不早于", why)

    def test_suffix_before_always_stale(self):
        """'…前'方向:真实活跃 ≤ 估算时刻,保守一律判超窗。"""
        est = (self.now - timedelta(days=95)).strftime("%Y-%m-%d %H:%M") + "前"
        ok, why = within_active_window(est, None, hr_days=7)
        self.assertFalse(ok)
        self.assertIn("不晚于", why)

    def test_date_only_today_yesterday(self):
        """'YYYY-MM-DD 活跃'(今日/昨日):按该日 00:00 判定,7 天窗口内通过。"""
        today = self.now.strftime("%Y-%m-%d") + " 活跃"
        ok, _ = within_active_window(today, None, hr_days=7)
        self.assertTrue(ok)
        yesterday = (self.now - timedelta(days=1)).strftime("%Y-%m-%d") + " 活跃"
        ok, _ = within_active_window(yesterday, None, hr_days=7)
        self.assertTrue(ok)

    def test_plain_string_accepted(self):
        """无后缀的分钟级字符串按普通时刻判定。"""
        est = (self.now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M")
        ok, _ = within_active_window(est, None, hr_days=7)
        self.assertTrue(ok)

    def test_legacy_iso_string_accepted(self):
        """旧库 ISO 微秒格式字符串兼容。"""
        est = (self.now - timedelta(days=3)).strftime("%Y-%m-%d %H:%M:%S.%f")
        ok, _ = within_active_window(est, None, hr_days=7)
        self.assertTrue(ok)


class TestActiveEstimate(unittest.TestCase):
    """活跃文案 → 存储字符串(估算基准=入库/抓取时刻)。"""

    def setUp(self):
        self.now = datetime(2026, 11, 6, 18, 21)

    def test_today_yesterday_date_only(self):
        from app.utils import active_estimate_from_text
        self.assertEqual(active_estimate_from_text("今日活跃", self.now), "2026-11-06 活跃")
        self.assertEqual(active_estimate_from_text("昨日活跃", self.now), "2026-11-05 活跃")

    def test_window_texts_use_inbound_minus_delta(self):
        from app.utils import active_estimate_from_text
        # 本周活跃 = 入库时间 - 7 天,加"后"
        self.assertEqual(active_estimate_from_text("本周活跃", self.now), "2026-10-30 18:21后")
        self.assertEqual(active_estimate_from_text("本月活跃", self.now), "2026-10-07 18:21后")
        self.assertEqual(active_estimate_from_text("3日内活跃", self.now), "2026-11-03 18:21后")

    def test_before_texts(self):
        from app.utils import active_estimate_from_text
        self.assertEqual(active_estimate_from_text("三个月前活跃", self.now), "2026-08-03 18:21前")
        self.assertEqual(active_estimate_from_text("长期未活跃", self.now), "2026-04-20 18:21前")

    def test_precise_texts(self):
        from app.utils import active_estimate_from_text
        self.assertEqual(active_estimate_from_text("2小时前活跃", self.now), "2026-11-06 16:21")
        self.assertEqual(active_estimate_from_text("3天前活跃", self.now), "2026-11-03 18:21")


class TestActiveLabel(unittest.TestCase):
    """存储字符串 → 状态标签(与存储同一套规则)。"""

    def setUp(self):
        self.now = datetime(2026, 11, 6, 18, 0)

    def test_date_only_labels(self):
        from app.utils import active_label
        self.assertEqual(active_label("2026-11-06 活跃", self.now), "今日活跃")
        self.assertEqual(active_label("2026-11-05 活跃", self.now), "昨日活跃")

    def test_suffix_labels(self):
        from app.utils import active_label
        self.assertEqual(active_label("2026-11-03 10:00后", self.now), "3天内活跃")
        self.assertEqual(active_label("2026-08-03 10:00前", self.now), "95天前活跃")

    def test_precise_labels(self):
        from app.utils import active_label
        self.assertEqual(active_label("2026-11-06 17:30", self.now), "刚刚活跃")
        self.assertEqual(active_label("2026-11-06 16:00", self.now), "2小时前活跃")
        self.assertEqual(active_label("2026-11-03 18:00", self.now), "3天前活跃")


if __name__ == "__main__":
    unittest.main()