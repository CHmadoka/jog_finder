"""诊断四:批量采集 (hrOnlineLabelStatus, hrLabels/onlineHrLabelList, hrIsOnline) 组合,
确认状态码语义与取值范围(不写库,仅页内 fetch 数页)。"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402
from app.platforms import get_platform  # noqa: E402

KW = ["数据分析", "运维工程师", "行政", "会计", "销售", "测试工程师", "人事专员", "前端"]


async def main() -> None:
    plat = get_platform("job51")
    await browser_manager.ensure(plat.home_url)
    page = await browser_manager.get_or_open_page(
        plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker
    )
    if "we.51job.com" not in (page.url or ""):
        await page.goto(plat.home_prefix, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(3000)
    pairs: Counter = Counter()
    label_by_status: defaultdict = defaultdict(Counter)
    online_by_status: defaultdict = Counter()
    for kw in KW:
        raw = await page.evaluate(plat._fetch_js(plat._api_qs(kw, "020000", 1)))
        try:
            d = json.loads(raw)
        except Exception:
            print(f"[{kw}] 非JSON响应,跳过")
            continue
        items = ((d.get("resultbody") or {}).get("job") or {}).get("items") or []
        for it in items:
            st = it.get("hrOnlineLabelStatus")
            pairs[st] += 1
            online_by_status[(st, bool(it.get("hrIsOnline")))] += 1
            labs = list(it.get("onlineHrLabelList") or []) or list(it.get("hrLabels") or [])
            for lb in labs:
                label_by_status[st][lb] += 1
        await asyncio.sleep(4)
    print("== status 分布 ==", dict(pairs))
    print("== (status, hrIsOnline) 组合 ==", {f"st{k[0]}/online={k[1]}": v for k, v in online_by_status.items()})
    for st in sorted(label_by_status, key=lambda x: (x is None, x)):
        print(f"\n== status={st} 标签频次 ==")
        for lb, n in label_by_status[st].most_common(12):
            print(f"  {lb}: {n}")
    await browser_manager.shutdown()


asyncio.run(main())