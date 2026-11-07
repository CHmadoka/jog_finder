"""快速查看浏览器标签页与 51job/liepin 页面实际登录状态(只读,不导航)。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402


async def main() -> None:
    browser = await browser_manager.ensure()
    ctx = browser_manager.primary_context()
    print(f"标签页数: {len(ctx.pages)}")
    for i, p in enumerate(ctx.pages):
        try:
            title = (await p.title())[:40]
        except Exception:
            title = "?"
        print(f"  [{i}] {p.url[:110]}  | {title}")

    for frag in ("51job", "liepin"):
        pg = next((p for p in ctx.pages if frag in p.url), None)
        if not pg:
            print(f"\n== 无 {frag} 标签 ==")
            continue
        print(f"\n== {frag} 页面实况 ==")
        print("  URL:", pg.url[:140])
        try:
            state = await pg.evaluate(
                "() => JSON.stringify({"
                " title: document.title.slice(0,60),"
                " hasLoginLink: [...document.querySelectorAll('a')].some(a => /登录|登陆/.test(a.innerText||'') && (a.offsetHeight||0)>0),"
                " bodyHead: (document.body?.innerText||'').slice(0, 300)"
                "})"
            )
            print(" ", state[:900])
        except Exception as exc:  # noqa: BLE001
            print("  evaluate 失败:", exc)
    await browser_manager.shutdown()


asyncio.run(main())