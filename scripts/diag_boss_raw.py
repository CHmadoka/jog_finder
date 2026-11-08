"""Boss(zhipin) 原始数据留存实测:只读探测 → 1 次列表接口 → (可选)1 个详情页。

目的:验证 save_raw 在 Boss 平台 search / detail 两条路径端到端可用,同时把
平台暴露压到最小——1 次免疫 reload + 1 次列表接口 + 1 次详情导航,不翻页、
不并发、不遍历。受限/验证特征命中即刻中止,不再发任何请求。

用法:
  .venv\\Scripts\\python scripts\\diag_boss_raw.py            # 只测 search
  .venv\\Scripts\\python scripts\\diag_boss_raw.py --detail   # search + 1 个详情
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
from app.platforms.selectors import BOSS  # noqa: E402

KW, CITY = "Java", "上海"

PROBE_JS = r"""(() => {
  const t = document.body ? (document.body.innerText || '') : '';
  return JSON.stringify({
    url: location.href.slice(0, 140),
    title: (document.title || '').slice(0, 80),
    avatar: !!document.querySelector('.nav-figure, .nav-figure-wrapper, [ka=header-username]'),
    loginBtn: !!document.querySelector('[ka=header-login], .header-login-btn, a[href*="login"]'),
    restricted: /访问受限|访问过于频繁|请求过于频繁|操作过于频繁/.test(t),
    verify: /安全验证|拖动滑块/.test(t),
    textLen: t.length
  });
})()"""


def raw_files() -> set[Path]:
    d = config.RAW_DIR / "boss"
    return set(d.glob("*.json")) if d.is_dir() else set()


async def main() -> None:
    do_detail = "--detail" in sys.argv
    plat = get_platform("boss")
    before = raw_files()

    await browser_manager.ensure()
    page = await browser_manager.get_or_open_page(
        plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker
    )
    print(f"复用页面(未导航): {page.url[:110]}")

    # ---- 只读探测(零平台请求):登录态 + 风控特征 ----
    st = json.loads(await page.evaluate(PROBE_JS))
    print("只读探测:", json.dumps(st, ensure_ascii=False))
    if st["restricted"] or st["verify"]:
        print("!! 当前页面已呈风控特征,立即停止,不发任何请求")
        return
    if st["loginBtn"] and not st["avatar"]:
        print("!! 未登录(检测到登录入口),停止")
        return
    print(f"登录态: avatar={st['avatar']} loginBtn={st['loginBtn']}")

    # ---- 1 次列表接口(页面保持静止,不 navigate) ----
    city_code = BOSS["city_codes"].get(CITY, "")
    res = await plat._fetch_joblist(page, KW, city_code, 1)
    code = res.get("code")
    items = res.get("list") or []
    print(f"列表接口: code={code} 条数={len(items)} hasMore={res.get('hasMore')}")
    if code != 0:
        print("接口未返回 0(拒绝形态已随 raw 留档),停止,不再发请求")
        return
    if not items:
        print("列表为空,停止")
        return
    if not do_detail:
        print("(未加 --detail,未打开详情页)")
    else:
        # ---- 1 个详情页(带列表来路 referer,模拟真人点击) ----
        map_ = await plat._decode_map(page)
        draft = None
        for x in items:
            d, warns = plat._draft_from_api(x, map_, CITY)
            for w in warns:
                print(f"  [条目警告] {w}")
            if d:
                draft = d
                break
        if draft is None:
            print("列表条目均不可用,无法测详情")
            return
        print(f"详情目标: {draft.title} | {draft.company} | {draft.salary}")
        plat._search_ctx = {
            "referer": "https://www.zhipin.com/web/geek/job?query=Java&city=" + city_code
        }
        dp = await page.context.new_page()
        try:
            await plat._parse_detail(dp, draft, should_stop=lambda: False, progress=print)
        finally:
            try:
                await dp.close()
            except Exception:  # noqa: BLE001
                pass
        print(
            f"详情解析: desc={len(draft.description or '')}字"
            f" hr={draft.hr_name}/{draft.hr_title}"
            f" act={draft.hr_active_text or '-'}"
            f" raw_flags={[k for k in draft.raw if k in ('offline', 'blanked', '_warnings')]}"
        )

    # ---- 本次新增的留存文件 ----
    new = sorted(raw_files() - before)
    print(f"\n本次留存 {len(new)} 份:")
    for p in new:
        env = json.loads(p.read_text(encoding="utf-8"))
        body = env.get("body")
        if env["kind"] == "search":
            desc = f"code={body.get('code')} 条数={len(body.get('list') or [])}"
        else:
            ev = (body or {}).get("evaluate") or {}
            desc = (
                f"secs={len(ev.get('secs') or [])} body={len(ev.get('body') or '')}字"
                f" decodeMap={len((body or {}).get('decodeMap') or {})}项"
            )
        print(f"  {p.relative_to(config.DATA_DIR)}  {p.stat().st_size} B  {desc}")


asyncio.run(main())
