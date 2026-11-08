"""真实路径实测:Boss 走完整 iter_jobs(1 页 + 仅 1 个岗位开详情),验证 save_raw。

与 diag_boss_raw.py 的区别:本脚本走调度器实际使用的 iter_jobs 入口,详情页由
iter_jobs 自身 new_page() 创建、经 navigate_native 导航——验证的是线上真路径。

请求量:1 次列表接口 + 1 个详情页(skip_check 把其余岗位全部判为"已入库"跳过,
不打开详情)。不加 --detail 时只跑列表页。

用法:
  .venv\\Scripts\\python scripts\\diag_boss_live.py
  .venv\\Scripts\\python scripts\\diag_boss_live.py --detail
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402
from app.config import config  # noqa: E402
from app.platforms import get_platform  # noqa: E402

KW, CITY = "Java", "上海"


def raw_files() -> set[Path]:
    d = config.RAW_DIR / "boss"
    return set(d.glob("*.json")) if d.is_dir() else set()


async def main() -> None:
    allow_detail = "--detail" in sys.argv
    plat = get_platform("boss")
    before = raw_files()
    picked: list[str] = []

    async def skip_check(job_id: str) -> bool:
        # 只放第一个岗位进详情,其余全部当作已入库跳过(不打开详情页)
        if not allow_detail or len(picked) >= 1:
            return True
        picked.append(job_id)
        print(f"  → 放行 1 个岗位进详情: {job_id}")
        return False

    await browser_manager.ensure()
    page = await browser_manager.get_or_open_page(
        plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker
    )
    print(f"载体页: {page.url[:100]}")

    n = 0
    try:
        async for d in plat.iter_jobs(
            page,
            city=CITY,
            keyword=KW,
            extras={},
            max_pages=1,
            detail_page=None,
            progress=lambda m: print(f"  [进度] {m}"),
            skip_check=skip_check,
            warn=lambda msgs, url: print(f"  [警告] {msgs} @ {url[:60]}"),
            should_stop=lambda: False,
        ):
            n += 1
            if n <= 3 or d.description:
                print(
                    f"  #{n} {d.title} | {d.company} | {d.salary}"
                    f" | desc={len(d.description or '')}字"
                    f" | hr={d.hr_name}/{d.hr_title} act={d.hr_active_text or '-'}"
                )
    except Exception as exc:  # noqa: BLE001
        print(f"  [异常] {type(exc).__name__}: {exc}")
    print(f"iter_jobs 产出 {n} 条")

    new = sorted(raw_files() - before)
    print(f"\n本次留存 {len(new)} 份:")
    for p in new:
        env = json.loads(p.read_text(encoding="utf-8"))
        body = env.get("body") or {}
        if env["kind"] == "search":
            desc = f"code={body.get('code')} 条数={len(body.get('list') or [])}"
        else:
            ev = body.get("evaluate") or {}
            desc = (f"secs={len(ev.get('secs') or [])}"
                    f" body={len(ev.get('body') or '')}字"
                    f" decodeMap={len(body.get('decodeMap') or {})}项")
        print(f"  {p.relative_to(config.DATA_DIR)}  {p.stat().st_size} B  {desc}")


try:
    asyncio.run(main())
finally:
    import warnings
    warnings.filterwarnings("ignore")
