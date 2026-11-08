"""原始数据留存(save_raw)单元测试:信封结构/开关/量控清理。"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import config  # noqa: E402
from app.utils import save_raw  # noqa: E402


class SaveRawTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = {k: getattr(config, k) for k in
                     ("RAW_DIR", "RAW_CAPTURE", "RAW_RETENTION_DAYS", "RAW_MAX_PER_PLATFORM")}
        self.addCleanup(self._restore)
        config.RAW_DIR = Path(self._tmp.name)
        config.RAW_CAPTURE = 1
        config.RAW_RETENTION_DAYS = 7
        config.RAW_MAX_PER_PLATFORM = 300

    def _restore(self):
        for k, v in self._old.items():
            setattr(config, k, v)

    def test_writes_envelope(self):
        body = {"status": "1", "resultbody": {"job": {"items": [{"jobId": "1"}]}}}
        path = save_raw("job51", "search", body, {"keyword": "数据", "page": 3})
        self.assertIsNotNone(path)
        env = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(env["platform"], "job51")
        self.assertEqual(env["kind"], "search")
        self.assertEqual(env["meta"], {"keyword": "数据", "page": 3})
        self.assertEqual(env["body"], body)          # body 原样不变形
        self.assertIn("ts", env)
        self.assertTrue(path.name.startswith("search_"))
        self.assertTrue(path.name.endswith(".json"))

    def test_disabled(self):
        config.RAW_CAPTURE = 0
        self.assertIsNone(save_raw("job51", "search", {"a": 1}))
        self.assertFalse((config.RAW_DIR / "job51").exists())

    def test_unserializable_body_no_crash(self):
        """body 含不可序列化对象:default=str 兜底,不抛异常不中断。"""
        path = save_raw("job51", "detail", {"dt": datetime(2026, 11, 8, 12, 0), "weird": object()})
        self.assertIsNotNone(path)
        env = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(env["body"]["dt"], "2026-11-08 12:00:00")

    def test_cleanup_max_per_platform(self):
        config.RAW_MAX_PER_PLATFORM = 3
        paths = []
        base = time.time() - 100
        for i in range(5):
            p = save_raw("boss", "search", {"i": i})
            # 伪造严格递增的过去时刻,消除同秒写入的 mtime 竞态
            os.utime(p, (base + i, base + i))
            paths.append(p)
        # 每次写入都触发清理:最终只留最新 3 份
        left = sorted((config.RAW_DIR / "boss").glob("*.json"))
        self.assertEqual(len(left), 3)
        self.assertNotIn(paths[0], left)   # 最旧的被清掉
        self.assertIn(paths[-1], left)     # 最新的保留

    def test_cleanup_retention_days(self):
        config.RAW_RETENTION_DAYS = 7
        path = save_raw("liepin", "search", {"a": 1})
        # 伪造 mtime 为 8 天前 → 超期;再写一份触发清理
        old_ts = (datetime.now() - timedelta(days=8)).timestamp()
        os.utime(path, (old_ts, old_ts))
        save_raw("liepin", "search", {"b": 2})
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()