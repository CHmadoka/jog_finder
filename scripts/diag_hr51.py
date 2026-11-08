"""诊断:前程无忧 search-pc 接口 HR 相关字段实测(不写库,只打印)。

用法: .venv\\Scripts\\python scripts\\diag_hr51.py [关键词]
连接当前已登录浏览器,页内 fetch 一页搜索接口,汇总全部 hr* 字段形态。
"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402
from app.platforms import get_platform  # noqa: E402


async def main() -> None:
    kw = sys.argv[1] if len(sys.argv) > 1 else "运维"
    plat = get_platform("job51")
    await browser_manager.ensure(plat.home_url)
    page = await browser_manager.get_or_open_page(
        plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker
    )
    if "we.51job.com" not in (page.url or ""):
        await page.goto(plat.home_prefix, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
    raw = await page.evaluate(plat._fetch_js(plat._api_qs(kw, "020000", 1)))
    d = json.loads(raw)
    items = ((d.get("resultbody") or {}).get("job") or {}).get("items") or []
    print(f"status={d.get('status')} items={len(items)} keyword={kw}")
    if not items:
        print("无数据:", str(d)[:300])
        return
    # 第一条的完整键
    print("\n== 条目全部键 ==")
    print(", ".join(sorted(items[0].keys())))
    # HR 相关字段汇总
    keys = [k for k in items[0] if "hr" in k.lower() or "online" in k.lower()]
    print(f"\n== HR 相关键: {keys} ==")
    for k in keys:
        vals = Counter()
        for it in items:
            v = it.get(k)
            vals[json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v] += 1
        print(f"{k}: {dict(vals)}")
    # 样本条目(前3条)的 HR 概览
    print("\n== 样本 ==")
    for it in items[:3]:
        hr = {k: it.get(k) for k in keys}
        print(json.dumps({
            "jobName": it.get("jobName"), "issueDateString": it.get("issueDateString"),
            **hr,
        }, ensure_ascii=False))
    await browser_manager.shutdown()


asyncio.run(main())