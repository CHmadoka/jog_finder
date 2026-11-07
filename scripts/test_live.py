"""小规模实测适配器主流程(不写库):job51/liepin 各取前 5 条验证解析质量。

用法: .venv\\Scripts\\python scripts\\test_live.py job51|liepin
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402
from app.platforms import get_platform  # noqa: E402


async def warn_cb(msgs, url):
    for m in msgs:
        print(f"  [警告] {m}  (url={url[:60]})")


async def live(pid: str, limit: int = 5) -> None:
    plat = get_platform(pid)
    print(f"\n========== 实测 {plat.name} ==========")
    await browser_manager.ensure()
    ctx = browser_manager.primary_context()
    page = await browser_manager.get_or_open_page(plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker)
    logged = await plat.is_logged_in(page)
    print(f"登录态: {'已登录' if logged else '未登录!!'}")
    if not logged:
        return
    count = 0
    try:
        async for d in plat.iter_jobs(
            page,
            city="上海",
            keyword="Java",
            extras={},
            max_pages=1,
            progress=lambda m: print(f"  [进度] {m}"),
            skip_check=None,
            warn=warn_cb,
            should_stop=lambda: False,
        ):
            count += 1
            print(
                f"  #{count} {d.title} | {d.company} | {d.salary} | {d.city}-{d.district}"
                f" | exp={d.experience} edu={d.education} | ind={d.industry}"
                f" | hr={d.hr_name}/{d.hr_title}/online={d.hr_online}/act={d.hr_active_text}"
                f" | pub={d.published_at} | desc={len(d.description or '')}字"
                f" | id={d.platform_job_id}"
            )
            if count >= limit:
                print(f"  (达到实测上限 {limit},提前结束)")
                break
    except Exception as exc:  # noqa: BLE001
        print(f"  [异常] {type(exc).__name__}: {exc}")
    print(f"实测结束:共取得 {count} 条")


async def main() -> None:
    for pid in (sys.argv[1:] or ["job51", "liepin"]):
        await live(pid)
        await asyncio.sleep(3)
    await browser_manager.shutdown()


asyncio.run(main())