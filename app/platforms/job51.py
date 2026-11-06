"""前程无忧(51job)适配器。

优先解析列表/详情页内嵌 JSON(window.__SEARCH_RESULT__ 与 JSON-LD),
DOM 选择器仅作兜底;所有选择器见 selectors.JOB51。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import AsyncIterator
from urllib.parse import quote

from playwright.async_api import Page, TimeoutError as PWTimeout

from ..utils import human_delay, human_scroll, parse_relative_time, save_snapshot
from . import register
from .base import AntiBot, BasePlatform, JobDraft, LoginRequired
from .common import deep_find_list, parse_salary_months, salary_in_range, strip_html
from .selectors import JOB51

logger = logging.getLogger(__name__)

_ID_PATTERNS = [
    r"/all/(\d+)\.html",
    r"/(\d{6,})\.html",
    r"jobId=(\w+)",
]


def _extract_job_id(url: str) -> str:
    for pat in _ID_PATTERNS:
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return re.sub(r"\W+", "_", url)[-60:]


def _company_of(j: dict) -> str:
    c = j.get("company")
    if isinstance(c, dict):
        return str(c.get("companyName") or c.get("name") or "")
    return str(c or j.get("companyName") or "")


class Job51Platform(BasePlatform):
    id = "job51"
    name = "前程无忧"
    home_url = "https://www.51job.com"
    home_prefix = "https://we.51job.com"

    def supported_cities(self) -> set[str]:
        return set(JOB51["city_codes"]) - {"全国"}

    # ---------- 登录态 ----------
    async def is_logged_in(self, page: Page) -> bool:
        try:
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(2000)
        entry = page.locator(JOB51["login_entry"]).first
        try:
            if await entry.is_visible():
                return False
        except Exception:
            pass
        # 兜底:页面出现明确的未登录文案时判定未登录(登录入口选择器可能失效)
        try:
            body = (await page.evaluate("() => (document.body?.innerText||'').slice(0, 3000)")) or ""
            if re.search(r"请登录|Hi[,，]\s*游客|登录/注册", body):
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

    # ---------- 搜索 ----------
    def _search_url(self, city: str, keyword: str, page_num: int, extras: dict) -> str:
        area = JOB51["city_codes"].get(city, JOB51["city_codes"]["全国"]) if city else JOB51["city_codes"]["全国"]
        sort_type = str(extras.get("sortType", "0"))
        params = {
            "keyword": keyword,
            "searchType": "2",
            "sortType": sort_type,
            "jobArea": area,
            "pageSize": "50",
            "pageNum": str(page_num),
        }
        qs = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in params.items())
        return f"https://we.51job.com/pc/search?{qs}"

    async def _wait_list(self, page: Page) -> None:
        try:
            await page.wait_for_selector(".j_joblist, div[class*='joblist']", timeout=10000)
        except PWTimeout:
            # 列表容器选择器可能失效,仍然检查内嵌 JSON
            pass
        await page.wait_for_timeout(1500)

    async def _extract_global(self, page: Page):
        for name in JOB51["result_globals"]:
            try:
                data = await page.evaluate(f"() => {{ try {{ return window['{name.split('.', 1)[1]}'] ?? null }} catch(e) {{ return null }} }}")
                if data:
                    return data
            except Exception:
                continue
        return None

    async def _parse_list(self, page: Page, city: str, warn=None) -> list[JobDraft]:
        drafts: list[JobDraft] = []
        data = await self._extract_global(page)
        jobs = deep_find_list(data, JOB51["result_list_keys"]) if data else None
        if jobs:
            for j in jobs:
                if not isinstance(j, dict):
                    continue
                href = j.get("jobHref") or ""
                if href and not href.startswith("http"):
                    href = "https://jobs.51job.com" + ("" if href.startswith("/") else "/") + href
                url = href or j.get("jobUrl") or ""
                title = j.get("jobName") or j.get("jobTitle") or ""
                if not title:
                    # 字段异常:条目缺职位名,无法入库,记录到异常面板(带链接)
                    if warn is not None:
                        await warn(
                            ["前程无忧列表字段异常: 条目缺少职位名(jobName),该岗位已跳过(平台可能已改版)"],
                            str(url or ""),
                        )
                    continue
                area = j.get("jobAreaString") or j.get("jobArea") or ""
                district = area.split("-")[-1].strip() if "-" in area else ""
                salary = j.get("salaryDf") or j.get("salary") or ""
                months = parse_salary_months(salary)
                pub = parse_relative_time(str(j.get("emitJobTime") or j.get("lastModifyTime") or ""))
                drafts.append(
                    JobDraft(
                        platform_job_id=str(j.get("jobId") or j.get("jobIdEnc") or _extract_job_id(url) or title),
                        title=title,
                        url=url,
                        company=_company_of(j),
                        salary=salary,
                        salary_months=months,
                        city=city or (area.split("-")[0].strip() if area else ""),
                        district=district,
                        experience=j.get("workExpString") or j.get("jobRequire") or "",
                        education=j.get("degreeString") or "",
                        hr_name=j.get("hrName") or "",
                        hr_title=j.get("hrPosition") or "",
                        published_at=pub,
                        raw=j,
                    )
                )
            return drafts
        # DOM 兜底
        cards = page.locator(JOB51["card"])
        n = await cards.count()
        for i in range(n):
            card = cards.nth(i)
            try:
                link = card.locator(JOB51["card_href"]).first
                href = await link.get_attribute("href") if await link.count() else None
                url = href or ""
                if url and not url.startswith("http"):
                    url = "https://jobs.51job.com" + ("" if url.startswith("/") else "/") + url
                title = (await card.locator(JOB51["card_title"]).first.text_content() or "").strip()
                if not title:
                    continue
                company = (await card.locator(JOB51["card_company"]).first.text_content() or "").strip()
                salary = (await card.locator(JOB51["card_salary"]).first.text_content() or "").strip()
                drafts.append(
                    JobDraft(
                        platform_job_id=_extract_job_id(url) or title,
                        title=title,
                        url=url,
                        company=company,
                        salary=salary,
                        city=city,
                        raw={"source": "dom"},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("解析第 %d 个卡片失败: %s", i, exc)
        if not drafts:
            html = await page.content()
            save_snapshot("job51", "list_empty", html[:200000])
            if warn is not None:
                await warn(
                    ["前程无忧列表解析出 0 个岗位(内嵌JSON与DOM均未命中,页面结构可能已变化)"],
                    page.url or "",
                )
        return drafts

    async def _parse_detail(self, page: Page, draft: JobDraft) -> None:
        """解析详情页,补全职位描述等字段。"""
        await human_delay()
        try:
            await page.goto(draft.url or self.home_url, wait_until="domcontentloaded", timeout=25000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(1500)
        html = await page.content()
        if "请登录" in html and "password" in html.lower() and len(html) < 20000:
            raise LoginRequired("51job 详情页要求登录")
        # 1) JSON-LD
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
                        sal = o.get("baseSalary") or {}
                        if not draft.salary and isinstance(sal, dict):
                            v = sal.get("value") or {}
                            draft.salary = f"{v.get('minValue', '')}-{v.get('maxValue', '')}{sal.get('unitText', '')}".strip("-")
        except Exception as exc:  # noqa: BLE001
            logger.debug("51job JSON-LD 解析失败: %s", exc)
        # 2) DOM 兜底
        if not draft.description:
            try:
                el = page.locator(JOB51["detail_desc"]).first
                if await el.count():
                    draft.description = ((await el.text_content()) or "").strip()
            except Exception:
                pass
        if not draft.title:
            try:
                el = page.locator(JOB51["detail_title"]).first
                if await el.count():
                    draft.title = ((await el.text_content()) or "").strip()
            except Exception:
                pass
        if not draft.description and draft.url:
            save_snapshot("job51", "detail_no_desc", html[:200000])
            draft.raw.setdefault("_warnings", []).append(
                "前程无忧详情页未解析到职位描述(页面结构可能已变化)"
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
    ) -> AsyncIterator[JobDraft]:
        own_detail = detail_page is None
        # 详情标签页懒创建:全部岗位都已入库时不开任何新标签
        dp = detail_page
        sal_rng = (extras or {}).get("salary") or ""
        try:
            seen: set[str] = set()
            for page_num in range(1, max(1, max_pages) + 1):
                url = self._search_url(city, keyword, page_num, extras or {})
                if progress:
                    progress(f"打开第 {page_num} 页: {keyword}@{city or '全国'}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except PWTimeout:
                    pass
                await self._wait_list(page)
                if "passport.51job.com" in page.url or "/login" in page.url:
                    raise LoginRequired("51job 搜索页跳转到登录页")
                drafts = await self._parse_list(page, city, warn=warn)
                fresh = [d for d in drafts if d.platform_job_id not in seen]
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
                    if draft.url:
                        if dp is None:
                            dp = await page.context.new_page()
                        await self._parse_detail(dp, draft)
                    yield draft
                if sal_filtered and progress:
                    progress(f"第 {page_num} 页有 {sal_filtered} 个岗位薪资范围外,已过滤")
                if page_num < max_pages:
                    await human_scroll(page)
                    await human_delay()
        finally:
            if own_detail and dp is not None:
                try:
                    await dp.close()
                except Exception:
                    pass


register(Job51Platform())