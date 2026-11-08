"""前程无忧(51job)适配器 —— 接口直连版,对齐 Boss 高标准(2026-11 实测校准)。

要点:
- 列表不走 DOM:在 we.51job.com 页面上下文内直接 fetch 内部接口
  /api/job/search-pc(带登录态;精简参数即可,无需 decode__1048 风控 token,实测有效)
- 列表接口自带完整职位描述(jobDescribe)/精确发布时间(issueDateString)/
  数值薪资(jobSalaryMin/Max)/公司行业/HR在线 —— 常规情况**不需要打开详情页**,
  单页一次请求,风控暴露面最小;仅在描述缺失时打开详情页兜底(JSON-LD + DOM)
- 翻页终止:接口 totalcount / 空页 / 页数上限
- 风控:接口连续失败立即中止(AntiBot),绝不重试连发;验证页特征即时判定
- 活跃度:51job 无 HR 活跃文案,判定走精确发布时间(JOB_PUBLISH_DAYS 窗口);
  hrIsOnline 存 hr_online 布尔供界面展示
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time as _time
from typing import AsyncIterator
from urllib.parse import quote, urlencode

from playwright.async_api import Page, TimeoutError as PWTimeout

from ..utils import (
    RETRY_ATTEMPTS,
    detail_delay,
    global_nav_gate,
    human_delay,
    human_read,
    interruptible_sleep,
    page_responsive,
    parse_relative_time,
    patient_poll,
    save_snapshot,
    silent_backoff,
    warmup,
)
from . import register
from .base import AntiBot, BasePlatform, BudgetExhausted, JobDraft, LoginRequired
from .common import parse_salary_range, parse_salary_months, salary_in_range, strip_html
from .selectors import JOB51

logger = logging.getLogger(__name__)

# 与页面自身搜索请求保持一致的每页条数(捕获页面首屏响应后,后续 fetch 页必须
# 与之对齐,否则翻页间会出现 21~N 条的漏采裂缝)
PAGE_SIZE = 20

_ID_PATTERNS = [
    r"/(\d{6,})\.html",
    r"/all/(\d+)\.html",
    r"jobId=(\w+)",
]


def _extract_job_id(url: str) -> str:
    for pat in _ID_PATTERNS:
        m = re.search(pat, url or "")
        if m:
            return m.group(1)
    return re.sub(r"\W+", "_", url)[-60:]


def _salary_from_ld(sal) -> str:
    """JSON-LD baseSalary → "min-maxK" 文本(51job 用元/月或元/年,统一换算 K/月)。"""
    if not isinstance(sal, dict):
        return ""
    v = sal.get("value") or {}
    if not isinstance(v, dict):
        return ""
    try:
        lo, hi = float(v.get("minValue")), float(v.get("maxValue"))
    except (TypeError, ValueError):
        return ""
    if lo <= 0 and hi <= 0:
        return ""
    if lo > hi:
        lo, hi = hi, lo
    unit = str(sal.get("unitText") or v.get("unitText") or "").upper()
    if "YEAR" in unit:
        lo, hi = lo / 12000, hi / 12000        # 元/年 → K/月
    elif "MONTH" in unit:
        lo, hi = lo / 1000, hi / 1000          # 元/月 → K/月
    elif lo > 1000:                            # 无单位但数值过大,按元处理
        lo, hi = lo / 1000, hi / 1000
    def _fmt(x: float) -> str:
        return f"{x:.1f}".rstrip("0").rstrip(".")
    return f"{_fmt(lo)}-{_fmt(hi)}K"


# 详情页正常渲染特征(存在即绝非验证/拦截页)
_DETAIL_DOM_RE = ".job_msg, .bmsg, .jdesc, .cn, [class*='job'] [class*='desc']"
# 验证页特征(URL/文本;文案含 51job 通用风控提示)
_VERIFY_RE = re.compile(r"verify|captcha|validate", re.I)
# Access Verification:前程无忧人机验证页标题/正文实测特征(2026-11)
_VERIFY_TEXT_RE = re.compile(
    r"安全验证|拖动滑块|完成验证|人机验证|账号行为异常|安全中心|access\s*verification", re.I
)


class Job51Platform(BasePlatform):
    id = "job51"
    name = "前程无忧"
    home_url = "https://www.51job.com"
    home_prefix = "https://we.51job.com"
    detail_url_marker = "jobs.51job.com"

    def supported_cities(self) -> set[str]:
        return set(JOB51["city_codes"]) - {"全国"}

    # ---------- 接口辅助 ----------
    @staticmethod
    def _api_qs(keyword: str, area: str, page_num: int, page_size: int = PAGE_SIZE) -> str:
        """search-pc 接口查询串(实测精简参数即可,无需 decode__1048/accountId)。"""
        params = {
            "api_key": "51job",
            "timestamp": str(int(_time.time())),
            "keyword": keyword,
            "searchType": "2",
            "function": "",
            "industry": "",
            "jobArea": area,
            "jobArea2": "",
            "landmark": "",
            "metro": "",
            "salary": "",
            "workYear": "",
            "degree": "",
            "companyType": "",
            "companySize": "",
            "jobType": "",
            "issueDate": "",
            "sortType": "0",
            "pageNum": str(page_num),
            "requestId": "",
            "pageSize": str(page_size),
            "source": "1",
        }
        return urlencode(params)

    @staticmethod
    def _fetch_js(qs: str) -> str:
        return (
            "fetch('/api/job/search-pc?" + qs + "', "
            "{headers: {'accept': 'application/json'}})"
            ".then(r => r.text())"
        )

    async def _fetch_search(self, page: Page, keyword: str, area: str, page_num: int) -> dict | None:
        """页内 fetch 搜索接口。返回解析后的 JSON;失败返回 None(由调用方决策)。

        只容忍"页面正在导航"的瞬态异常(最多 2 次);响应非 JSON(被风控
        重定向到 HTML 页)绝不重试连发——由调用方按错误类型处理。
        """
        for i in range(2):
            try:
                # 全局导航闸:多平台并发时控制叠加请求率(同 IP 总频率)
                await global_nav_gate()
                raw = await page.evaluate(self._fetch_js(self._api_qs(keyword, area, page_num)))
                if raw and raw.strip().startswith("{"):
                    return json.loads(raw)
                # 非 JSON:多为风控/异常页,直接返回 None
                logger.warning("51job 接口非 JSON 响应(第 %d 次): %s", i + 1, str(raw)[:120])
                return None
            except PWTimeout:
                return None
            except Exception as exc:  # noqa: BLE001  (导航瞬态)
                logger.warning("51job 接口 fetch 异常(重试 %d/2): %s", i + 1, str(exc)[:120])
                await asyncio.sleep(1.5)
        return None

    async def _fetch_via_page_pv(self, page: Page, keyword: str, area: str,
                                 page_num: int, extras: dict | None, city: str) -> dict | None:
        """以"真实搜索页 PV"方式取数:导航到搜索页并捕获页面自身发出的签名请求。

        真人浏览的接口请求是页面浏览的副产品;"PV≈0 而接口数百次"的纯 API
        会话形状是风控强判定信号。每 3~6 页用本方法把接口请求伴生到一次真实
        页面浏览上(页面自己的请求天然携带合法签名与完整上下文),捕获失败
        回退页内 fetch。
        """
        url = self._search_url(city, keyword, page_num, extras)
        try:
            async with page.expect_response(lambda r: "search-pc" in r.url, timeout=20000) as ri:
                await global_nav_gate()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            d = json.loads(await (await ri.value).text())
            if str(d.get("status")) == "1":
                return d
            logger.warning("51job 搜索页 PV 响应异常: status=%s", d.get("status"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("51job 搜索页 PV 取数失败(回退 fetch): %s | %s", url[:100], str(exc)[:120])
        # PV 产生后静默停留片刻(真人浏览),再回退页内 fetch
        await page.wait_for_timeout(random.randint(3000, 7000))
        return await self._fetch_search(page, keyword, area, page_num)

    # ---------- 登录态 ----------
    async def is_logged_in(self, page: Page) -> bool:
        try:
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(2000)
        # 已登录页头显示用户名;未登录页头有可见"登录/注册"入口。
        # 限定页头区域(top<200)且整词匹配,避免正文推广位的"登录"字样误伤
        try:
            state = await page.evaluate(
                """() => {
                    const vis = el => el && (el.offsetHeight || 0) > 0;
                    const entry = [...document.querySelectorAll('a,button,span,div')]
                        .find(e => vis(e)
                            && e.getBoundingClientRect().top < 200
                            && /^(登录\\/注册|登录|登陆)$/.test((e.innerText || '').trim()));
                    return JSON.stringify({ login: !!entry });
                }"""
            )
            st = json.loads(state or "{}")
            if st.get("login"):
                return False
        except Exception:  # noqa: BLE001
            pass
        # 兜底:页面出现明确的未登录文案时判定未登录(登录入口选择器可能失效)
        try:
            body = (await page.evaluate("() => (document.body?.innerText||'').slice(0, 3000)")) or ""
            if re.search(r"请登录|Hi[,，]\s*游客", body):
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------- 风控 ----------
    async def _is_verify_page(self, page: Page) -> bool:
        try:
            u = page.url or ""
            if _VERIFY_RE.search(u):
                return True
            title = (await page.title()) or ""
            if _VERIFY_TEXT_RE.search(title):
                return True
            # 详情页结构仍在 → 内容正常渲染,不是验证页
            if await page.query_selector(_DETAIL_DOM_RE):
                return False
            text = (await page.evaluate("() => (document.body?.innerText||'').slice(0,2000)")) or ""
            # 正文误报防线:验证页正文极少(标题+滑块),要求内容足够短才凭文本判定
            return bool(_VERIFY_TEXT_RE.search(text)) and len(text) < 600
        except Exception:
            return False

    async def _wait_if_verify(self, page: Page, progress=None, should_stop=None) -> None:
        """检测到人机验证(Access Verification/安全验证):强提醒 + 持续静默等待。

        未检测到验证页直接返回;检测到则每 5 秒**只读**轮询(读 URL/标题/DOM,
        不产生任何平台请求),绝不刷新页面、绝不重新导航;通过后任务自动继续。
        用户点「停止抓取」可随时退出等待(调度器按手动停止收尾,不报错)。
        """
        if not await self._is_verify_page(page):
            return
        from ..utils import set_verify_wait
        set_verify_wait(self.id, True)
        try:
            if progress:
                progress("⚠ 人机验证(Access Verification):请在浏览器完成验证,通过后自动继续(每5秒检测,不刷新页面)")
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

    # ---------- 搜索 ----------
    def _search_url(self, city: str, keyword: str, page_num: int, extras: dict | None = None) -> str:
        area = JOB51["city_codes"].get(city, JOB51["city_codes"]["全国"]) if city else JOB51["city_codes"]["全国"]
        sort_type = str(((extras or {}).get("sortType", "0")))
        params = {
            "keyword": keyword,
            "searchType": "2",
            "sortType": sort_type,
            "jobArea": area,
            "pageSize": str(PAGE_SIZE),
            "pageNum": str(page_num),
        }
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        return f"https://we.51job.com/pc/search?{qs}"

    # ---------- 列表解析(接口数据) ----------
    def _draft_from_api(self, j: dict, city: str) -> tuple[JobDraft | None, list[str]]:
        """解析单条接口数据 → (draft, 警告列表)。draft 为 None 表示条目不可用。"""
        warns: list[str] = []
        title = str(j.get("jobName") or "")
        jid = str(j.get("jobId") or "")
        if not title or not jid:
            if not jid:
                warns.append("列表条目缺少 jobId,该岗位无法入库(平台接口可能已改版)")
            if not title:
                warns.append("列表条目缺少 jobName(职位名),该岗位已跳过(平台接口可能已改版)")
            return None, warns
        url = str(j.get("jobHref") or (f"https://jobs.51job.com/all/{jid}.html" if jid else ""))
        detail = j.get("jobAreaLevelDetail") or {}
        area = str(j.get("jobAreaString") or detail.get("cityString") or "")
        salary = str(j.get("provideSalaryString") or "")
        desc = strip_html(j.get("jobDescribe") or "")
        # 技能/福利标签拼进描述尾部(供备注规则扫描),剔除与经验/学历重复的项
        tags = [str(t) for t in (j.get("jobTags") or []) if str(t).strip()]
        extra_tags = [
            t for t in tags
            if t not in {str(j.get("workYearString") or ""), str(j.get("degreeString") or "")}
        ]
        if extra_tags:
            desc = (desc + "\n技能/福利标签: " + "、".join(extra_tags)).strip()
        industry = str(j.get("coIndustryText") or j.get("companyIndustryType1Str") or "")
        # 过期岗位:接口明确标记的不再入库
        if j.get("isExpire"):
            warns.append(f"岗位已过期(isExpire=true),跳过: {title}")
            return None, warns
        if not desc:
            warns.append("列表接口未返回职位描述(jobDescribe),将尝试详情页兜底(平台可能已改版)")
        draft = JobDraft(
            platform_job_id=jid,
            title=title,
            url=url,
            company=str(j.get("companyName") or ""),
            salary=salary,
            salary_months=parse_salary_months(salary),
            city=str(detail.get("cityString") or area or city),
            district=str(detail.get("districtString") or ""),
            experience=str(j.get("workYearString") or ""),
            education=str(j.get("degreeString") or ""),
            description=desc,
            company_intro="",
            hr_name=str(j.get("hrName") or ""),
            hr_title=str(j.get("hrPosition") or ""),
            hr_online=bool(j.get("hrIsOnline")),
            industry=industry,
            company_scale=str(j.get("companySizeString") or ""),
            published_at=parse_relative_time(str(j.get("issueDateString") or "")),
            raw={
                "companyTypeString": j.get("companyTypeString") or "",
                "salaryMinK": (int(j["jobSalaryMin"]) / 1000) if str(j.get("jobSalaryMin") or "").isdigit() else None,
                "salaryMaxK": (int(j["jobSalaryMax"]) / 1000) if str(j.get("jobSalaryMax") or "").isdigit() else None,
                "hrLabels": j.get("hrLabels") or [],
                "tags": tags,
                "_warnings": warns,
            },
        )
        return draft, warns

    # ---------- 详情兜底(仅描述缺失时) ----------
    async def _parse_detail(self, page: Page, draft: JobDraft, should_stop=None, progress=None) -> None:
        """打开详情页兜底解析描述(JSON-LD + DOM);落地校验 + 风控/blank 检测。"""
        # 会话预算:详情额度耗尽即优雅收尾(51job 常规零详情,仅兜底场景计数)
        budget = self._budget
        if budget is not None:
            if budget.details_left <= 0:
                raise BudgetExhausted(f"详情页预算耗尽(上限 {budget.details} 个)")
            budget.take_detail()
        # 详情页节奏:随机间隔(任务级设置,默认 3~8 秒),模拟人工浏览
        await detail_delay(self.id)
        # 危险率长休息:模拟真人起身离开(无记忆触发,无固定批长元周期)
        await self._detail_pacer.tick(self.id, progress=progress, should_stop=should_stop)
        # 全局导航闸:多平台并发时控制叠加请求率
        await global_nav_gate()
        if should_stop is not None and should_stop():
            return
        # 真人打开详情来自列表页点击:带上"列表来路" referer(直达入口是强自动化签名)
        referrer = (self._search_ctx or {}).get("referer") or self.home_url
        t0 = _time.monotonic()
        try:
            await page.goto(draft.url or self.home_url, wait_until="domcontentloaded",
                            timeout=25000, referrer=referrer)
        except Exception:  # noqa: BLE001
            pass
        # 静默观察落地(意外等待不刷新页面):每轮静默 5~10 秒后检查一次,3 轮不落地才定论
        landed = draft.platform_job_id in (page.url or "")
        if not landed:
            async def _landed() -> bool:
                return draft.platform_job_id in (page.url or "")
            landed = await patient_poll(_landed, should_stop=should_stop)
        if not landed:
            # 静默观察 3 轮仍不落地,分级处理:
            # - 页面无响应/被清空 → 风控未识别或中断,自动中止任务(防止对异常状态连发);
            # - 平台正常响应但岗位失效(重定向)→ 视为已下线,跳过。
            if "about:blank" in (page.url or "") or not await page_responsive(page):
                raise AntiBot("详情页无响应(疑似风控),已中止;请检查浏览器后重试")
            # 单次导航即定论,不做二次硬闯(反复刷新加重风控)
            logger.warning("51job 详情落地异常(视为已下线,跳过): %s -> %s", draft.title, (page.url or "")[:120])
            draft.raw["offline"] = True
            return
        if "about:blank" in (page.url or "") or not ((await page.title()) or "").strip():
            logger.warning("51job 详情页被清空(反自动化拦截): %s", draft.title)
            draft.raw["blanked"] = True
            return
        await self._wait_if_verify(page, progress=progress, should_stop=should_stop)
        await page.wait_for_timeout(1500)
        # JSON-LD
        try:
            ld = await page.evaluate(
                "() => Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]')).map(s => s.textContent)"
            )
            for raw in ld or []:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                objs = obj if isinstance(obj, list) else [obj]
                for o in objs:
                    if isinstance(o, dict) and o.get("@type") in ("JobPosting", "jobPosting"):
                        draft.title = draft.title or str(o.get("title") or "")
                        desc = o.get("description") or ""
                        draft.description = strip_html(desc) if desc else draft.description
                        org = o.get("hiringOrganization") or {}
                        if isinstance(org, dict):
                            draft.company = draft.company or str(org.get("name") or "")
                        dp = o.get("datePosted")
                        if dp and not draft.published_at:
                            draft.published_at = parse_relative_time(str(dp)) or draft.published_at
                        if not draft.salary:
                            draft.salary = _salary_from_ld(o.get("baseSalary")) or draft.salary
                            if draft.salary and not draft.salary_months:
                                draft.salary_months = parse_salary_months(draft.salary)
        except Exception as exc:  # noqa: BLE001
            logger.debug("51job JSON-LD 解析失败: %s", exc)
        # DOM 兜底
        if not draft.description:
            try:
                el = page.locator(JOB51["detail_desc"]).first
                if await el.count():
                    draft.description = ((await el.text_content()) or "").strip()
            except Exception:  # noqa: BLE001
                pass
        if not draft.title:
            try:
                el = page.locator(JOB51["detail_title"]).first
                if await el.count():
                    draft.title = ((await el.text_content()) or "").strip()
            except Exception:  # noqa: BLE001
                pass
        # 阅读仿真:停留∝描述长度(落地校验/解析耗时已计入,只补差额),
        # 滚动浏览后再离开——零阅读访问(停留与内容无关)是强自动化签名
        try:
            await human_read(page, draft.description or draft.title or "", t0=t0)
        except Exception:  # noqa: BLE001
            pass
        # 阅读仿真:停留∝描述长度(落地校验/解析耗时已计入,只补差额),
        # 滚动浏览后再离开——零阅读访问(停留与内容无关)是强自动化签名
        try:
            await human_read(page, draft.description or draft.title or "", t0=t0)
        except Exception:  # noqa: BLE001
            pass

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
        # 详情标签页懒创建:全部岗位都带完整描述时不开任何新标签
        dp = detail_page
        area = JOB51["city_codes"].get(city, JOB51["city_codes"]["全国"]) if city else JOB51["city_codes"]["全国"]
        sal_rng = (extras or {}).get("salary") or ""
        # 任务级会话上下文:请求预算 + "列表来路"(详情 referer 模拟真人点击链)
        budget = (extras or {}).get("budget")
        self._budget = budget
        self._search_ctx: dict = {}
        # PV 配平倒计时:每 3~6 个接口页以"真实搜索页导航"取数一次(见 _fetch_via_page_pv)
        pv_left = random.randint(3, 6)
        try:
            # 载体页策略:
            # - 冷启动(页面不在 we.51job.com):goto 搜索页,并捕获页面自身发出的
            #   search-pc 签名请求作为第一页数据(实测:跨域冷启动后立刻页内 fetch
            #   会被软风控拒绝——返回合法的 status=1 空列表;页面自身的请求天然有效)
            # - 热启动(复用已有 we 域标签):无页面自身请求可捕获,直接页内 fetch
            first_url = self._search_url(city, keyword, 1, extras)
            first_page_data = None
            if "we.51job.com" not in (page.url or ""):
                if progress:
                    progress(f"打开前程无忧搜索页:{keyword}@{city or '全国'}")
                try:
                    async with page.expect_response(
                        lambda r: "search-pc" in r.url, timeout=20000
                    ) as ri:
                        try:
                            await page.goto(first_url, wait_until="domcontentloaded", timeout=30000)
                        except PWTimeout:
                            pass
                        resp = await ri.value
                        body = await resp.text()
                    d = json.loads(body)
                    if str(d.get("status")) == "1":
                        first_page_data = d
                    else:
                        logger.warning("51job 页面首屏响应异常: status=%s", d.get("status"))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("捕获页面首屏响应失败(fetch 兜底): %s", str(exc)[:120])
                await page.wait_for_timeout(2000)
            if "we.51job.com" not in (page.url or ""):
                raise AntiBot("页面未落在搜索域(被重定向),已中止;请手动打开 we.51job.com 后重试")
            if "passport.51job.com" in (page.url or "") or re.search(r"/login", (page.url or "")):
                raise LoginRequired("51job 搜索页跳转到登录页")
            await self._wait_if_verify(page, progress=progress, should_stop=should_stop)
            # 启动预热:静默停留 + 轻滚动,避免任务启动瞬间即高频发请求
            await warmup(page, self.id)

            seen: set[str] = set()
            total: int | None = None
            fail_streak = 0
            retried_empty = False
            page_num = 0
            while page_num < max(1, max_pages):
                page_num += 1
                if should_stop is not None and should_stop():
                    return
                # 详情 referer 跟随当前页对应搜索列表;页预算耗尽即优雅收尾
                self._search_ctx["referer"] = self._search_url(city, keyword, page_num, extras)
                if budget is not None and budget.pages_left <= 0:
                    raise BudgetExhausted(f"列表页预算耗尽(上限 {budget.pages} 页)")
                if progress:
                    progress(f"接口拉取第 {page_num} 页: {keyword}@{city or '全国'}")
                if page_num == 1 and first_page_data is not None:
                    data = first_page_data
                    first_page_data = None
                else:
                    # PV 配平:每 3~6 页以"真实搜索页导航+捕获页面自身签名请求"取数,
                    # 让接口请求伴生页面浏览(打断纯 API 会话形状);失败自动回退 fetch
                    pv_left -= 1
                    if pv_left <= 0:
                        pv_left = random.randint(3, 6)
                        data = await self._fetch_via_page_pv(page, keyword, area, page_num, extras, city)
                    else:
                        data = await self._fetch_search(page, keyword, area, page_num)
                if not (data and str(data.get("status")) == "1"):
                    fail_streak += 1
                    msg = (data or {}).get("message") or ("非JSON响应" if data is None else f"status={data.get('status')}")
                    logger.warning("51job 接口异常: %s | 页面URL: %s", msg, (page.url or "")[:120])
                    if progress:
                        progress(f"接口异常({msg}),静默等待后重试,第 {fail_streak}/{RETRY_ATTEMPTS} 次")
                    if fail_streak >= RETRY_ATTEMPTS:
                        raise AntiBot(f"接口连续{RETRY_ATTEMPTS}次失败({msg}),已中止;请检查浏览器后重试")
                    # 意外等待静默退避(不刷新页面):5~10 秒后重试当前页
                    await silent_backoff()
                    page_num -= 1  # 重试当前页
                    continue
                fail_streak = 0
                if budget is not None:
                    budget.take_page()
                job = ((data.get("resultbody") or {}).get("job") or {})
                items = job.get("items") or []
                try:
                    total = int(job.get("totalcount") or job.get("totalCount") or 0) or total
                except (TypeError, ValueError):
                    pass
                if not items:
                    # totalcount 显示仍有数据但本页为空:疑似短时冷却/竞态,等待后重试一次;
                    # 重试仍空才视为翻完(不误杀正常翻完场景)
                    if total and total > (page_num - 1) * 50 and not retried_empty:
                        retried_empty = True
                        if progress:
                            progress(f"第 {page_num} 页异常为空(接口 total={total}),静默等待后重试")
                        await silent_backoff()
                        page_num -= 1  # 重试当前页
                        continue
                    if progress:
                        progress(f"第 {page_num} 页接口返回 0 条,视为已翻完")
                    break
                fresh: list[JobDraft] = []
                for j in items:
                    if not isinstance(j, dict):
                        continue
                    draft, warns = self._draft_from_api(j, city)
                    if warns and warn is not None:
                        u0 = str((j or {}).get("jobHref") or "")
                        await warn([f"前程无忧列表字段异常: {w}" for w in warns], u0)
                    if draft and draft.platform_job_id not in seen:
                        seen.add(draft.platform_job_id)
                        fresh.append(draft)
                random.shuffle(fresh)   # 打乱列表内处理顺序(顺序穷举是机器签名)
                if not fresh and progress:
                    # 不提前停止:整页重复≠结果翻完(排序微调常致页间重叠),
                    # 持续翻到 totalcount 耗尽或页数上限(手动/异常中断除外)
                    progress(f"第 {page_num} 页 {len(items)} 个岗位均与先前重复,继续翻页")
                if progress:
                    progress(f"第 {page_num} 页取得 {len(fresh)} 个岗位")
                sal_filtered = 0
                for draft in fresh:
                    # 薪资范围过滤:接口数值薪资优先(最准),文本解析兜底;解析失败不误杀
                    if sal_rng:
                        lo_k, hi_k = (draft.raw.get("salaryMinK"), draft.raw.get("salaryMaxK"))
                        in_rng = None
                        if lo_k is not None and hi_k is not None:
                            # 直接用接口数值区间与配置区间比较(部分重叠算通过)
                            rng = parse_salary_range(sal_rng)
                            if rng:
                                clo, chi = rng
                                if clo is not None and hi_k < clo:
                                    in_rng = False
                                elif chi is not None and lo_k > chi:
                                    in_rng = False
                                else:
                                    in_rng = True
                        if in_rng is None:
                            in_rng = salary_in_range(draft.salary, sal_rng)
                        if in_rng is False:
                            sal_filtered += 1
                            continue
                    # 详情前查重:已入库岗位不再处理(接口直连无需打开详情页)
                    if skip_check and await skip_check(draft.platform_job_id):
                        yield draft
                        continue
                    if should_stop is not None and should_stop():
                        return
                    # 描述缺失才开详情页兜底(常规情况零详情请求)
                    if not (draft.description or "").strip() and draft.url:
                        if dp is None:
                            dp = await page.context.new_page()
                        await self._parse_detail(dp, draft, should_stop=should_stop, progress=progress)
                    yield draft
                if sal_filtered and progress:
                    progress(f"第 {page_num} 页有 {sal_filtered} 个岗位薪资范围外,已过滤")
                # totalcount 耗尽 → 停止翻页
                if total is not None and page_num * PAGE_SIZE >= total:
                    if progress:
                        progress(f"已覆盖全部 {total} 条结果,提前结束翻页")
                    break
                if page_num < max_pages:
                    await human_delay(self.id)
                    # 批次长休息:每 2~4 页插入一次 15~40 秒会话休息(防风控)
                    await self._page_pacer.tick(self.id, progress=progress, should_stop=should_stop)
        finally:
            if own_detail and dp is not None:
                try:
                    await dp.close()
                except Exception:  # noqa: BLE001
                    pass


register(Job51Platform())