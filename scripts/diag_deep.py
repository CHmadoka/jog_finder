"""深度诊断 51job/liepin 搜索页:XHR 接口、window 变量、DOM 卡片结构。

只访问 1 次搜索页(低频防风控)。
用法: .venv\\Scripts\\python scripts\\diag_deep.py job51|liepin
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402
from app.platforms import get_platform  # noqa: E402

SNAP = Path(__file__).resolve().parent.parent / "data" / "snapshots"


async def diag(pid: str) -> None:
    plat = get_platform(pid)
    print(f"\n========== {plat.name} ==========")
    await browser_manager.ensure()
    ctx = browser_manager.primary_context()
    page = await browser_manager.get_or_open_page(plat.home_prefix, plat.home_url)
    url = (
        plat._search_url("上海", "Java", 1, {})
        if pid == "job51"
        else plat._search_url("上海", "Java", 1)
    )
    print("搜索页:", url[:130])
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as exc:  # noqa: BLE001
        print("goto 异常:", exc)
    await page.wait_for_timeout(6000)

    # 1) window 上所有 __ 开头的全局变量
    try:
        names = await page.evaluate("() => Object.keys(window).filter(k => k.startsWith('__'))")
        print("window.__* 变量:", names[:20])
        for n in names[:4]:
            try:
                s = json.dumps(await page.evaluate(f"() => window['{n}']"), ensure_ascii=False, default=str)
                p = SNAP / f"diag_{pid}_{n.strip('_')}.json"
                p.write_text(s, encoding="utf-8")
                print(f"  {n}: {len(s)} 字符 -> {p.name}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {n}: 序列化失败 {exc}")
    except Exception as exc:  # noqa: BLE001
        print("枚举失败:", exc)

    # 2) XHR/fetch 资源请求(找搜索接口)
    try:
        reqs = await page.evaluate(
            """() => performance.getEntriesByType('resource')
                .map(e => e.name)
                .filter(u => /api|search|list|query/i.test(u) && !/\\.(js|css|png|jpg|svg|gif|woff)/.test(u))
                .slice(0, 30)"""
        )
        print(f"接口类请求 {len(reqs or [])} 个:")
        for r in reqs or []:
            print("  ", r[:160])
    except Exception as exc:  # noqa: BLE001
        print("资源枚举失败:", exc)

    # 3) DOM 卡片候选选择器命中数
    candidates = {
        "job51": [".j_joblist .e", ".j_joblist .el", ".j_joblist > div", "[class*='joblist'] [class*='job']",
                  ".joblist-box .e", "div[class*='JobCard']", "[class*='jobcard']"],
        "liepin": [".job-card-pc-container", ".job-list-box li", "ul.sojobs li", "[class*='job-card']",
                   "[data-nick]", ".job-card-box"],
    }[pid]
    for sel in candidates:
        try:
            n = await page.locator(sel).count()
            if n:
                print(f"  [命中 {n:3d}] {sel}")
        except Exception:
            pass

    # 4) 保存完整 HTML 快照供离线分析
    try:
        html = await page.content()
        p = SNAP / f"diag_{pid}_search.html"
        p.write_text(html, encoding="utf-8")
        print(f"HTML 快照: {p.name}({len(html)} 字符)")
    except Exception as exc:  # noqa: BLE001
        print("快照失败:", exc)
    await asyncio.sleep(1)


async def main() -> None:
    for pid in (sys.argv[1:] or ["job51", "liepin"]):
        await diag(pid)
        await asyncio.sleep(2)
    await browser_manager.shutdown()
    print("\n完成")


asyncio.run(main())