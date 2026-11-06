"""抓取调度器:每平台最多一个并发任务;平台之间相互独立。

DB 均为同步 SQLite 操作,统一经 asyncio.to_thread 执行,避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

from ..browser import browser_manager
from ..config import config
from ..filter import evaluate_job
from ..models import AppEvent, CrawlTask, Gear, Job, SearchFilter, SessionLocal
from ..models import now_local
from ..platforms import get_platform
from ..platforms.base import AntiBot, JobDraft, LoginRequired

logger = logging.getLogger(__name__)


class AlreadyRunning(Exception):
    """该平台已有任务在运行。"""


# ---------- 独立的同步 DB 帮助函数(供 to_thread 调用) ----------
def _db_load_condition(filter_id: int, platform: str) -> tuple[str, list, str, str, dict]:
    with SessionLocal() as db:
        flt = db.get(SearchFilter, filter_id)
        if not flt:
            raise ValueError(f"筛选条件不存在: {filter_id}")
        if flt.platform != platform:
            raise ValueError(f"筛选条件属于平台 {flt.platform},与目标平台 {platform} 不符")
        gear = db.get(Gear, flt.gear_id) if flt.gear_id else None
        return (
            gear.name if gear else "",
            list(gear.keywords) if gear else [],
            flt.city or "",
            flt.keyword or "",
            dict(flt.extras or {}),
        )


def _db_load_gear(gear_id: int) -> tuple[str, list]:
    with SessionLocal() as db:
        gear = db.get(Gear, gear_id)
        if not gear:
            raise ValueError(f"档位不存在: {gear_id}")
        return gear.name, list(gear.keywords or [])


def _db_create_task(platform: str, snapshot: dict) -> int:
    with SessionLocal() as db:
        row = CrawlTask(platform=platform, filter_snapshot=snapshot, status="running")
        db.add(row)
        db.commit()
        return row.id


def _db_load_existing_ids(platform: str) -> set[str]:
    """任务启动时一次性加载该平台已有岗位 ID,供详情前查重(内存级,零 DB 压力)。"""
    with SessionLocal() as db:
        rows = db.query(Job.platform_job_id).filter(Job.platform == platform).all()
    return {r[0] for r in rows}


def _db_finish_task(task_id: int, status: str, message: str, counters: dict) -> None:
    with SessionLocal() as db:
        row = db.get(CrawlTask, task_id)
        if row:
            row.status = status
            row.message = message
            row.found = counters.get("found", 0)
            row.saved = counters.get("saved", 0)
            row.skipped = counters.get("skipped", 0)
            row.finished_at = now_local()
            db.commit()


def _db_exists_touch(platform: str, job_id: str, hr_active_at) -> bool:
    """查重;已存在则顺带刷新活跃时间。返回 True 表示已存在。"""
    with SessionLocal() as db:
        exists = (
            db.query(Job)
            .filter(Job.platform == platform, Job.platform_job_id == job_id)
            .first()
        )
        if exists:
            exists.hr_active_at = hr_active_at or exists.hr_active_at
            db.commit()
            return True
    return False


# 已存在岗位可被列表数据更新的字段(不打开详情页也能拿到;空值不覆盖,避免解析失败污染)
_REFRESH_FIELDS = (
    "title", "company", "salary", "salary_months", "city", "district",
    "experience", "education", "hr_name", "hr_title",
    "industry", "company_stage", "company_scale", "welfare",
    "url",
)


def _db_update_existing(platform: str, job_id: str, draft: JobDraft) -> int:
    """重复岗位:只更新有变化的字段(活跃估算/薪资/行业等),不重复入库。

    返回发生变化的字段数,用于进度提示。
    """
    with SessionLocal() as db:
        row = (
            db.query(Job)
            .filter(Job.platform == platform, Job.platform_job_id == job_id)
            .first()
        )
        if not row:
            return 0
        changed = 0
        for f in _REFRESH_FIELDS:
            new = getattr(draft, f, None)
            if new in (None, ""):
                continue
            if getattr(row, f) != new:
                setattr(row, f, new)
                changed += 1
        # HR 活跃:本次解析到估算字符串/原文才刷新
        if draft.hr_active_at:
            if row.hr_active_at != draft.hr_active_at:
                row.hr_active_at = draft.hr_active_at
                changed += 1
            if draft.hr_active_text and row.hr_active_text != draft.hr_active_text:
                row.hr_active_text = draft.hr_active_text
                changed += 1
        # 打开参数(securityId 等):有新值则刷新
        if draft.open_params:
            if dict(row.extra or {}) != dict(draft.open_params):
                row.extra = dict(draft.open_params)
                changed += 1
        # HR 在线状态是实时布尔,直接覆盖
        if bool(row.hr_online) != bool(draft.hr_online):
            row.hr_online = bool(draft.hr_online)
            changed += 1
        if changed:
            db.commit()
        return changed


def _db_add_events(platform: str, messages: list[str]) -> int:
    """异常事件落库(左下角面板)。24 小时内相同消息去重,防止刷屏;返回新增条数。"""
    if not messages:
        return 0
    added = 0
    with SessionLocal() as db:
        cutoff = now_local() - timedelta(hours=24)
        existing = {
            m for (m,) in db.query(AppEvent.message)
            .filter(AppEvent.platform == platform, AppEvent.created_at >= cutoff)
            .all()
        }
        for msg in dict.fromkeys(messages):     # 保序去重
            msg = str(msg or "").strip()
            if not msg or msg in existing:
                continue
            db.add(AppEvent(platform=platform, level="warn", message=msg))
            existing.add(msg)
            added += 1
        if added:
            db.commit()
    return added


def _db_insert_job(plat_id: str, draft: JobDraft, result, keyword: str, city: str) -> None:
    with SessionLocal() as db:
        job = Job(
            platform=plat_id,
            platform_job_id=draft.platform_job_id,
            title=draft.title,
            company=draft.company,
            salary=draft.salary,
            salary_months=draft.salary_months,
            city=draft.city or city,
            district=draft.district,
            experience=draft.experience,
            education=draft.education,
            description=draft.description,
            company_intro=draft.company_intro,
            industry=getattr(draft, "industry", ""),
            company_stage=getattr(draft, "company_stage", ""),
            company_scale=getattr(draft, "company_scale", ""),
            welfare=getattr(draft, "welfare", ""),
            hr_online=bool(getattr(draft, "hr_online", False)),
            hr_name=draft.hr_name,
            hr_title=draft.hr_title,
            hr_active_at=(draft.hr_active_at or "")[:32],
            hr_active_text=(draft.hr_active_text or "")[:50],
            extra=dict(draft.open_params or {}),
            published_at=draft.published_at,
            url=draft.url,
            keyword=keyword,
            remark=result.remark,
            fit=result.fit,
            fit_score=result.fit_score,
        )
        db.add(job)
        db.commit()


class Scheduler:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._stops: dict[str, asyncio.Event] = {}
        self._progress: dict[str, str] = {}  # platform -> 最近一条进度文本
        self._counters: dict[str, dict] = {}  # platform -> 实时计数

    # ---------- 状态 ----------
    def is_running(self, platform: str) -> bool:
        task = self._tasks.get(platform)
        return bool(task and not task.done())

    def status(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for pid, task in self._tasks.items():
            out[pid] = {
                "running": not task.done(),
                "progress": self._progress.get(pid, ""),
                "counters": self._counters.get(pid, {}),
            }
        return out

    def request_stop(self, platform: str) -> bool:
        ev = self._stops.get(platform)
        if ev and self.is_running(platform):
            ev.set()
            return True
        return False

    # ---------- 启动 ----------
    async def start(
        self,
        platform: str,
        *,
        filter_id: int | None = None,
        gear_id: int | None = None,
        keyword: str | None = None,
        city: str | None = None,
        max_pages: int | None = None,
        all_keywords: bool = True,
    ) -> int:
        """启动抓取任务。

        条件来源优先级:filter_id(已存条件) > gear_id(直选档位) > keyword(临时词)。
        all_keywords=True 时跑档位的全部词条(对齐实例的多关键词批量采集)。
        """
        if self.is_running(platform):
            raise AlreadyRunning(f"平台 {platform} 已有任务在运行(每平台仅允许一个)")

        plat = get_platform(platform)
        # 解析条件
        gear_name, gear_keywords, f_city, f_kw, extras = "", [], "", "", {}
        if filter_id:
            gear_name, gear_keywords, f_city, f_kw, extras = await asyncio.to_thread(
                _db_load_condition, filter_id, platform
            )
        elif gear_id:
            gear_name, gear_keywords = await asyncio.to_thread(_db_load_gear, gear_id)

        city = city if city is not None else f_city
        # 关键词列表:显式 keyword 优先(单词条);否则用档位全部/首个词条
        if keyword:
            kw_list = [keyword]
        elif gear_keywords:
            kw_list = list(gear_keywords) if all_keywords else [gear_keywords[0]]
        else:
            raise ValueError("缺少搜索关键词(未选择档位/条件且未提供 keyword)")

        max_pages = max_pages or config.MAX_PAGES_PER_TASK
        snapshot = {
            "city": city, "keyword": "、".join(kw_list), "gear": gear_name,
            "keywords": kw_list, "extras": extras, "max_pages": max_pages,
        }

        task_id = await asyncio.to_thread(_db_create_task, platform, snapshot)

        self._stops[platform] = asyncio.Event()
        self._progress[platform] = "启动中..."
        self._counters[platform] = {"found": 0, "saved": 0, "skipped": 0}
        self._tasks[platform] = asyncio.create_task(
            self._run(plat, task_id, snapshot), name=f"crawl-{platform}"
        )
        return task_id

    # ---------- 任务体 ----------
    async def _run(self, plat, task_id: int, snap: dict) -> None:
        platform = plat.id
        stop = self._stops[platform]
        counters = self._counters[platform]

        def progress(msg: str) -> None:
            self._progress[platform] = f"[任务{task_id}] {msg}"
            logger.info("%s", self._progress[platform])

        status, message = "done", ""
        try:
            browser = await browser_manager.ensure()
            page = await browser_manager.get_or_open_page(
                plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker
            )
            if not await plat.is_logged_in(page):
                raise LoginRequired(f"{plat.name} 未登录,请在浏览器窗口中登录后重试")
            progress(f"{plat.name} 登录态正常,开始抓取")

            # 详情标签页由适配器按需懒创建(iter_jobs 内),不再预开空白标签
            found = saved = skipped = 0
            seen_ids: set[str] = set()  # 跨词条内存去重(同一岗位可能被多个词条搜出)
            # 已入库岗位集合:详情前查重用,避免重复打开已抓过的详情页(风控主因)
            existing_ids = await asyncio.to_thread(_db_load_existing_ids, platform)

            async def skip_known(jid: str) -> bool:
                return jid in existing_ids

            kw_list: list[str] = snap.get("keywords") or []
            for ki, kw in enumerate(kw_list, start=1):
                if stop.is_set():
                    status, message = "stopped", "用户手动停止"
                    break
                progress(f"词条 {ki}/{len(kw_list)}: 「{kw}」开始")
                async for draft in plat.iter_jobs(
                    page,
                    city=snap["city"],
                    keyword=kw,
                    extras=snap["extras"],
                    max_pages=snap["max_pages"],
                    detail_page=None,
                    progress=progress,
                    skip_check=skip_known,
                ):
                    if stop.is_set():
                        status, message = "stopped", "用户手动停止"
                        break
                    if draft.platform_job_id in seen_ids:
                        skipped += 1
                        counters.update(found=found, saved=saved, skipped=skipped)
                        continue
                    seen_ids.add(draft.platform_job_id)
                    found += 1
                    counters.update(found=found)
                    outcome = await self._process_one(plat, draft, snap, progress)
                    if outcome == "saved":
                        saved += 1
                    else:
                        skipped += 1
                    counters.update(saved=saved, skipped=skipped)
                if ki < len(kw_list) and not stop.is_set():
                    from ..utils import human_delay

                    await human_delay()
            if not stop.is_set() and status == "done":
                status = "done"

            if status == "done":
                message = (
                    f"完成:{len(kw_list)} 个词条,发现 {found},新入库 {saved},跳过 {skipped}"
                )
        except LoginRequired as exc:
            status, message = "error", str(exc)
        except AntiBot as exc:
            status, message = "error", f"触发平台风控: {exc}(请稍后在浏览器中人工验证后重试)"
        except Exception as exc:  # noqa: BLE001
            logger.exception("抓取任务异常")
            status, message = "error", f"{type(exc).__name__}: {exc}"
        finally:
            await asyncio.to_thread(
                _db_finish_task, task_id, status, message, dict(counters)
            )
            self._progress[platform] = f"[任务{task_id}] {message or status}"
            self._tasks.pop(platform, None)
            self._stops.pop(platform, None)
            logger.info("任务 %d 结束: %s - %s", task_id, status, message)

    async def _process_one(self, plat, draft: JobDraft, snap: dict, progress) -> str:
        """查重 → 评估 → 入库。返回 'saved' 或 'skipped'。"""
        # 适配器解析层警告(字段缺失/平台文本可能变化)持久化到异常面板
        warns = draft.raw.pop("_warnings", None) or []
        if warns:
            n = await asyncio.to_thread(_db_add_events, plat.id, [str(w) for w in warns])
            if n:
                progress(f"⚠ 记录 {n} 条解析异常(见左下角异常面板)")
        gear_name, keywords = snap.get("gear", ""), snap.get("keywords", [])
        # 对齐实例:详情页跳回首页/异常跳转视为岗位已下线,不入库
        if draft.raw.get("offline"):
            progress(f"跳过「{draft.title}」: 岗位已下线或链接失效")
            return "skipped"
        # 详情页被平台反自动化清空(blank):无有效数据,不入库并记录异常
        if draft.raw.get("blanked"):
            await asyncio.to_thread(
                _db_add_events, plat.id,
                [f"详情页被平台反自动化拦截(空白页),本轮跳过: {draft.title}"],
            )
            progress(f"跳过「{draft.title}」: 详情页被平台拦截(空白页),见异常面板")
            return "skipped"
        exists = await asyncio.to_thread(_db_update_existing, plat.id, draft.platform_job_id, draft)
        if exists:
            # 已存在:仅更新有变化的字段,不重复入库(需求:比对是否已存在)
            if exists > 0:
                progress(f"已存在「{draft.title}」: 同步更新{exists}个字段")
            else:
                progress(f"跳过「{draft.title}」: 已存在,无字段变化")
            return "skipped"

        result = await evaluate_job(
            draft, gear_name, keywords, extras=snap.get("extras") or {}
        )
        if result.dropped:
            progress(f"跳过「{draft.title}」: {result.drop_reason}")
            return "skipped"

        await asyncio.to_thread(
            _db_insert_job, plat.id, draft, result, snap.get("keyword", ""), snap.get("city", "")
        )
        note = f"(备注: {result.remark})" if result.remark else ""
        progress(f"入库「{draft.title}」@{draft.company}{note}")
        return "saved"


scheduler = Scheduler()