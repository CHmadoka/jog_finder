"""Boss直聘(zhipin.com)适配器 —— 基于 2026-11 实测版本(参考 boss-search 实例脚本)。

要点:
- 列表不走 DOM:在 zhipin.com 页面上下文内直接 fetch 内部接口
  /wapi/zpgeek/search/joblist.json(带登录态、返回明文结构化数据,含行业/HR在线)
- 薪资等数字可能被 kanzhun-mix 字体 PUA 码位混淆:页面内 canvas 字形模板匹配解码
- 详情 URL 必须携带 securityId/lid,否则会被重定向到其他职位;落地页需校验 jobId
- 触发安全验证时等待人工处理(最多 120 秒)
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import AsyncIterator
from urllib.parse import quote

from playwright.async_api import Page, TimeoutError as PWTimeout

from ..utils import (
    RETRY_ATTEMPTS,
    active_estimate_from_text,
    detail_delay,
    global_nav_gate,
    human_delay,
    human_read,
    human_scroll,
    interruptible_sleep,
    page_responsive,
    patient_call,
    patient_poll,
    save_raw,
    save_snapshot,
    warmup,
)
from . import register
from .base import AntiBot, BasePlatform, BudgetExhausted, JobDraft, LoginRequired
from .common import parse_salary_months, salary_in_range
from .selectors import BOSS

logger = logging.getLogger(__name__)

# 详情页/搜索页通用的验证特征(实测:标题或正文前段出现即命中)
# 标题只认"安全验证/安全挑战":正常岗位标题可能含"验证码"(安全类岗位),
# 不能作为验证页依据;正文特征保留全词表但要求正文足够短(真验证页仅标题+滑块)
_VERIFY_TITLE_RE = re.compile(r"安全验证|安全挑战")
_VERIFY_TEXT_RE = re.compile(r"安全验证|安全挑战|拖动滑块|验证码")
# 访问受限/限流页:高频访问后 Boss 把详情与列表接口一并重定向到
# /web/passport/zp/403.html?code=32(页面文案"访问受限")。与"安全验证"不同:
# 滑块解决不了限流,必须立即停止全部导航,否则每条岗位都会撞一次受限页
# (实测:被限流后每 ~5 秒刷新一次受限页,持续加重风控)。
_RESTRICTED_URL_RE = re.compile(r"/web/passport/zp/403\.html|403\.html\?code=")
_RESTRICTED_TEXT_RE = re.compile(r"访问受限|访问过于频繁|请求过于频繁|操作过于频繁")
_RESTRICTED_MSG = "访问受限(限流),已中止;请等 30 分钟并在浏览器确认可打开职位页后再重试"
# 详情页结构特征:存在这些区块即说明岗位内容正常渲染,绝非验证页
_DETAIL_DOM_RE = ".job-sec-text, .job-banner, .job-scribe, .detail-content"
# HR 活跃文案(实测详情页名片区)
_ACT_RE = re.compile(
    r"(刚刚活跃|今日活跃|昨日活跃|本周活跃|近两周活跃|本月活跃|上月活跃"
    r"|\d+日内活跃"
    r"|三个月前活跃|半年前活跃|长期未活跃|长期本站活跃"
    r"|\d+分钟前活跃|\d+小时前活跃|\d+天前活跃|\d+个月?前活跃)"
)

# 页面内 canvas 字形模板匹配:PUA 码位 → 数字(font.mjs 移植)
PAGE_DECODE_JS = r"""(() => {
  try {
    const body = document.body ? document.body.innerText : '';
    const puas = [...new Set([...body].map(c => c.codePointAt(0)).filter(c => c >= 0xE000 && c <= 0xF8FF))];
    if (!puas.length) return JSON.stringify({ map: {} });
    const cv = document.createElement('canvas');
    cv.width = 64; cv.height = 64;
    const ctx = cv.getContext('2d', { willReadFrequently: true });
    function sig(ch, font) {
      ctx.clearRect(0, 0, 64, 64);
      ctx.fillStyle = '#000';
      ctx.font = '48px ' + font;
      ctx.textBaseline = 'middle';
      ctx.textAlign = 'center';
      ctx.fillText(ch, 32, 36);
      const data = ctx.getImageData(0, 0, 64, 64).data;
      const px = [];
      for (let i = 0; i < data.length; i += 4) px.push(data[i + 3] > 100 ? 1 : 0);
      let minX = 64, minY = 64, maxX = -1, maxY = -1;
      for (let y = 0; y < 64; y++) for (let x = 0; x < 64; x++) if (px[y * 64 + x]) {
        if (x < minX) minX = x;
        if (x > maxX) maxX = x;
        if (y < minY) minY = y;
        if (y > maxY) maxY = y;
      }
      if (maxX < 0) return null;
      const w = maxX - minX + 1, h = maxY - minY + 1, g = [];
      for (let yy = 0; yy < 16; yy++) for (let xx = 0; xx < 16; xx++) {
        g.push(px[(minY + Math.floor(yy * h / 16)) * 64 + (minX + Math.floor(xx * w / 16))] ? 1 : 0);
      }
      return g;
    }
    const refs = {};
    for (let d = 0; d <= 9; d++) refs[d] = sig(String(d), 'Arial, "Microsoft YaHei", sans-serif');
    const map = {}, conf = {};
    for (const p of puas) {
      const s = sig(String.fromCodePoint(p), 'kanzhun-mix, kanzhun-Regular');
      if (!s) continue;
      let best = -1, bestScore = -1;
      for (let d = 0; d <= 9; d++) {
        let sc = 0;
        for (let i = 0; i < 256; i++) if (s[i] === refs[d][i]) sc++;
        sc /= 256;
        if (sc > bestScore) { bestScore = sc; best = d; }
      }
      map[p] = best;
      conf[p] = Math.round(bestScore * 100) / 100;
    }
    return JSON.stringify({ map, conf });
  } catch (e) {
    return JSON.stringify({ map: {}, err: String(e) });
  }
})()"""


def decode_text(s, map_) -> str:
    """按 map 把 PUA 码位替换为对应数字。"""
    if not s or not map_:
        return s or ""
    out = []
    for ch in str(s):
        c = ord(ch)
        out.append(str(map_[c]) if (0xE000 <= c <= 0xF8FF and c in map_) else ch)
    return "".join(out)


class BossPlatform(BasePlatform):
    id = "boss"
    name = "Boss直聘"
    home_url = "https://www.zhipin.com"
    home_prefix = "https://www.zhipin.com"
    detail_url_marker = "/job_detail/"

    def supported_cities(self) -> set[str]:
        return set(BOSS["city_codes"]) - {"全国"}

    # ---------- 接口辅助 ----------
    @staticmethod
    def _joblist_js(query: str, city: str, page_num: int, page_size: int = 30) -> str:
        city_part = f"&city={quote(city)}" if city else ""
        return (
            "fetch('/wapi/zpgeek/search/joblist.json?scene=1&query="
            + quote(query, safe="")
            + city_part
            + f"&page={page_num}&pageSize={page_size}"
            + "', {headers: {'accept': 'application/json'}})"
            ".then(r => r.json())"
            ".then(j => JSON.stringify({code: j.code, hasMore: (j.zpData||{}).hasMore,"
            " list: ((j.zpData||{}).jobList||[]).map(x => ({"
            "id: x.encryptJobId, securityId: x.securityId, lid: x.lid,"
            " jobName: x.jobName, salaryDesc: x.salaryDesc,"
            " jobLabels: x.jobLabels, skills: x.skills,"
            " jobExperience: x.jobExperience, jobDegree: x.jobDegree,"
            " cityName: x.cityName, areaDistrict: x.areaDistrict, businessDistrict: x.businessDistrict,"
            " brandName: x.brandName, brandIndustry: x.brandIndustry,"
            " brandStageName: x.brandStageName, brandScaleName: x.brandScaleName,"
            " welfareList: x.welfareList, bossName: x.bossName, bossTitle: x.bossTitle, bossOnline: x.bossOnline"
            " }))}))"
        )

    async def _fetch_joblist(self, page: Page, query: str, city: str, page_num: int) -> dict:
        """页内 fetch 接口拿列表数据。

        - 页面内 JSON.stringify 返回 str,Playwright evaluate 拿到的也是 str,需 json.loads;
        - 意外等待静默重试:瞬态异常(页面正在导航等)静默等待 5~10 秒后原样重试,
          最多 3 次(全程不刷新页面),3 次仍失败返回 {} 由调用方按接口不可用中止;
        - **接口返回拒绝码(code!=0)绝不重试连发**——那正是触发 _security_check
          风控重定向循环的原因,由调用方按错误类型处理。
        """
        import json as _json

        async def _once():
            # 全局导航闸:多平台并发时控制叠加请求率(同 IP 总频率)
            await global_nav_gate()
            raw = await page.evaluate(self._joblist_js(query, city, page_num))
            if isinstance(raw, str):
                raw = _json.loads(raw)
            if isinstance(raw, dict) and raw.get("code") is not None:
                return raw     # 含拒绝码:如实返回(不重试),由调用方判定
            raise RuntimeError(f"响应结构异常: {type(raw).__name__}")

        ok, raw = await patient_call(_once)
        if not ok:
            logger.warning(
                "Boss joblist fetch 静默重试 %d 次仍失败: %s", RETRY_ATTEMPTS, str(raw)[:120]
            )
            return {}
        # 原始数据留存:成功与拒绝码形态都存(拒绝码正是风控排查样本)
        if isinstance(raw, dict):
            save_raw(self.id, "search", raw, {
                "keyword": query, "city": city, "page": page_num,
                "url": (page.url or "")[:300],
            })
        return raw

    async def _decode_map(self, page: Page) -> dict:
        try:
            data = await page.evaluate(PAGE_DECODE_JS)
            return (data or {}).get("map") or {}
        except Exception:
            return {}

    # ---------- 域名载体 ----------
    async def _ensure_on_domain(self, page: Page) -> tuple[bool, Page]:
        """确保页面落在 zhipin.com 域且加载稳定,返回 (是否成功, 可用页面)。

        处理三种情况:tab 被用户关闭 / 首页加载慢(自动跳城市页)/
        浏览器实例病态(全部 tab 拒绝导航 → 自动重启浏览器自愈,登录态保留)。
        """
        from ..browser import browser_manager

        cur = page
        for rnd in range(2):
            if "zhipin.com" in cur.url:
                try:
                    await cur.wait_for_load_state("networkidle", timeout=6000)
                except Exception as exc:  # noqa: BLE001
                    if "closed" in str(exc).lower():
                        cur = await browser_manager.get_or_open_page(self.home_prefix, self.home_url)
                        continue
                if "zhipin.com" in cur.url:
                    return True, cur
            try:
                await cur.goto(self.home_url, wait_until="domcontentloaded", timeout=25000)
                await cur.wait_for_timeout(2500)
            except Exception as exc:  # noqa: BLE001
                if "closed" in str(exc).lower():
                    # Playwright 持有的是废弃 target:重新发现有效页面
                    try:
                        cur = await browser_manager.get_or_open_page(self.home_prefix, self.home_url)
                        continue
                    except Exception:  # noqa: BLE001
                        pass
            if "zhipin.com" in cur.url:
                return True, cur
            if rnd == 0:
                logger.warning("浏览器实例疑似病态(导航无效),自动重启浏览器自愈...")
                await browser_manager.restart()
                ctx = browser_manager.primary_context()
                cur = ctx.pages[0] if ctx.pages else await ctx.new_page()
            else:
                await asyncio.sleep(2)
        return "zhipin.com" in cur.url, cur

    # ---------- 登录态 ----------
    async def is_logged_in(self, page: Page) -> bool:
        ok, page = await self._ensure_on_domain(page)
        if not ok:
            raise AntiBot("页面未能加载,已中止;请在浏览器手动打开 zhipin.com 确认后重试")
        if "/web/user/?ka=header-login" in page.url or re.search(r"login", page.url):
            return False
        # 以页面登录 UI 为准:游客态搜索接口也可能返回 code=0,不能作为已登录依据
        try:
            state = await page.evaluate(
                "() => JSON.stringify({"
                " login: !!document.querySelector('[ka=header-login], .header-login-btn, a[href*=\"login\"]'),"
                " avatar: !!document.querySelector('.nav-figure, .nav-figure-wrapper, [ka=header-username]')}) "
            )
            import json as _json

            st = _json.loads(state)
            if st.get("avatar"):
                return True
            if st.get("login"):
                return False
        except Exception:  # noqa: BLE001
            pass
        # 页面 UI 不可判时再用接口 code 兜底
        try:
            res = await self._fetch_joblist(page, "Python", "", 1)
            return res.get("code") == 0
        except Exception:
            return False

    # ---------- 风控 ----------
    @staticmethod
    def _is_restricted_url(url: str) -> bool:
        return bool(_RESTRICTED_URL_RE.search(url or ""))

    async def _raise_if_restricted(self, page: Page) -> None:
        """命中"访问受限"页立即抛 AntiBot(调用方中止整个任务,不再发任何请求)。

        判定依据(依次):URL 落在 403 限流页 / 标题含受限文案 / 正文前段含受限
        文案(正文仅取前 300 字,避免岗位描述里偶发的同词误伤)。
        """
        if self._is_restricted_url(page.url or ""):
            raise AntiBot(_RESTRICTED_MSG)
        try:
            title = (await page.title()) or ""
            if _RESTRICTED_TEXT_RE.search(title):
                raise AntiBot(_RESTRICTED_MSG)
            text = (await page.evaluate("() => (document.body?.innerText||'').slice(0, 300)")) or ""
            if _RESTRICTED_TEXT_RE.search(text):
                raise AntiBot(_RESTRICTED_MSG)
        except AntiBot:
            raise
        except Exception:  # noqa: BLE001  (页面导航瞬间 evaluate 可能抛错,忽略)
            pass

    async def _is_verify_page(self, page: Page) -> bool:
        try:
            u = page.url or ""
            if "_security_check" in u or "security-check" in u or "/verify" in u:
                return True
            title = await page.title()
            if _VERIFY_TITLE_RE.search(title or ""):
                return True
            # 详情页结构仍在 → 岗位内容正常渲染,不是验证页
            # (验证页只替换内容不改URL,但不会伪造详情页区块 DOM)
            if await page.query_selector(_DETAIL_DOM_RE):
                return False
            text = (await page.evaluate("() => (document.body?.innerText||'').slice(0,3000)")) or ""
            # 正文误报防线:正常岗位详情可能含"验证码"等词(如 AI 类岗位描述),
            # 而真验证页正文极少(仅标题+滑块)。要求内容足够短才凭文本判定。
            return bool(_VERIFY_TEXT_RE.search(text)) and len(text) < 800
        except Exception:
            return "verify" in page.url

    async def _wait_manual_verify(self, page: Page, progress=None, should_stop=None) -> None:
        """普通人机验证(滑块/验证码):强提醒 + 持续静默等待人工完成。

        每 5 秒**只读**轮询一次页面是否已通过验证(读取 URL/标题/DOM,不产生任何
        平台请求),绝不刷新页面、绝不重新导航;验证通过后任务自动继续。
        用户点「停止抓取」可随时退出等待(调度器按手动停止收尾,不报错)。
        """
        from ..utils import set_verify_wait
        set_verify_wait(self.id, True)
        try:
            if progress:
                progress("⚠ 人机验证:请在浏览器完成滑块/验证码,通过后自动继续(每5秒检测,不刷新页面)")
            while True:
                if should_stop is not None and should_stop():
                    raise AntiBot("用户在等待验证时停止任务")
                await asyncio.sleep(5)
                try:
                    if not await self._is_verify_page(page):
                        # 验证通过后先冷却再继续:刚通过验证的会话被平台重点观察,
                        # 立即恢复高频抓取极易二次触发
                        cooldown = random.uniform(20, 40)
                        if progress:
                            progress(f"验证已通过,冷却 {cooldown:.0f}s 后继续")
                        await interruptible_sleep(cooldown, should_stop)
                        if progress:
                            progress("冷却完成,继续抓取")
                        return
                except Exception:  # noqa: BLE001  (渲染中的瞬态异常:继续静默观察)
                    pass
        finally:
            set_verify_wait(self.id, False)

    # ---------- 列表(接口直连) ----------
    def _draft_from_api(self, x: dict, map_: dict, city: str) -> tuple[JobDraft | None, list[str]]:
        """解析单条列表数据 → (draft, 警告列表)。draft 为 None 表示条目不可用。"""
        warns: list[str] = []
        title = decode_text(x.get("jobName") or "", map_)
        jid = str(x.get("id") or "")
        if not title or not jid:
            if not jid:
                warns.append("列表条目缺少 encryptJobId,该岗位无法入库(平台接口可能已改版)")
            if not title:
                warns.append("列表条目缺少 jobName(职位名),该岗位已跳过(平台接口可能已改版)")
            return None, warns
        labels = x.get("jobLabels") or []
        labels = [decode_text(t, map_) for t in labels if t]
        salary = decode_text(x.get("salaryDesc") or "", map_)
        area = decode_text(x.get("areaDistrict") or "", map_)
        bd = decode_text(x.get("businessDistrict") or "", map_)
        welfare = [decode_text(w, map_) for w in (x.get("welfareList") or []) if w]
        active = "在线" if x.get("bossOnline") else ""
        # 字段缺失警告:平台接口/文本变化时在左下角异常面板持久提示
        if "bossOnline" not in x:
            warns.append("列表接口未返回 bossOnline 字段,HR在线状态无法判定(平台可能已改版)")
        url = f"https://www.zhipin.com/job_detail/{jid}.html"
        security_id = str(x.get("securityId") or "")
        lid = str(x.get("lid") or "")
        if not security_id:
            warns.append("列表接口未返回 securityId,详情直链可能被重定向/风控(平台可能已改版)")
        return JobDraft(
            platform_job_id=jid,
            title=title,
            url=url,
            open_params={"securityId": security_id, "lid": lid},
            company=decode_text(x.get("brandName") or "", map_),
            salary=salary,
            salary_months=parse_salary_months(salary),
            city=decode_text(x.get("cityName") or "", map_) or city,
            district=area or bd,
            experience=decode_text(x.get("jobExperience") or "", map_),
            education=decode_text(x.get("jobDegree") or "", map_),
            company_stage=decode_text(x.get("brandStageName") or "", map_),
            company_scale=decode_text(x.get("brandScaleName") or "", map_),
            welfare="、".join(welfare),
            hr_online=bool(x.get("bossOnline")),
            description="",
            hr_name=decode_text(x.get("bossName") or "", map_),
            hr_title=decode_text(x.get("bossTitle") or "", map_),
            industry=decode_text(x.get("brandIndustry") or "", map_),
            raw={
                "hr_active_text": active,
                "securityId": x.get("securityId") or "",
                "lid": x.get("lid") or "",
                "labels": labels,
                "_warnings": warns,
            },
        ), warns

    # ---------- 详情 ----------
    async def _parse_detail(self, page: Page, draft: JobDraft, should_stop=None, progress=None) -> None:
        # 会话预算:详情额度耗尽即优雅收尾(补全/修复路径无预算,不受影响)。
        # 单登录态短时打开大量详情会显著偏离账户基线——这是 403 限流的主因之一。
        budget = self._budget
        if budget is not None:
            if budget.details_left <= 0:
                raise BudgetExhausted(f"详情页预算耗尽(上限 {budget.details} 个)")
            budget.take_detail()
        # 详情 URL 带 securityId/lid,防止 Boss 重定向到其他职位
        u = draft.url
        params = []
        if draft.raw.get("securityId"):
            params.append("securityId=" + quote(str(draft.raw["securityId"]), safe=""))
        if draft.raw.get("lid"):
            params.append("lid=" + quote(str(draft.raw["lid"]), safe=""))
        if params:
            u = u + ("&" if "?" in u else "?") + "&".join(params)

        # 详情页节奏:随机间隔(任务级设置,默认 3~8 秒)。固定约 1s 的均匀高频访问
        # 是 Boss 限流(code=32 访问受限)的主因,拉大并随机化间隔模拟人工浏览
        await detail_delay(self.id)
        # 危险率长休息:模拟真人起身离开(无记忆触发,无固定批长元周期)
        await self._detail_pacer.tick(self.id, progress=progress, should_stop=should_stop)
        # 全局导航闸:多平台并发时控制叠加请求率
        await global_nav_gate()
        # 用户已请求停止:不再发起任何导航(停止指令即时生效)
        if should_stop is not None and should_stop():
            return
        # 真人打开详情来自列表页点击:带上"列表来路" referer(100% 直达入口是强自动化签名)
        referrer = (self._search_ctx or {}).get("referer") or self.home_url
        t0 = time.monotonic()
        try:
            await page.goto(u, wait_until="domcontentloaded", timeout=25000, referrer=referrer)
        except Exception:  # noqa: BLE001  (超时/导航中断均由后置 URL 轮询兜底)
            pass
        # 访问受限第一时间判定:立即中止整个任务。绝不能当"岗位已下线"继续
        # 下一条——那会对受限页连续导航几十次(实测被限流后每 5 秒撞一次)
        await self._raise_if_restricted(page)
        # 静默观察落地(意外等待不刷新页面):每轮静默 5~10 秒后检查一次,
        # 3 轮不落地才定论;受限特征命中时立即中止任务
        landed = draft.platform_job_id in page.url

        async def _landed() -> bool:
            if self._is_restricted_url(page.url or ""):
                raise AntiBot(_RESTRICTED_MSG)
            return draft.platform_job_id in page.url

        if not landed:
            landed = await patient_poll(_landed, should_stop=should_stop)
        if not landed:
            # 正文型受限页(URL 未变)兜底判定,再走原验证页判定
            await self._raise_if_restricted(page)
            if await self._is_verify_page(page):
                await self._wait_manual_verify(page, progress=progress, should_stop=should_stop)
                landed = draft.platform_job_id in page.url
        if not landed:
            # 静默观察 3 轮仍不落地,分级处理:
            # - 页面无响应/被清空 → 风控未识别或中断,自动中止任务(防止对异常状态连发);
            # - 平台正常响应但岗位失效(重定向到列表/首页)→ 视为已下线,跳过。
            # 全程不做二次导航硬闯(反复刷新加重风控)
            if "about:blank" in (page.url or "") or not await page_responsive(page):
                raise AntiBot("详情页无响应(疑似风控),已中止;请检查浏览器后重试")
            logger.warning("Boss 详情落地页异常(视为已下线,跳过): %s -> %s", draft.title, page.url[:120])
            draft.raw["offline"] = True
            return
        # 快轮询等活跃文案/HR名片渲染完成(免疫补丁生效后页面稳定,通常 <2s;
        # 部分岗位 SSR 不带 HR 信息,等满上限即放弃)。0.4s 间隔抢在平台反调试
        # 可能的清理动作之前拿到完整数据。
        for _ in range(12):
            if should_stop is not None and should_stop():
                return
            try:
                has = await page.evaluate(
                    "() => { const t = (document.body?.innerText || '');"
                    " return t.includes('活跃') || !!document.querySelector('.job-scribe')"
                    " || /职位描述|岗位职责|工作职责/.test(t.slice(0, 3000)); }"
                )
            except Exception:  # noqa: BLE001  (导航瞬间 evaluate 可能抛错)
                has = False
            if has or "about:blank" in (page.url or ""):
                break
            await asyncio.sleep(0.4)
        if "about:blank" in (page.url or "") or not (await page.title()).strip():
            # 页面被平台反调试清空:免疫补丁之外的兜底检测,避免拿空数据入库
            logger.warning("Boss 详情页被清空(反自动化拦截): %s", draft.title)
            draft.raw["blanked"] = True
            return
        await self._raise_if_restricted(page)
        if await self._is_verify_page(page):
            await self._wait_manual_verify(page, progress=progress, should_stop=should_stop)
        try:
            data = await page.evaluate(
                """() => ({
                    secs: [...document.querySelectorAll('.job-sec-text, .detail-content')].map(e => (e.innerText||'').slice(0, 6000)),
                    head: (document.querySelector('.job-banner')?.innerText || '').slice(0, 600),
                    body: (document.body?.innerText || '').slice(0, 9000),
                    keywords: [...document.querySelectorAll('.job-keyword-list li, .job-tags li, .tag-list li')].map(e => (e.innerText||'').trim()).filter(Boolean),
                })"""
            ) or {}
            map_ = await self._decode_map(page)
            # 原始数据留存:解码前的 evaluate 产物与字形解码映射——薪资乱码/字段
            # 缺失类问题直接翻文件定位,不必重开详情页复测
            save_raw(self.id, "detail", {"evaluate": data, "decodeMap": map_}, {
                "job_id": draft.platform_job_id, "url": draft.url or "",
                "page_url": (page.url or "")[:300],
            })
            secs = [decode_text(s, map_) for s in (data.get("secs") or [])]
            head = decode_text(data.get("head") or "", map_)
            body = decode_text(data.get("body") or "", map_)
            kws = [decode_text(k, map_) for k in (data.get("keywords") or [])]
            if secs:
                draft.description = "\n\n".join(s for s in secs if s.strip())
            if kws:
                draft.description = (draft.description or "") + "\n技能标签: " + "、".join(kws)
            # HR 活跃:名片区优先,全页面文本兜底(对齐实例 detail.mjs)
            # 存储规则:原始文案 + 统一估算字符串(后缀携带方向),不伪造精确时间
            m = _ACT_RE.search(head) or _ACT_RE.search(body)
            if m:
                if not draft.hr_active_at:
                    draft.hr_active_text = m.group(1)
                    draft.hr_active_at = active_estimate_from_text(m.group(1))
            else:
                draft.raw.setdefault("_warnings", []).append(
                    "详情页未解析到HR活跃文案(平台文本可能已变化)"
                )
            if not (draft.description or "").strip():
                draft.raw.setdefault("_warnings", []).append(
                    "详情页未解析到职位描述(页面结构可能已变化)"
                )
            if not draft.hr_title:
                m2 = re.search(r"([\u4e00-\u9fa5A-Za-z]{2,6}(?:HR|人事|经理|主管|总监|专员|顾问))", head)
                if m2:
                    draft.hr_title = m2.group(1)[:30]
        except Exception as exc:  # noqa: BLE001
            logger.debug("Boss 详情解析失败: %s", exc)
            draft.raw.setdefault("_warnings", []).append(
                f"详情页数据提取异常,部分字段未写入: {type(exc).__name__}: {str(exc)[:120]}"
            )
            if draft.url:
                try:
                    save_snapshot("boss", "detail_parse_fail", (await page.content())[:200000])
                except Exception:
                    pass
        # 阅读仿真:停留∝描述长度(落地校验/解析耗时已计入,只补差额),
        # 滚动浏览后再离开——零阅读访问(停留与内容无关)是强自动化签名
        try:
            await human_read(page, draft.description or draft.title or "", t0=t0)
        except Exception:  # noqa: BLE001
            pass

    async def rebuild_open_params(self, page: Page, jid2kw: dict[str, str], progress=None) -> dict[str, dict]:
        """翻列表接口,为旧岗位重建 securityId/lid(防详情直链被重定向)。

        按旧数据的抓取关键词逐词翻页(最多5页/词),命中即收集。
        """
        ok_onsite, page = await self._ensure_on_domain(page)
        if not ok_onsite:
            raise AntiBot("页面未能加载,无法重建岗位参数;请手动打开 zhipin.com 后重试")
        out: dict[str, dict] = {}
        kws = sorted({(k or "").strip() for k in jid2kw.values() if (k or "").strip()})
        for kw in kws:
            for page_num in range(1, 6):
                if all(jid in out for jid in jid2kw):
                    return out
                if progress:
                    progress(f"重建岗位参数: 「{kw}」第 {page_num} 页")
                res = await self._fetch_joblist(page, kw, "", page_num)
                if not res or res.get("code") != 0:
                    break
                for x in res.get("list") or []:
                    jid = str(x.get("id") or "")
                    if jid in jid2kw and jid not in out:
                        out[jid] = {
                            "securityId": str(x.get("securityId") or ""),
                            "lid": str(x.get("lid") or ""),
                        }
                if not res.get("hasMore", True):
                    break
                await human_delay(self.id)
        return out

    # ---------- 主流程 ----------
    async def iter_jobs(
        self,
        page: Page,
        *,
        city: str,
        keyword: str,
        extras: dict,
        max_pages: int,
        detail_page: Page | None = None,
        progress=None,
        skip_check=None,
        warn=None,
        should_stop=None,
    ) -> AsyncIterator[JobDraft]:
        own_detail = detail_page is None
        # 详情标签页懒创建:全部岗位都已入库时不开任何新标签
        # (预创建会立刻弹出一个空白标签页,且纯查重场景下全程空白)
        dp = detail_page
        city_code = BOSS["city_codes"].get(city, "") if city else ""
        sal_rng = (extras or {}).get("salary") or ""
        # 任务级会话上下文:请求预算 + "列表来路"(详情 referer 模拟真人点击链)
        budget = (extras or {}).get("budget")
        self._budget = budget
        self._search_ctx = {
            "referer": "https://www.zhipin.com/web/geek/job?query="
                       + quote(keyword or "", safe="")
                       + (f"&city={city_code}" if city_code else "")
        }
        # PV 配平倒计时:每 3~6 个接口页把载体页真实导航一次(打断"纯 API 会话"形状)
        pv_left = random.randint(3, 6)
        try:
            # 页面主要作为 cookie 载体:列表数据走页内接口 fetch,不长期停留在
            # 搜索结果页(其自身轮询与我们的 fetch 叠加易触发风控);但每 3~6 页
            # 会真实导航回首页一次做"PV 配平"(见下)
            ok_onsite, page = await self._ensure_on_domain(page)
            if not ok_onsite:
                raise AntiBot("页面未能加载(已自动重启浏览器),已中止;请手动打开 zhipin.com 后重试")
            if progress:
                progress(f"接口直连采集「{keyword}」@{city or '全国'}(浏览器页面保持静止)")
            # 启动预热:静默停留 + 轻滚动,避免任务启动瞬间即高频发请求
            await warmup(page, self.id)
            if budget is not None and budget.pages_left <= 0:
                raise BudgetExhausted(f"列表页预算耗尽(上限 {budget.pages} 页)")
            res = await self._fetch_joblist(page, keyword or "数据", city_code, 1)
            if not res or res.get("code") != 0:
                raise AntiBot(
                    f"接口不可用(code={res.get('code') if res else '无响应'},疑似限流或需安全验证),已中止;请检查浏览器后重试"
                )
            if budget is not None:
                budget.take_page()
            # 探测页与正式第 1 页同参数(有词条时):复用结果,省一次请求与一次暴露
            reuse_first = bool(keyword)

            map_ = await self._decode_map(page)
            seen: set[str] = set()
            skipped_known = 0
            for page_num in range(1, max(1, max_pages) + 1):
                if should_stop is not None and should_stop():
                    return
                if page_num > 1:
                    await human_delay(self.id)
                    # 危险率长休息:模拟真人起身离开(无记忆触发,无固定批长元周期)
                    await self._page_pacer.tick(self.id, progress=progress, should_stop=should_stop)
                    # PV 配平:每 3~6 个接口页把载体页真实导航回首页一次。真人浏览的
                    # API 是页面浏览的副产品,"PV≈0 而接口数百次"的纯 API 会话是
                    # 风控强判定信号;首页是"安静页"(无搜索接口轮询),不与 fetch 叠加
                    pv_left -= 1
                    if pv_left <= 0:
                        pv_left = random.randint(3, 6)
                        if progress:
                            progress("回首页浏览片刻(配平会话形状)")
                        try:
                            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
                            await page.wait_for_timeout(random.randint(4000, 9000))
                            await human_scroll(page, 2)
                        except Exception:  # noqa: BLE001
                            pass
                if progress:
                    progress(f"接口拉取第 {page_num} 页: {keyword}@{city or '全国'}")
                if page_num == 1 and reuse_first:
                    pass                      # 复用探测页结果(同参数,不重复请求)
                else:
                    if budget is not None and budget.pages_left <= 0:
                        raise BudgetExhausted(f"列表页预算耗尽(上限 {budget.pages} 页)")
                    res = await self._fetch_joblist(page, keyword, city_code, page_num)
                    if budget is not None:
                        budget.take_page()
                if not res:
                    raise AntiBot("接口无响应(疑似风控重定向),已中止;请检查浏览器后重试")
                if res.get("code") != 0:
                    # 服务器明确拒绝:绝不重试连发(连发正是触发 _security_check 循环的原因)
                    code = res.get("code")
                    raise AntiBot(f"接口拒绝(code={code}),已中止;请在浏览器完成安全验证后重试")
                if await self._is_verify_page(page):
                    await self._wait_manual_verify(page, progress=progress, should_stop=should_stop)
                    res = await self._fetch_joblist(page, keyword, city_code, page_num)
                fresh = []
                for x in res.get("list") or []:
                    draft, warns = self._draft_from_api(x, map_, city)
                    if warns and warn is not None:
                        # 条目级字段异常(缺ID/缺标题等):上报异常面板(带跳转链接)
                        jid0 = str(x.get("id") or "")
                        u0 = f"https://www.zhipin.com/job_detail/{jid0}.html" if jid0 else ""
                        await warn([f"Boss列表字段异常: {w}" for w in warns], u0)
                    if draft and draft.platform_job_id not in seen:
                        seen.add(draft.platform_job_id)
                        fresh.append(draft)
                random.shuffle(fresh)   # 打乱列表内详情打开顺序(顺序穷举是机器签名)
                if not fresh and progress:
                    # 不再提前停止:页与页之间常有重叠(排序微调),整页重复
                    # 不代表结果翻完;持续翻到 hasMore=False 或设定页数上限
                    progress(
                        f"第 {page_num} 页 {len(res.get('list') or [])} 个岗位均与先前重复,继续翻页"
                    )
                if progress:
                    progress(f"第 {page_num} 页取得 {len(fresh)} 个岗位")
                sal_filtered = 0
                for draft in fresh:
                    # 薪资范围过滤:列表阶段直接排除,不打开详情页(解析失败不误杀)
                    if sal_rng and draft.salary:
                        in_rng = salary_in_range(draft.salary, sal_rng)
                        if in_rng is False:
                            sal_filtered += 1
                            continue
                    # 详情前查重:已入库岗位不再打开详情页(避免重复访问触发风控)
                    if skip_check and await skip_check(draft.platform_job_id):
                        skipped_known += 1
                        if progress and skipped_known % 10 == 0:
                            progress(f"已跳过 {skipped_known} 个已入库岗位(不重复打开详情)")
                        yield draft
                        continue
                    if should_stop is not None and should_stop():
                        return
                    if dp is None:
                        dp = await page.context.new_page()
                    await self._parse_detail(dp, draft, should_stop=should_stop, progress=progress)
                    yield draft
                if sal_filtered and progress:
                    progress(f"第 {page_num} 页有 {sal_filtered} 个岗位薪资范围外,已过滤")
                if not res.get("hasMore", True):
                    break
        finally:
            if own_detail and dp is not None:
                # 停在安全验证页时保留标签:验证页只替换内容不改URL,须检测页面文本;
                # 用户完成滑块后平台会自动跳回岗位页,无需再手动开页面
                keep_verify = False
                try:
                    txt = await dp.evaluate(
                        "() => (document.title||'') + ((document.body?.innerText||'').slice(0, 2000))"
                    )
                    keep_verify = any(k in (txt or "") for k in ("安全验证", "安全挑战", "拖动滑块", "验证码"))
                except Exception:  # noqa: BLE001
                    keep_verify = False
                if not keep_verify:
                    try:
                        await dp.close()
                    except Exception:  # noqa: BLE001
                        pass


register(BossPlatform())