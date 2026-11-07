"""完整链路实测:调度器启动 job51/liepin 真实任务(1词条×上海×1页),验证入库/查重/过滤。

用法: .venv\\Scripts\\python scripts\\test_full_task.py job51|liepin
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import config  # noqa: E402

config.ensure_dirs()
from app.scheduler.runner import scheduler  # noqa: E402


async def watch(pid: str, task_id: int) -> None:
    import asyncio as _a

    while scheduler.is_running(pid):
        st = scheduler.status().get(pid, {})
        prog = st.get("progress", "")
        print("  [状态]", prog[:100], flush=True)
        await _a.sleep(4)
    st = scheduler.status().get(pid, {})
    print("最终:", st.get("progress"))
    print("计数:", st.get("counters"))


async def main() -> None:
    pid = sys.argv[1] if len(sys.argv) > 1 else "job51"
    # 仅跑档位的第一个词条(实测最小化):临时档位 id=1 词条多,取其第一个词
    from app.models import Gear, SessionLocal

    with SessionLocal() as db:
        gear = db.get(Gear, 1)
        kws = list(gear.keywords or [])
    print(f"档位词条: {kws}")
    # 临时单词条:直接传 city 与薪资,不走条件;词条用档位第一个(通过新建临时档位避免改动)
    from app.models import Gear as G

    with SessionLocal() as db:
        g = G(name="__实测临时档位", keywords=[kws[0]])
        db.add(g)
        db.commit()
        gid = g.id
    try:
        task_id = await scheduler.start(
            pid,
            gear_id=gid,
            cities=["上海"],
            max_pages=1,
        )
        print(f"任务 {task_id} 启动")
        await watch(pid, task_id)
        import sqlite3

        con = sqlite3.connect("data/job_finder.db")
        n = con.execute("select count(*) from job where platform=?", (pid,)).fetchone()[0]
        print(f"{pid} 库内岗位数: {n}")
        rows = con.execute(
            "select title,company,salary,city,district,industry,hr_active_text,remark,length(description)"
            " from job where platform=? order by id desc limit 3",
            (pid,),
        ).fetchall()
        for r in rows:
            print("  库内:", str(r)[:220])
        con.close()
    finally:
        with SessionLocal() as db:
            g = db.get(G, gid)
            if g:
                db.delete(g)
                db.commit()
        print("临时档位已清理")


asyncio.run(main())