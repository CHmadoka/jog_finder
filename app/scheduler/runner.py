"""抓取调度器:每平台最多一个并发任务;平台之间相互独立。

DB 均为同步 SQLite 操作,统一经 asyncio.to_thread 执行,避免阻塞事件循环。
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import NamedTuple

from sqlalchemy import or_

from ..browser import browser_manager
from ..config import config
from ..filter import evaluate_job
from ..models import AppEvent, CrawlTask, Gear, Job, JobTable, SearchFilter, SessionLocal
from ..models import now_local
from ..platforms import all_platforms, get_platform
from ..platforms.base import AntiBot, JobDraft, LoginRequired, WarnCallback

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


def _db_check_table(table_id: int) -> bool:
    """校验目标数据表是否存在。"""
    with SessionLocal() as db:
        return db.get(JobTable, table_id) is not None


def _db_create_task(platform: str, snapshot: dict, table_id: int | None) -> int:
    """每次抓取都落一条任务记录,条件字段(词条/城市/薪资范围)写入专用列。"""
    with SessionLocal() as db:
        row = CrawlTask(
            platform=platform,
            filter_snapshot=snapshot,
            keywords="|".join(snapshot.get("keywords") or []),
            city=snapshot.get("city") or "",
            salary_range=snapshot.get("salary") or "",
            status="running",
        )
        db.add(row)
        db.commit()
        return row.id


def _db_load_existing_ids(platform: str) -> set[str]:
    """任务启动时一次性加载该平台已有岗位 ID,供详情前查重(内存级,零 DB 压力)。"""
    with SessionLocal() as db:
        rows = db.query(Job.platform_job_id).filter(Job.platform == platform).all()
    return {r[0] for r in rows}


def _db_finish_task(task_id: int, status: str, message: str, counters: dict) -> None:
    """写任务结果。完成时间仅在 status=done 时写入:为空即表示意外中断或中止。"""
    with SessionLocal() as db:
        row = db.get(CrawlTask, task_id)
        if row:
            row.status = status
            row.message = message
            row.found = counters.get("found", 0)
            row.saved = counters.get("saved", 0)
            row.skipped = counters.get("skipped", 0)
            row.skip_stats = dict(counters.get("skip_stats") or {})
            if status == "done":
                row.finished_at = now_local()
            db.commit()


# 已存在岗位可被列表数据更新的字段(不打开详情页也能拿到;空值不覆盖,避免解析失败污染)
_REFRESH_FIELDS = (
    "title", "company", "salary", "salary_months", "city", "district",
    "experience", "education", "hr_name", "hr_title",
    "industry", "company_stage", "company_scale", "welfare",
    "url",
)


class UpsertResult(NamedTuple):
    """重复岗位处理结果。

    exists 与 changed 解耦:岗位已存在但字段无变化时 changed=0,
    仍不是新岗位——调用方以 exists 判定,避免"返回 0 = 不存在"的歧义
    把无变化的重复岗位误判为新岗位而重复 INSERT。
    """

    exists: bool
    changed: int


def _db_update_existing(
    platform: str, job_id: str, draft: JobDraft, task_id: int | None = None,
    source_kw: str = "",
) -> UpsertResult:
    """重复岗位:只更新有变化的字段(活跃估算/薪资/行业等),不重复入库。

    返回 UpsertResult:exists 标识岗位是否已存在,changed 为发生变化的
    字段数;有变化时把 crawl_task_id 更新为本次任务
    (外键语义:最后更新该岗位的抓取)。
    """
    with SessionLocal() as db:
        row = (
            db.query(Job)
            .filter(Job.platform == platform, Job.platform_job_id == job_id)
            .first()
        )
        if not row:
            return UpsertResult(exists=False, changed=0)
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
        # 来源词条:岗位被其它词条/任务再次发现时对齐"最后更新"语义
        if source_kw and row.source_keyword != source_kw:
            row.source_keyword = source_kw
            changed += 1
        if changed:
            if task_id:
                row.crawl_task_id = task_id
            db.commit()
        return UpsertResult(exists=True, changed=changed)


def _db_load_missing_active(platform: str) -> list[dict]:
    """加载活跃字段缺失的岗位(旧数据修复对象)。"""
    with SessionLocal() as db:
        rows = (
            db.query(Job)
            .filter(
                Job.platform == platform,
                or_(Job.hr_active_at.is_(None), Job.hr_active_at == ""),
            )
            .order_by(Job.id)
            .all()
        )
        return [
            {
                "id": r.id,
                "platform_job_id": r.platform_job_id,
                "title": r.title or "",
                "url": r.url or "",
                "extra": dict(r.extra or {}),
                "keyword": r.keyword or "",
            }
            for r in rows
        ]


def _db_refresh_job_active(job_id: int, draft: JobDraft) -> int:
    """旧数据修复:把详情解析出的活跃/描述回写岗位行(仅补空/更新),返回变更字段数。"""
    with SessionLocal() as db:
        row = db.get(Job, job_id)
        if not row:
            return 0
        changed = 0
        if draft.hr_active_at and draft.hr_active_at != (row.hr_active_at or ""):
            row.hr_active_at = draft.hr_active_at[:32]
            changed += 1
        if draft.hr_active_text and draft.hr_active_text != (row.hr_active_text or ""):
            row.hr_active_text = draft.hr_active_text[:50]
            changed += 1
        if not (row.description or "").strip() and (draft.description or "").strip():
            row.description = draft.description
            changed += 1
        if draft.open_params:
            merged = dict(row.extra or {})
            before = dict(merged)
            merged.update({k: v for k, v in draft.open_params.items() if v})
            if merged != before:
                row.extra = merged
                changed += 1
        if changed:
            db.commit()
        return changed


def _db_set_open_params(job_id: int, params: dict) -> bool:
    """回写重建的防重定向参数(Boss securityId/lid 等)。返回是否有变化。"""
    with SessionLocal() as db:
        row = db.get(Job, job_id)
        if not row:
            return False
        merged = dict(row.extra or {})
        merged.update({k: v for k, v in (params or {}).items() if v})
        if merged == dict(row.extra or {}):
            return False
        row.extra = merged
        db.commit()
        return True


def _db_add_events(
    platform: str, items: list, level: str = "warn"
) -> int:
    """异常事件落库(左下角面板)。

    items: 消息字符串,或 {"message": ..., "url": ...}(url 供前端点击跳转)。
    24 小时内相同消息去重,防止刷屏;返回新增条数。
    """
    if not items:
        return 0
    norm: list[tuple[str, str]] = []
    for it in items:
        if isinstance(it, dict):
            norm.append((str(it.get("message") or "").strip(), str(it.get("url") or "")))
        else:
            norm.append((str(it or "").strip(), ""))
    norm = [(m, u) for m, u in norm if m]
    if not norm:
        return 0
    added = 0
    with SessionLocal() as db:
        cutoff = now_local() - timedelta(hours=24)
        existing = {
            m for (m,) in db.query(AppEvent.message)
            .filter(AppEvent.platform == platform, AppEvent.created_at >= cutoff)
            .all()
        }
        for msg, url in dict.fromkeys(norm):    # 保序去重(以(消息,链接)为键)
            if msg in existing:
                continue
            db.add(AppEvent(platform=platform, level=level, message=msg, url=url))
            existing.add(msg)
            added += 1
        if added:
            db.commit()
    return added


def _db_insert_job(
    plat_id: str, draft: JobDraft, result, keyword: str, city: str,
    task_id: int | None = None, table_id: int | None = None,
    source_kw: str = "",
) -> None:
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
            gear_id=None,
            keyword=keyword,
            source_keyword=source_kw,
            crawl_task_id=task_id,
            table_id=table_id or 1,
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
        # platform -> 最近一次结束的任务结果 {task_id, status, message}。
        # 供 /tasks/status 返回,前端据此在任务意外中断(风控/异常)时弹窗提示。
        self._last_result: dict[str, dict] = {}
        # platform -> 运行中的 crawl_task id(删除任务接口用于拒绝删除运行中任务)
        self._running_task_ids: dict[str, int] = {}
        # platform -> 任务组合(词条×城市)进度 {"total": n, "items": [{kw, city, state}]}
        # state: pending(未开始)/running(进行中)/done(已完成)。状态卡悬停展开显示。
        self._combos: dict[str, dict] = {}

    # ---------- 状态 ----------
    def is_running(self, platform: str) -> bool:
        task = self._tasks.get(platform)
        return bool(task and not task.done())

    def status(self) -> dict[str, dict]:
        """各平台实时运行状态(内存进度)。

        必须覆盖全部已知平台,而不是只返回运行中的平台:任务结束时会从
        _tasks 移除条目,若响应缺失该平台,前端缓存的 running=true 永远
        不会刷新——表现为"任务早已停止,界面仍显示正在抓取,停止按钮
        点了没反应"。结束平台返回最终进度与最近一次任务结果(last)。
        """
        out: dict[str, dict] = {}
        for p in all_platforms():
            entry = {
                "running": False,
                "progress": self._progress.get(p.id, ""),
                "counters": self._counters.get(p.id, {}),
            }
            last = self._last_result.get(p.id)
            if last:
                entry["last"] = last
            out[p.id] = entry
        for pid, task in self._tasks.items():
            entry = out.setdefault(pid, {"running": False, "progress": "", "counters": {}})
            entry["running"] = not task.done()
            entry["progress"] = self._progress.get(pid, "")
            entry["counters"] = self._counters.get(pid, {})
            combos = self._combos.get(pid)
            if combos:
                entry["combos"] = combos
        return out

    def request_stop(self, platform: str) -> bool:
        ev = self._stops.get(platform)
        if ev and self.is_running(platform):
            ev.set()
            return True
        return False

    def running_task_id(self, platform: str) -> int | None:
        """该平台当前运行中的 crawl_task id(未运行返回 None)。"""
        if not self.is_running(platform):
            return None
        return self._running_task_ids.get(platform)

    # ---------- 启动 ----------
    async def start(
        self,
        platform: str,
        *,
        filter_id: int | None = None,
        gear_id: int | None = None,
        keyword: str | None = None,
        city: str | None = None,
        cities: list[str] | None = None,
        salary: str | None = None,
        table_id: int | None = None,
        max_pages: int | None = None,
        all_keywords: bool = True,
    ) -> int:
        """启动抓取任务。

        条件来源优先级:filter_id(已存条件) > gear_id(直选档位) > keyword(临时词)。
        all_keywords=True 时跑档位的全部词条(对齐实例的多关键词批量采集)。
        cities: 城市列表(多选,空=全国);city 为旧单值参数(兼容),cities 优先。
        salary: 薪资范围原文(如 "10-20" / "10," / ",20",单位K/月,单边可缺省)。
        table_id: 新岗位存入的目标数据表(空=默认表)。
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

        # 城市列表:显式 cities 优先;否则从已存条件 city 拆分(逗号分隔;兼容旧单值)
        if cities is None:
            cities = [c.strip() for c in (f_city or "").split(",") if c.strip()]
        cities = list(dict.fromkeys(c.strip() for c in (cities or []) if c and c.strip()))
        # 平台未收录的城市代码:启动前明确拒绝,而不是悄悄回退全国(误导用户)
        supported = plat.supported_cities()
        bad = [c for c in cities if c not in supported]
        if bad:
            raise ValueError(
                f"{plat.name} 暂未收录城市代码: {','.join(bad)}"
                + (f"。当前支持: {'、'.join(sorted(supported))}" if supported else "")
            )

        # 关键词列表:显式 keyword 优先(单词条);否则用档位全部/首个词条
        if keyword:
            kw_list = [keyword]
        elif gear_keywords:
            kw_list = list(gear_keywords) if all_keywords else [gear_keywords[0]]
        else:
            raise ValueError("缺少搜索关键词(未选择档位/条件且未提供 keyword)")

        # 薪资范围:显式传入优先,其次已存条件 extras.salary;存原文,仅比较时解析
        sal_raw = (salary or "").strip() or str(extras.get("salary") or "").strip()
        extras = {**extras, "salary": sal_raw}
        # 目标数据表校验(空=默认表)
        if table_id:
            ok = await asyncio.to_thread(_db_check_table, table_id)
            if not ok:
                raise ValueError(f"数据表不存在: {table_id}")

        max_pages = max_pages or config.MAX_PAGES_PER_TASK
        snapshot = {
            "city": ",".join(cities), "cities": cities, "keyword": "、".join(kw_list), "gear": gear_name,
            "keywords": kw_list, "extras": extras, "max_pages": max_pages,
            "salary": sal_raw, "table_id": table_id,
        }

        task_id = await asyncio.to_thread(_db_create_task, platform, snapshot, table_id)

        self._stops[platform] = asyncio.Event()
        self._progress[platform] = "启动中..."
        self._counters[platform] = {"found": 0, "saved": 0, "skipped": 0, "skip_stats": {}}
        self._running_task_ids[platform] = task_id
        self._tasks[platform] = asyncio.create_task(
            self._run(plat, task_id, snapshot), name=f"crawl-{platform}"
        )
        return task_id

    # ---------- 旧数据修复:补全 HR 活跃 ----------
    async def start_refresh(self, platform: str) -> None:
        """启动"补全活跃"任务:对 hr_active_at 为空的旧岗位重新解析详情页并回写。

        与抓取任务共用每平台互斥与进度面板;不产生 crawl_task 记录。
        """
        if self.is_running(platform):
            raise AlreadyRunning(f"平台 {platform} 已有任务在运行(每平台仅允许一个)")
        plat = get_platform(platform)
        self._stops[platform] = asyncio.Event()
        self._progress[platform] = "补全启动中..."
        self._counters[platform] = {"found": 0, "saved": 0, "skipped": 0}
        self._tasks[platform] = asyncio.create_task(
            self._run_refresh(plat), name=f"refresh-{platform}"
        )

    async def _run_refresh(self, plat) -> None:
        platform = plat.id
        stop = self._stops[platform]
        counters = self._counters[platform]

        def progress(msg: str) -> None:
            self._progress[platform] = f"[补全] {msg}"
            logger.info("%s", self._progress[platform])

        status, message = "done", ""
        dp = None
        try:
            jobs = await asyncio.to_thread(_db_load_missing_active, platform)
            if not jobs:
                message = "没有需要补全的岗位(活跃字段均已存在)"
                progress(message)
                return
            await browser_manager.ensure()
            page = await browser_manager.get_or_open_page(
                plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker
            )
            # 登录检查:未登录时详情页解析必然失败(字段全部写不进去),提前中止
            if not await plat.is_logged_in(page):
                await asyncio.to_thread(
                    _db_add_events, platform,
                    [{"message": f"补全任务中止:{plat.name} 未登录,请在浏览器窗口中登录后重试",
                      "url": plat.home_url}],
                    level="error",
                )
                raise LoginRequired(f"{plat.name} 未登录,请在浏览器窗口中登录后重试")
            progress(f"{plat.name} 登录态正常,开始补全")
            progress(f"待补全 {len(jobs)} 条")

            # 旧数据可能缺防重定向参数(Boss securityId/lid):裸直链通常仍能按
            # jobId 正常落地;若被平台重定向到其它岗位,_parse_detail 的
            # "落地 jobId 必须匹配"校验会将其标记 offline,不会写错数据。
            todo = list(jobs)
            dp = await browser_manager.primary_context().new_page()
            ok = 0
            bad = 0
            for i, j in enumerate(todo, start=1):
                if stop.is_set():
                    status, message = "stopped", "用户手动停止"
                    break
                progress(f"{i}/{len(todo)}:「{j['title'][:14]}」")
                draft = JobDraft(
                    platform_job_id=j["platform_job_id"],
                    title=j["title"],
                    url=j["url"],
                    open_params=dict(j["extra"]),
                    raw={
                        "securityId": j["extra"].get("securityId", ""),
                        "lid": j["extra"].get("lid", ""),
                    },
                )
                try:
                    await plat._parse_detail(dp, draft, should_stop=stop.is_set)
                except AntiBot as exc:
                    status, message = "error", f"触发风控: {exc}"
                    break
                except LoginRequired as exc:
                    status, message = "error", str(exc)
                    break
                if draft.raw.get("offline"):
                    await asyncio.to_thread(
                        _db_add_events, platform,
                        [{"message": f"补全:岗位已下线,跳过: {j['title']}", "url": j["url"] or plat.home_url}],
                    )
                    bad += 1
                elif draft.raw.get("blanked"):
                    await asyncio.to_thread(
                        _db_add_events, platform,
                        [{"message": f"补全:详情页被反自动化拦截,跳过: {j['title']}", "url": j["url"] or plat.home_url}],
                    )
                    bad += 1
                else:
                    changed = await asyncio.to_thread(_db_refresh_job_active, j["id"], draft)
                    if changed:
                        ok += 1
                    else:
                        bad += 1  # 平台未提供活跃信息,无字段变化
                counters.update(found=ok, skipped=bad)
            if status == "done":
                message = f"补全完成:更新 {ok},未获取 {bad},共 {len(todo)} 条"
        except LoginRequired as exc:
            status, message = "error", str(exc)
            await asyncio.to_thread(
                _db_add_events, platform,
                [{"message": f"补全任务异常: {message}", "url": plat.home_url}], level="error",
            )
        except AntiBot as exc:
            status, message = "error", f"触发平台风控: {exc}(请稍后在浏览器中人工验证后重试)"
            await asyncio.to_thread(
                _db_add_events, platform,
                [{"message": f"补全任务异常: {message}", "url": plat.home_url}], level="error",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("补全任务异常")
            status, message = "error", f"{type(exc).__name__}: {exc}"
            await asyncio.to_thread(
                _db_add_events, platform,
                [{"message": f"补全任务异常: {message}", "url": plat.home_url}], level="error",
            )
        finally:
            if dp is not None:
                # 停在安全验证页时保留标签(验证页只替换内容不改URL,须检测页面文本),
                # 供人工完成滑块后平台自动回跳
                keep = False
                try:
                    txt = await dp.evaluate(
                        "() => (document.title||'') + ((document.body?.innerText||'').slice(0, 2000))"
                    )
                    keep = any(k in (txt or "") for k in ("安全验证", "安全挑战", "拖动滑块", "验证码"))
                except Exception:  # noqa: BLE001
                    keep = False
                if not keep:
                    try:
                        await dp.close()
                    except Exception:  # noqa: BLE001
                        pass
            self._progress[platform] = f"[补全] {message or status}"
            self._last_result[platform] = {"task_id": None, "status": status, "message": message}
            self._tasks.pop(platform, None)
            self._stops.pop(platform, None)
            logger.info("补全任务结束: %s - %s", status, message)

    # ---------- 任务体 ----------
    async def _run(self, plat, task_id: int, snap: dict) -> None:
        platform = plat.id
        stop = self._stops[platform]
        counters = self._counters[platform]

        def progress(msg: str) -> None:
            self._progress[platform] = f"[任务{task_id}] {msg}"
            logger.info("%s", self._progress[platform])

        status, message = "done", ""
        skip_stats: dict[str, int] = {}

        def bump(reason: str) -> None:
            skip_stats[reason] = skip_stats.get(reason, 0) + 1

        async def warn_cb(msgs: list[str], url: str) -> None:
            """适配器上报的解析层异常:持久化到异常面板(带跳转链接)。"""
            items = [{"message": str(m), "url": url or ""} for m in msgs]
            n = await asyncio.to_thread(_db_add_events, platform, items)
            if n:
                progress(f"⚠ 记录 {n} 条字段解析异常(见左下角异常面板,可点击跳转)")

        try:
            browser = await browser_manager.ensure()
            page = await browser_manager.get_or_open_page(
                plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker
            )
            # 登录检查:目标平台未登录时直接中止,避免抓到残缺数据
            if not await plat.is_logged_in(page):
                await asyncio.to_thread(
                    _db_add_events, platform,
                    [{"message": f"抓取中止:{plat.name} 未登录,请在浏览器窗口中登录后重试",
                      "url": plat.home_url}],
                    level="error",
                )
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
            # 城市列表:多选城市逐一检索(空列表=全国);兼容旧快照的单 city 字段
            cities = list(snap.get("cities") or [])
            if not cities and snap.get("city"):
                cities = [c.strip() for c in str(snap["city"]).split(",") if c.strip()]
            combos = [(kw, c) for kw in kw_list for c in (cities or [""])]
            total = len(combos)
            # 组合进度(状态卡悬停展开显示):每组合 {kw, city, state: pending/running/done}
            combo_items = [{"kw": kw, "city": c, "state": "pending"} for (kw, c) in combos]
            self._combos[platform] = {"total": total, "items": combo_items}
            for idx, (kw, cur_city) in enumerate(combos, start=1):
                if stop.is_set():
                    status, message = "stopped", "用户手动停止"
                    break
                combo_items[idx - 1]["state"] = "running"
                loc = f"@{cur_city}" if cur_city else "@全国"
                progress(f"任务组合 {idx}/{total}: 「{kw}」{loc} 开始")
                async for draft in plat.iter_jobs(
                    page,
                    city=cur_city,
                    keyword=kw,
                    extras=snap["extras"],
                    max_pages=snap["max_pages"],
                    detail_page=None,
                    progress=progress,
                    skip_check=skip_known,
                    warn=warn_cb,
                    should_stop=stop.is_set,
                ):
                    if stop.is_set():
                        status, message = "stopped", "用户手动停止"
                        break
                    if draft.platform_job_id in seen_ids:
                        skipped += 1
                        bump("同轮重复")
                        counters.update(found=found, saved=saved, skipped=skipped, skip_stats=skip_stats)
                        continue
                    seen_ids.add(draft.platform_job_id)
                    found += 1
                    counters.update(found=found)
                    outcome = await self._process_one(
                        plat, draft, snap, progress, task_id, bump, cur_city, kw
                    )
                    if outcome == "saved":
                        saved += 1
                    else:
                        skipped += 1
                    counters.update(saved=saved, skipped=skipped, skip_stats=skip_stats)
                if idx < total and not stop.is_set():
                    from ..utils import human_delay

                    await human_delay()
                if not stop.is_set():
                    combo_items[idx - 1]["state"] = "done"
            if not stop.is_set() and status == "done":
                status = "done"

            brief = f"发现 {found},新入库 {saved},跳过 {skipped}"
            if status == "done":
                detail = ",".join(f"{k} {v}" for k, v in skip_stats.items())
                n_city = len(cities) or 1
                message = f"完成:{len(kw_list)} 个词条×{n_city} 个城市,{brief}" + (f"({detail})" if detail else "")
            elif status == "stopped":
                message = f"用户手动停止:{brief}"
        except LoginRequired as exc:
            status, message = "error", str(exc)
            await asyncio.to_thread(
                _db_add_events, platform,
                [{"message": f"任务{task_id}中断: {message}", "url": plat.home_url}],
                level="error",
            )
        except AntiBot as exc:
            status, message = "error", f"触发平台风控: {exc}(请稍后在浏览器中人工验证后重试)"
            # 风控中断同样落异常面板:任务结束即弹窗提示,面板提供可追溯记录
            await asyncio.to_thread(
                _db_add_events, platform,
                [{"message": f"任务{task_id}风控中断: {message}", "url": plat.home_url}],
                level="error",
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("抓取任务异常")
            status, message = "error", f"{type(exc).__name__}: {exc}"
            await asyncio.to_thread(
                _db_add_events, platform,
                [{"message": f"任务{task_id}异常: {message}", "url": plat.home_url}],
                level="error",
            )
        finally:
            await asyncio.to_thread(
                _db_finish_task, task_id, status, message, dict(counters)
            )
            self._progress[platform] = f"[任务{task_id}] {message or status}"
            self._last_result[platform] = {
                "task_id": task_id, "status": status, "message": message,
            }
            self._tasks.pop(platform, None)
            self._stops.pop(platform, None)
            self._combos.pop(platform, None)
            if self._running_task_ids.get(platform) == task_id:
                self._running_task_ids.pop(platform, None)
            logger.info("任务 %d 结束: %s - %s", task_id, status, message)

    async def _process_one(
        self, plat, draft: JobDraft, snap: dict, progress, task_id: int, bump,
        city: str = "", kw: str = "",
    ) -> str:
        """查重 → 评估 → 入库。返回 'saved' 或 'skipped';bump 记录跳过原因。

        city: 本次检索的城市(多选循环中的当前城市),作为岗位城市的入库兜底。
        kw: 本次检索的词条(搜索来源),入库到 source_keyword 供详情页命中高亮。
        """
        # 适配器解析层警告(字段缺失/平台文本可能变化)持久化到异常面板(带岗位链接)
        warns = draft.raw.pop("_warnings", None) or []
        if warns:
            items = [{"message": str(w), "url": self._openable_url(plat.id, draft)} for w in warns]
            n = await asyncio.to_thread(_db_add_events, plat.id, items)
            if n:
                progress(f"⚠ 记录 {n} 条解析异常(见左下角异常面板,可点击跳转)")
        gear_name, keywords = snap.get("gear", ""), snap.get("keywords", [])
        # 对齐实例:详情页跳回首页/异常跳转视为岗位已下线,不入库
        if draft.raw.get("offline"):
            progress(f"跳过「{draft.title}」: 岗位已下线或链接失效")
            bump("已下线/失效")
            return "skipped"
        # 详情页被平台反自动化清空(blank):无有效数据,不入库并记录异常
        if draft.raw.get("blanked"):
            await asyncio.to_thread(
                _db_add_events, plat.id,
                [{"message": f"详情页被平台反自动化拦截(空白页),本轮跳过: {draft.title}",
                  "url": self._openable_url(plat.id, draft)}],
            )
            progress(f"跳过「{draft.title}」: 详情页被平台拦截(空白页),见异常面板")
            bump("被平台拦截")
            return "skipped"
        up = await asyncio.to_thread(
            _db_update_existing, plat.id, draft.platform_job_id, draft, task_id, kw
        )
        if up.exists:
            # 已存在:仅更新有变化的字段,不重复入库(需求:比对是否已存在)
            if up.changed > 0:
                progress(f"已存在「{draft.title}」: 同步更新{up.changed}个字段")
            else:
                progress(f"跳过「{draft.title}」: 已存在,无字段变化")
            bump("已存在")
            return "skipped"

        result = await evaluate_job(
            draft, gear_name, keywords, extras=snap.get("extras") or {}
        )
        if result.dropped:
            progress(f"跳过「{draft.title}」: {result.drop_reason}")
            bump("活跃度/新鲜度窗口")
            return "skipped"

        await asyncio.to_thread(
            _db_insert_job, plat.id, draft, result, snap.get("keyword", ""), city,
            task_id, snap.get("table_id"), kw,
        )
        note = f"(备注: {result.remark})" if result.remark else ""
        progress(f"入库「{draft.title}」@{draft.company}{note}")
        return "saved"

    @staticmethod
    def _openable_url(platform: str, draft: JobDraft) -> str:
        """构造可直接在浏览器打开的岗位链接(Boss 需携带防重定向参数)。"""
        url = draft.url or ""
        params = {k: v for k, v in (draft.open_params or {}).items() if v}
        if not url:
            return ""
        if params:
            from urllib.parse import urlencode

            url = url + ("&" if "?" in url else "?") + urlencode(params)
        return url


scheduler = Scheduler()