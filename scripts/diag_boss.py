"""诊断 Boss 登录态:列出标签页 + 接口探测(带/不带城市)。"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser import browser_manager  # noqa: E402


async def main() -> None:
    browser = await browser_manager.ensure()
    ctx = browser_manager.primary_context()
    print("== 标签页列表 ==")
    for i, p in enumerate(ctx.pages):
        print(f"  [{i}] {p.url[:100]}")

    zp = None
    for p in ctx.pages:
        if "zhipin.com" in p.url:
            zp = p
            break
    if not zp:
        zp = await ctx.new_page()
        await zp.goto("https://www.zhipin.com/", wait_until="domcontentloaded")
        await zp.wait_for_timeout(3000)
    print("\n== Boss Tab URL ==")
    print(" ", zp.url[:150])

    for city in ("", "101200100"):
        js = (
            "fetch('/wapi/zpgeek/search/joblist.json?scene=1&query="
            + ("Python" if not city else "Python&city=" + city)
            + "&page=1&pageSize=1',{headers:{'accept':'application/json'}})"
            ".then(r=>r.json()).then(j=>JSON.stringify({code:j.code, msg:(j.message||'').slice(0,60), n:((j.zpData||{}).jobList||[]).length}))"
        )
        try:
            res = await zp.evaluate(js)
            print(f"city={city!r:15} -> {res}")
        except Exception as exc:  # noqa: BLE001
            print(f"city={city!r:15} -> 异常 {exc}")

    state = await zp.evaluate(
        "() => JSON.stringify({title: document.title.slice(0,50),"
        " hasLoginBtn: !!document.querySelector('[ka=header-login], .header-login-btn'),"
        " fig: (document.querySelector('.nav-figure')?.innerText || '').slice(0,30)})"
    )
    print("\n== 页面状态 ==")
    print(" ", state)
    await browser_manager.shutdown()


asyncio.run(main())