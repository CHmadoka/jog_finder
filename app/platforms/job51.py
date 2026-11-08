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
import re
import time as _time
from typing import AsyncIterator
from urllib.parse import quote, urlencode

from playwright.async_api import Page, TimeoutError as PWTimeout

from ..utils import (
    RETRY_ATTEMPTS,
    detail_delay,
    human_delay,
    human_scroll,
    page_responsive,
    parse_relative_time,
    patient_poll,
    save_snapshot,
    silent_backoff,
)
from . import register
from .base import AntiBot, BasePlatform, JobDraft, LoginRequired
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
_VERIFY_TEXT_RE = re.compile(r"安全验证|拖动滑块|完成验证|人机验证|账号行为异常|安全中心")


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

    async def _raise_if_verify(self, page: Page) -> None:
        if await self._is_verify_page(page):
            raise AntiBot(
                "前程无忧触发安全验证,任务已暂停。请在浏览器中完成验证后重新开始任务"
                "(已抓岗位自动跳过)"
            )

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
    async def _parse_detail(self, page: Page, draft: JobDraft, should_stop=None) -> None:
        """打开详情页兜底解析描述(JSON-LD + DOM);落地校验 + 风控/blank 检测。"""
        # 详情页节奏:随机间隔(任务级设置,默认 3~8 秒),模拟人工浏览
        await detail_delay(self.id)
        if should_stop is not None and should_stop():
            return
        try:
            await page.goto(draft.url or self.home_url, wait_until="domcontentloaded", timeout=25000)
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
                raise AntiBot(
                    f"详情页无响应(静默重试 {RETRY_ATTEMPTS} 次后仍未落地): {draft.title}。"
                    "可能存在未识别的风控或页面中断,任务已自动中止,请人工检查浏览器后重试"
                )
            # 单次导航即定论,不做二次硬闯(反复刷新加重风控)
            logger.warning("51job 详情落地异常(视为已下线,跳过): %s -> %s", draft.title, (page.url or "")[:120])
            draft.raw["offline"] = True
            return
        if "about:blank" in (page.url or "") or not ((await page.title()) or "").strip():
            logger.warning("51job 详情页被清空(反自动化拦截): %s", draft.title)
            draft.raw["blanked"] = True
            return
        await self._raise_if_verify(page)
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
                raise AntiBot(
                    "前程无忧页面未能落在 we.51job.com 域(可能被重定向),"
                    "请在浏览器中手动打开 we.51job.com 确认可访问后重试"
                )
            if "passport.51job.com" in (page.url or "") or re.search(r"/login", (page.url or "")):
                raise LoginRequired("51job 搜索页跳转到登录页")
            await self._raise_if_verify(page)

            seen: set[str] = set()
            total: int | None = None
            fail_streak = 0
            retried_empty = False
            page_num = 0
            while page_num < max(1, max_pages):
                page_num += 1
                if should_stop is not None and should_stop():
                    return
                if progress:
                    progress(f"接口拉取第 {page_num} 页: {keyword}@{city or '全国'}")
                if page_num == 1 and first_page_data is not None:
                    data = first_page_data
                    first_page_data = None
                else:
                    data = await self._fetch_search(page, keyword, area, page_num)
                if not (data and str(data.get("status")) == "1"):
                    fail_streak += 1
                    msg = (data or {}).get("message") or ("非JSON响应" if data is None else f"status={data.get('status')}")
                    logger.warning("51job 接口异常: %s | 页面URL: %s", msg, (page.url or "")[:120])
                    if progress:
                        progress(f"接口异常({msg}),静默等待后重试,第 {fail_streak}/{RETRY_ATTEMPTS} 次")
                    if fail_streak >= RETRY_ATTEMPTS:
                        raise AntiBot(
                            f"前程无忧接口连续 {RETRY_ATTEMPTS} 次失败({msg}),任务已自动中止。"
                            "可能被限流或需要安全验证:请在浏览器中人工检查后重新开始任务"
                        )
                    # 意外等待静默退避(不刷新页面):5~10 秒后重试当前页
                    await silent_backoff()
                    page_num -= 1  # 重试当前页
                    continue
                fail_streak = 0
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
                        await self._parse_detail(dp, draft, should_stop=should_stop)
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
        finally:
            if own_detail and dp is not None:
                try:
                    await dp.close()
                except Exception:  # noqa: BLE001
                    pass


register(Job51Platform())