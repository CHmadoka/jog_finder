"""猎聘(liepin.com)适配器 —— 响应监听版,对齐 Boss 高标准(2026-11 实测校准)。

要点:
- 猎聘搜索接口(api-c.liepin.com /pc-search-job)有请求签名墙(页内直发 flag=-1400,
  实测),不复刻签名:改为**导航驱动 + 响应监听**——goto 搜索页 URL,页面自身
  发出的签名请求由 expect_response 捕获,拿到结构化 JSON(jobCardList,每页 ~40 条,
  含公司行业/规模/融资阶段/HR活跃文案/刷新时间);DOM 卡片解析兜底
- 列表不带职位描述:逐岗打开详情页,JSON-LD(schema.org JobPosting)优先,
  DOM(.job-intro-container)兜底
- 节奏:详情页随机间隔(任务级设置,默认 3~8s),翻页间 human_delay
- 风控:验证页特征即时判定(AntiBot 中止);登录页跳转立即中止
- 活跃度:recruiter.imShowText("1天前在线")→ 统一估算规则;缺失退回 refreshTime
- 平台行为:猎聘搜索结果会混入外地"推荐"岗位,dq 字段如实记录城市,不做过滤
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import AsyncIterator
from urllib.parse import quote

from playwright.async_api import Page, TimeoutError as PWTimeout

from ..utils import (
    active_estimate_from_text,
    detail_delay,
    fmt_dt,
    human_delay,
    human_scroll,
    parse_relative_time,
    save_snapshot,
)
from . import register
from .base import AntiBot, BasePlatform, JobDraft, LoginRequired
from .common import deep_find_list, parse_salary_months, salary_in_range, strip_html
from .selectors import LIEPIN

logger = logging.getLogger(__name__)


def _active_from_text(text: str) -> str:
    """活跃文案 → 存储字符串(含"前/后/活跃"方向后缀)。统一走 utils 规则,相对时间兜底。"""
    s = active_estimate_from_text(text)
    if not s:
        rt = parse_relative_time(text)
        s = fmt_dt(rt) or "" if rt else ""
    return s


def _job_id_from_url(url: str) -> str:
    m = re.search(r"/job/(\d+)\.shtml", url or "")
    if m:
        return m.group(1)
    return re.sub(r"\W+", "_", (url or ""))[-60:]


def _parse_compact_dt(s: str) -> datetime | None:
    """解析猎聘紧凑时间串 '20251103160956'(YYYYMMDDHHMMSS,可缺时分秒)。"""
    m = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})(\d{2})?(\d{2})?(\d{2})?", str(s or "").strip())
    if not m:
        return None
    try:
        return datetime(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4) or 0), int(m.group(5) or 0), int(m.group(6) or 0),
        )
    except ValueError:
        return None


# 猎聘风控特征:验证页 URL / 页面文案。命中即中止任务(不重试连发)。
# 实测风控页:safe.liepin.com/v/intercept/verifysms(短信验证,"账号行为异常")
_VERIFY_URL_RE = re.compile(r"verify|captcha|validate", re.I)
_VERIFY_TEXT_RE = re.compile(r"安全验证|拖动滑块|完成验证|人机验证|安全挑战|账号行为异常|安全中心")
# 详情页正常渲染特征区块(存在即绝非验证/拦截页)
_DETAIL_DOM_RE = ".job-intro-container, .job-item-desc, [class*='job-detail'], .job-intro"


class LiepinPlatform(BasePlatform):
    id = "liepin"
    name = "猎聘"
    home_url = "https://www.liepin.com"
    home_prefix = "https://www.liepin.com"
    detail_url_marker = "/job/"

    def supported_cities(self) -> set[str]:
        return set(LIEPIN["city_codes"]) - {"全国"}

    # ---------- 登录态 ----------
    async def is_logged_in(self, page: Page) -> bool:
        try:
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(2000)
        # 已登录页头显示昵称/头像;未登录页头有可见"登录/注册"入口。
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
            if re.search(r"请登录|您好,游客|您好，游客", body):
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------- 风控 ----------
    async def _is_verify_page(self, page: Page) -> bool:
        try:
            u = page.url or ""
            if _VERIFY_URL_RE.search(u):
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
                "猎聘触发安全验证,任务已暂停。请在浏览器中完成验证后重新开始任务(已抓岗位自动跳过)"
            )

    # ---------- 搜索 ----------
    def _search_url(self, city: str, keyword: str, page_num: int) -> str:
        city_code = LIEPIN["city_codes"].get(city) if city else None
        params = {"key": keyword, "currentPage": str(page_num)}
        if city_code:
            params["city"] = city_code
        qs = "&".join(f"{k}={quote(v, safe='')}" for k, v in params.items())
        return f"https://www.liepin.com/zhaopin/?{qs}"

    async def _wait_list(self, page: Page) -> None:
        try:
            await page.wait_for_selector(".jobCardPcContainer, .job-list-box", timeout=12000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(1500)

    # ---------- 列表解析 ----------
    async def _parse_list_json(self, data: dict, city: str, warn) -> list[JobDraft]:
        """解析捕获的搜索接口响应(flag=1,jobCardList)。"""
        drafts: list[JobDraft] = []
        cards = deep_find_list(data, ["jobCardList"]) or []
        for c in cards:
            if not isinstance(c, dict):
                continue
            job = c.get("job") or {}
            comp = c.get("comp") or {}
            rec = c.get("recruiter") or {}
            title = str(job.get("title") or "")
            url = str(job.get("link") or "")
            if url and "/job/" not in url:
                # 非岗位卡片(猎聘混排的职播/问答/广告等 /a/ 链接):静默跳过,
                # 不进详情流程(开了详情也只会被判"已下线",白耗一次请求)
                continue
            jid = _job_id_from_url(url) or str(job.get("jobId") or "")
            if not title or not jid:
                if warn is not None:
                    await warn(
                        ["猎聘列表字段异常: 条目缺少职位名或岗位链接,该岗位已跳过(平台可能已改版)"],
                        url or "https://www.liepin.com",
                    )
                continue
            area = str(job.get("dq") or "")
            city_name, district = (area.split("-", 1) + [""])[:2] if area else (city, "")
            salary = str(job.get("salary") or "")
            active_text = str(rec.get("imShowText") or "")
            refresh = _parse_compact_dt(str(job.get("refreshTime") or ""))
            drafts.append(
                JobDraft(
                    platform_job_id=jid,
                    title=title,
                    url=url,
                    company=str(comp.get("compName") or ""),
                    salary=salary,
                    salary_months=parse_salary_months(salary),
                    city=city_name or city,
                    district=district.strip(),
                    experience=str(job.get("requireWorkYears") or ""),
                    education=str(job.get("requireEduLevel") or ""),
                    hr_name=str(rec.get("recruiterName") or ""),
                    hr_title=str(rec.get("recruiterTitle") or ""),
                    hr_active_at=_active_from_text(active_text),
                    hr_active_text=active_text,
                    industry=str(comp.get("compIndustry") or ""),
                    company_stage=str(comp.get("compStage") or ""),
                    company_scale=str(comp.get("compScale") or ""),
                    published_at=refresh,
                    raw={"job": job, "comp": comp, "recruiter": {k: rec.get(k) for k in ("recruiterName", "recruiterTitle", "imShowText")}},
                )
            )
        if not cards and warn is not None:
            await warn(
                ["猎聘接口响应无岗位数据(jobCardList 为空,可能被风控或页面已改版)"],
                "https://www.liepin.com",
            )
        return drafts

    async def _parse_list_dom(self, page: Page, city: str, warn) -> list[JobDraft]:
        """DOM 兜底:.jobCardPcContainer 卡片,链接 + 文本模式解析(抗类名哈希变化)。"""
        drafts: list[JobDraft] = []
        cards = page.locator(LIEPIN["card"])
        n = await cards.count()
        for i in range(n):
            card = cards.nth(i)
            try:
                link = card.locator(LIEPIN["card_link"]).first
                if not await link.count():
                    continue
                href = await link.get_attribute("href") or ""
                if href and not href.startswith("http"):
                    href = "https://www.liepin.com" + ("" if href.startswith("/") else "/") + href
                # 职位名:jobTitleBox 内带 title 属性的 div(哈希类名不可靠)
                title = ""
                try:
                    t_el = card.locator(LIEPIN["card_title_attr"]).first
                    if await t_el.count():
                        title = (await t_el.get_attribute("title") or "").strip()
                except Exception:  # noqa: BLE001
                    pass
                if not title:
                    continue
                jid = _job_id_from_url(href)
                if not jid:
                    continue
                text = (await card.inner_text()) or ""
                m = re.search(r"【([^】]+)】", text)
                area = m.group(1) if m else ""
                city_name, district = (area.split("-", 1) + [""])[:2] if area else (city, "")
                m_sal = re.search(r"(\d+(?:\.\d+)?\s*[-~至]\s*\d+(?:\.\d+)?\s*[kK万]|\d+(?:\.\d+)?\s*[kK万])", text)
                salary = m_sal.group(1).replace(" ", "") if m_sal else ""
                # 经验/学历/公司/HR/活跃(文本模式,与类名无关)
                m_exp = re.search(r"(\d+[-~至]\d+年|\d+年以上|经验不限|应届生|在校生)", text)
                m_edu = re.search(r"(博士|硕士|本科|大专|中专|中技|初中及以下|学历不限)", text)
                m_hr = re.search(r"([\u4e00-\u9fa5A-Za-z·]{2,12}·[\u4e00-\u9fa5A-Za-z]{2,10})", text)
                m_act = re.search(r"(刚刚在线|今日在线|昨日在线|\d+分钟前在线|\d+小时前在线|\d+天前在线|\d+周前在线|\d+个月?前在线|长期未在线)", text)
                company = ""
                try:
                    c_el = card.locator(".companyName").first
                    if await c_el.count():
                        company = ((await c_el.text_content()) or "").strip()
                except Exception:  # noqa: BLE001
                    pass
                drafts.append(
                    JobDraft(
                        platform_job_id=jid,
                        title=title,
                        url=href,
                        company=company,
                        salary=salary,
                        salary_months=parse_salary_months(salary),
                        city=city_name or city,
                        district=district.strip(),
                        experience=m_exp.group(1) if m_exp else "",
                        education=m_edu.group(1) if m_edu else "",
                        hr_name=m_hr.group(1).split("·")[0] if m_hr else "",
                        hr_title=m_hr.group(1).split("·")[-1] if m_hr else "",
                        hr_active_at=_active_from_text(m_act.group(1)) if m_act else "",
                        hr_active_text=m_act.group(1) if m_act else "",
                        raw={"tags": text.split("\n"), "source": "dom"},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("猎聘卡片解析失败: %s", exc)
        if not drafts:
            html = await page.content()
            save_snapshot("liepin", "list_empty", html[:200000])
            if warn is not None:
                await warn(
                    ["猎聘列表解析出 0 个岗位(接口响应与DOM均未命中,页面结构可能已变化)"],
                    page.url or "",
                )
        return drafts

    # ---------- 详情解析 ----------
    async def _parse_detail(self, page: Page, draft: JobDraft, should_stop=None) -> None:
        """详情页:JSON-LD(schema.org JobPosting)优先,DOM 兜底;落地校验+风控/blank 检测。"""
        # 详情页节奏:随机间隔(任务级设置,默认 3~8 秒),模拟人工浏览
        await detail_delay(self.id)
        if should_stop is not None and should_stop():
            return
        try:
            await page.goto(draft.url or self.home_url, wait_until="domcontentloaded", timeout=25000)
        except Exception:  # noqa: BLE001
            pass
        # 落地校验:URL 需包含岗位 ID(防平台重定向)
        landed = False
        for _ in range(8):
            if should_stop is not None and should_stop():
                return
            if draft.platform_job_id in (page.url or ""):
                landed = True
                break
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=1500)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        if not landed:
            # 单次导航即定论,不做二次硬闯(反复刷新加重风控)
            logger.warning("猎聘详情落地异常(视为已下线,跳过): %s -> %s", draft.title, (page.url or "")[:120])
            draft.raw["offline"] = True
            return
        if "about:blank" in (page.url or "") or not ((await page.title()) or "").strip():
            logger.warning("猎聘详情页被清空(反自动化拦截): %s", draft.title)
            draft.raw["blanked"] = True
            return
        await self._raise_if_verify(page)
        await page.wait_for_timeout(1500)
        # JSON-LD(schema.org JobPosting,实测存在且描述完整)
        try:
            ld_list = await page.evaluate(
                "() => Array.from(document.querySelectorAll('script[type=\"application/ld+json\"]')).map(s => s.textContent)"
            )
            for raw in ld_list or []:
                try:
                    obj = json.loads(raw)
                except Exception:
                    continue
                for o in obj if isinstance(obj, list) else [obj]:
                    if isinstance(o, dict) and str(o.get("@type", "")).lower() == "jobposting":
                        draft.title = draft.title or str(o.get("title") or "")
                        draft.description = strip_html(o.get("description") or "") or draft.description
                        org = o.get("hiringOrganization") or {}
                        if isinstance(org, dict):
                            draft.company = draft.company or str(org.get("name") or "")
                        if o.get("datePosted") and not draft.published_at:
                            draft.published_at = parse_relative_time(str(o["datePosted"])) or draft.published_at
        except Exception as exc:  # noqa: BLE001
            logger.debug("猎聘 JSON-LD 解析失败: %s", exc)
        # DOM 兜底:描述与 HR 区块
        if not draft.description:
            try:
                el = page.locator(LIEPIN["detail_desc"]).first
                if await el.count():
                    draft.description = ((await el.text_content()) or "").strip()
            except Exception:  # noqa: BLE001
                pass
        if not draft.hr_name:
            try:
                el = page.locator(LIEPIN["detail_hr"]).first
                if await el.count():
                    text = ((await el.text_content()) or "").strip()
                    draft.hr_name = text.split("\n")[0][:40]
                    m = re.search(r"([\d月日天内周]+活跃|刚刚活跃|今日活跃|在线)", text)
                    if m and not draft.hr_active_at:
                        draft.hr_active_text = m.group(1)
                        draft.hr_active_at = _active_from_text(m.group(1))
            except Exception:  # noqa: BLE001
                pass
        if not draft.description and draft.url:
            html = await page.content()
            save_snapshot("liepin", "detail_no_desc", html[:200000])
            draft.raw.setdefault("_warnings", []).append(
                "猎聘详情页未解析到职位描述(页面结构可能已变化)"
            )

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
        dp = detail_page
        sal_rng = (extras or {}).get("salary") or ""
        try:
            seen: set[str] = set()
            for page_num in range(1, max(1, max_pages) + 1):
                if should_stop is not None and should_stop():
                    return
                url = self._search_url(city, keyword, page_num)
                if progress:
                    progress(f"打开第 {page_num} 页: {keyword}@{city or '全国'}")
                # 导航驱动 + 响应监听:页面自己发的签名请求,捕获响应拿结构化数据
                data = None
                try:
                    async with page.expect_response(
                        lambda r: "pc-search-job" in r.url and "cond-init" not in r.url,
                        timeout=20000,
                    ) as ri:
                        try:
                            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                        except PWTimeout:
                            pass
                        resp = await ri.value
                        body = await resp.text()
                    d = json.loads(body)
                    if d.get("flag") == 1:
                        data = d
                except Exception as exc:  # noqa: BLE001
                    logger.debug("猎聘响应捕获失败(走DOM兜底): %s", str(exc)[:120])
                if re.search(r"/login|passport", (page.url or "")):
                    raise LoginRequired("猎聘跳转到登录页")
                await self._raise_if_verify(page)
                if data is not None:                    drafts = await self._parse_list_json(data, city, warn)
                else:
                    await self._wait_list(page)
                    drafts = await self._parse_list_dom(page, city, warn)
                fresh = [d for d in drafts if d.platform_job_id and d.platform_job_id not in seen]
                for d in drafts:
                    seen.add(d.platform_job_id)
                if not fresh and progress:
                    # 不提前停止:整页重复≠结果翻完(排序微调常致页间重叠),
                    # 持续翻到设定页数上限为止(手动/异常中断除外)
                    progress(f"第 {page_num} 页无新岗位,继续翻页(翻完设定页数为止)")
                if progress:
                    progress(f"第 {page_num} 页解析出 {len(fresh)} 个岗位")
                sal_filtered = 0
                for draft in fresh:
                    # 薪资范围过滤:列表阶段直接排除,不打开详情页(解析失败不误杀)
                    if sal_rng and draft.salary:
                        in_rng = salary_in_range(draft.salary, sal_rng)
                        if in_rng is False:
                            sal_filtered += 1
                            continue
                    # 详情前查重:已入库岗位不再打开详情页
                    if skip_check and await skip_check(draft.platform_job_id):
                        yield draft
                        continue
                    if should_stop is not None and should_stop():
                        return
                    if draft.url:
                        if dp is None:
                            dp = await page.context.new_page()
                        await self._parse_detail(dp, draft, should_stop=should_stop)
                    yield draft
                if sal_filtered and progress:
                    progress(f"第 {page_num} 页有 {sal_filtered} 个岗位薪资范围外,已过滤")
                if not fresh and page_num > 1 and not drafts:
                    # 空页(接口返回空列表/DOM 无卡片)= 结果翻完,提前结束
                    if progress:
                        progress(f"第 {page_num} 页为空页,已翻完全部结果")
                    break
                if page_num < max_pages:
                    await human_scroll(page)
                    await human_delay(self.id)
        finally:
            if own_detail and dp is not None:
                try:
                    await dp.close()
                except Exception:  # noqa: BLE001
                    pass


register(LiepinPlatform())