"""猎聘(liepin.com)适配器。

SSR 数据(window.__INITIAL_STATE__)优先,JSON-LD 详情兜底,DOM 兜底解析列表。
"""
from __future__ import annotations

import json
import logging
import re
from typing import AsyncIterator
from urllib.parse import quote

from playwright.async_api import Page, TimeoutError as PWTimeout

from ..utils import (
    active_estimate_from_text,
    fmt_dt,
    human_delay,
    human_scroll,
    parse_relative_time,
    save_snapshot,
)
from . import register
from .base import AntiBot, BasePlatform, JobDraft, LoginRequired
from .common import deep_find_list, parse_salary_months, read_window_global, salary_in_range, strip_html
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
    m = re.search(r"/job/([A-Za-z0-9]+)\.shtml", url or "")
    if m:
        return m.group(1)
    return re.sub(r"\W+", "_", (url or ""))[-60:]


class LiepinPlatform(BasePlatform):
    id = "liepin"
    name = "猎聘"
    home_url = "https://www.liepin.com"
    home_prefix = "https://www.liepin.com"

    # ---------- 登录态 ----------
    async def is_logged_in(self, page: Page) -> bool:
        try:
            await page.goto(self.home_url, wait_until="domcontentloaded", timeout=20000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(2000)
        # 已登录页头显示昵称/头像;未登录有"登录"入口
        entry = page.locator("a:has-text('登录'), .login-btn").first
        try:
            if await entry.is_visible():
                return False
        except Exception:
            pass
        # 兜底:页面出现明确的未登录文案时判定未登录(登录入口选择器可能失效)
        try:
            body = (await page.evaluate("() => (document.body?.innerText||'').slice(0, 3000)")) or ""
            if re.search(r"请登录|登录/注册|您好,游客|您好，游客", body):
                return False
        except Exception:  # noqa: BLE001
            pass
        return True

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
            await page.wait_for_selector(
                ".job-card-pc-container, .job-list-box li, .sojobs li", timeout=12000
            )
        except PWTimeout:
            pass
        await page.wait_for_timeout(1500)

    # ---------- 列表解析 ----------
    async def _parse_list(self, page: Page, city: str, warn=None) -> list[JobDraft]:
        drafts: list[JobDraft] = []
        data = await read_window_global(page, LIEPIN["result_globals"])
        jobs = deep_find_list(data, LIEPIN["result_list_keys"]) if data else None
        if jobs:
            for j in jobs:
                title = j.get("title") or j.get("jobName") or j.get("name") or ""
                if not title:
                    # 字段异常:条目缺职位名,无法入库,记录到异常面板(带链接)
                    if warn is not None:
                        jid0 = str(j.get("jobId") or j.get("jobid") or j.get("id") or "")
                        u0 = f"https://www.liepin.com/job/{jid0}.shtml" if jid0 else ""
                        await warn(
                            ["猎聘列表字段异常: 条目缺少职位名,该岗位已跳过(平台可能已改版)"],
                            u0,
                        )
                    continue
                jid = str(j.get("jobId") or j.get("jobid") or j.get("id") or "")
                url = j.get("jobUrl") or (f"https://www.liepin.com/job/{jid}.shtml" if jid else "")
                salary = j.get("salary") or j.get("salaryStr") or ""
                area = j.get("cityRegion") or j.get("dq") or j.get("city") or ""
                district = area.split("-")[-1].strip() if "-" in area else ""
                active_text = str(j.get("activeTime") or j.get("bossActiveDesc") or "")
                active = _active_from_text(active_text)
                tags = j.get("jobLabels") or j.get("labels") or []
                exp = edu = ""
                for t in tags if isinstance(tags, list) else []:
                    ts = str(t)
                    if "经验" in ts or "年" in ts:
                        exp = exp or ts
                    elif "学历" in ts or "本科" in ts or "硕士" in ts or "大专" in ts:
                        edu = edu or ts
                drafts.append(
                    JobDraft(
                        platform_job_id=jid or _job_id_from_url(url) or title,
                        title=str(title),
                        url=url,
                        company=str(j.get("companyName") or j.get("company") or ""),
                        salary=salary,
                        salary_months=parse_salary_months(salary),
                        city=city or (area.split("-")[0].strip() if area else ""),
                        district=district,
                        experience=exp,
                        education=edu,
                        hr_name=str(j.get("bossName") or j.get("hrName") or ""),
                        hr_title=str(j.get("bossTitle") or j.get("hrTitle") or ""),
                        hr_active_at=active,
                        hr_active_text=active_text,
                        raw=j,
                    )
                )
            return drafts
        # DOM 兜底
        cards = page.locator(LIEPIN["card"])
        n = await cards.count()
        for i in range(n):
            card = cards.nth(i)
            try:
                link = card.locator(LIEPIN["card_link"]).first
                href = await link.get_attribute("href") if await link.count() else None
                url = href or ""
                if url and not url.startswith("http"):
                    url = "https://www.liepin.com" + ("" if url.startswith("/") else "/") + url
                title = (await card.locator(LIEPIN["card_title"]).first.text_content() or "").strip()
                if not title:
                    continue
                company = (await card.locator(LIEPIN["card_company"]).first.text_content() or "").strip()
                salary = (await card.locator(LIEPIN["card_salary"]).first.text_content() or "").strip()
                tags = await card.locator(LIEPIN["card_tags"]).all_text_contents()
                active_text = " ".join(t for t in tags if "活跃" in t).strip()
                active = _active_from_text(active_text)
                drafts.append(
                    JobDraft(
                        platform_job_id=_job_id_from_url(url) or title,
                        title=title,
                        url=url,
                        company=company,
                        salary=salary,
                        salary_months=parse_salary_months(salary),
                        city=city,
                        hr_active_at=active,
                        hr_active_text=active_text,
                        raw={"tags": tags, "source": "dom"},
                    )
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug("猎聘卡片解析失败: %s", exc)
        if not drafts:
            html = await page.content()
            save_snapshot("liepin", "list_empty", html[:200000])
            if warn is not None:
                await warn(
                    ["猎聘列表解析出 0 个岗位(SSR数据与DOM均未命中,页面结构可能已变化)"],
                    page.url or "",
                )
        return drafts

    # ---------- 详情解析 ----------
    async def _parse_detail(self, page: Page, draft: JobDraft) -> None:
        await human_delay()
        try:
            await page.goto(draft.url or self.home_url, wait_until="domcontentloaded", timeout=25000)
        except PWTimeout:
            pass
        await page.wait_for_timeout(1500)
        # JSON-LD
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
                            draft.published_at = parse_relative_time(str(o["datePosted"]))
        except Exception as exc:  # noqa: BLE001
            logger.debug("猎聘 JSON-LD 解析失败: %s", exc)
        # DOM 兜底:描述与 HR 区块
        if not draft.description:
            try:
                el = page.locator(LIEPIN["detail_desc"]).first
                if await el.count():
                    draft.description = ((await el.text_content()) or "").strip()
            except Exception:
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
            except Exception:
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
    ) -> AsyncIterator[JobDraft]:
        own_detail = detail_page is None
        # 详情标签页懒创建:全部岗位都已入库时不开任何新标签
        dp = detail_page
        sal_rng = (extras or {}).get("salary") or ""
        try:
            seen: set[str] = set()
            for page_num in range(1, max(1, max_pages) + 1):
                url = self._search_url(city, keyword, page_num)
                if progress:
                    progress(f"打开第 {page_num} 页: {keyword}@{city or '全国'}")
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                except PWTimeout:
                    pass
                await self._wait_list(page)
                if re.search(r"/login|passport", page.url):
                    raise LoginRequired("猎聘跳转到登录页")
                drafts = await self._parse_list(page, city)
                fresh = [d for d in drafts if d.platform_job_id and d.platform_job_id not in seen]
                for d in drafts:
                    seen.add(d.platform_job_id)
                if not fresh and progress:
                    # 不提前停止:整页重复≠结果翻完(排序微调常致页间重叠),
                    # 持续翻到设定页数上限为止(手动/异常中断除外)
                    progress(f"第 {page_num} 页无新岗位,继续翻页(翻完设定页数为止)")
                if progress:
                    progress(f"第 {page_num} 页解析出 {len(fresh)} 个岗位")
                for draft in fresh:
                    # 详情前查重:已入库岗位不再打开详情页
                    if skip_check and await skip_check(draft.platform_job_id):
                        yield draft
                        continue
                    if draft.url:
                        if dp is None:
                            dp = await page.context.new_page()
                        await self._parse_detail(dp, draft)
                    yield draft
                if page_num < max_pages:
                    await human_scroll(page)
                    await human_delay()
        finally:
            if own_detail and dp is not None:
                try:
                    await dp.close()
                except Exception:
                    pass


register(LiepinPlatform())