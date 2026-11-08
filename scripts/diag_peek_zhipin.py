"""临时诊断:列出 Playwright 视角全部 context 与标签(只读,不导航)。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402


async def main() -> None:
    browser = await browser_manager.ensure()
    print(f"contexts: {len(browser.contexts)}")
    for ci, ctx in enumerate(browser.contexts):
        print(f"\n== context[{ci}] pages={len(ctx.pages)} ==")
        for i, p in enumerate(ctx.pages):
            try:
                title = (await p.title())[:40]
            except Exception:  # noqa: BLE001
                title = "?"
            print(f"  [{i}] {p.url[:110]} | {title}")
    await browser_manager.shutdown()


asyncio.run(main())