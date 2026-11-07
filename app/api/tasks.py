"""平台信息与抓取任务控制。"""
from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import CrawlTask, Job, get_session
from ..models.schemas import PLATFORMS, TaskOut
from ..platforms import all_platforms, get_platform
from ..platforms.base import AntiBot
from ..scheduler import AlreadyRunning, scheduler

router = APIRouter(tags=["tasks"])


@router.get("/platforms")
def list_platforms(db: Session = Depends(get_session)):
    """平台列表 + 是否有任务在运行。"""
    running = {p: scheduler.is_running(p) for p in PLATFORMS}
    return [
        {**p.describe(), "running": running.get(p.id, False)} for p in all_platforms()
    ]


@router.post("/tasks/start", response_model=TaskOut)
async def start_task(body: dict, db: Session = Depends(get_session)):
    """启动抓取任务。

    body: {platform, filter_id?, gear_id?, cities?, city?, salary?, table_id?, max_pages?}
    条件优先级:filter_id > gear_id;城市 cities(多选列表)优先于旧单值 city。
    """
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise HTTPException(400, f"platform 必须是 {PLATFORMS}")
    cities = body.get("cities")
    if cities is None and body.get("city"):
        cities = [body["city"]]   # 兼容旧单值参数
    try:
        task_id = await scheduler.start(
            platform,
            filter_id=body.get("filter_id"),
            gear_id=body.get("gear_id"),
            cities=cities,
            salary=body.get("salary"),
            table_id=body.get("table_id"),
            max_pages=body.get("max_pages"),
        )
    except AlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    row = db.get(CrawlTask, task_id)
    return row


@router.post("/tasks/check_login")
async def check_login(body: dict):
    """抓取前登录预检查:探测目标平台登录态。

    未登录时前端直接拦截"开始抓取"并提示登录(任务级检查仍保留为兜底)。
    """
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise HTTPException(400, f"platform 必须是 {PLATFORMS}")
    if scheduler.is_running(platform):
        raise HTTPException(409, "该平台任务运行中,无需检查登录")
    from ..browser import browser_manager

    plat = get_platform(platform)
    try:
        await browser_manager.ensure()
        page = await browser_manager.get_or_open_page(
            plat.home_prefix, plat.home_url, avoid_marker=plat.detail_url_marker
        )
        logged = await plat.is_logged_in(page)
    except AntiBot as exc:
        raise HTTPException(503, f"页面未能加载,无法检查登录: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"登录检测失败: {exc}") from exc
    return {"platform": platform, "name": plat.name, "logged_in": bool(logged)}


@router.post("/tasks/{platform}/stop")
def stop_task(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(400, f"platform 必须是 {PLATFORMS}")
    if scheduler.request_stop(platform):
        return {"ok": True, "msg": "已发送停止指令"}
    return {"ok": False, "msg": "该平台当前没有运行中的任务"}


@router.post("/tasks/refresh_active")
async def refresh_active(body: dict):
    """旧数据修复:对活跃字段为空的岗位重新解析详情页并回写(与抓取共用平台互斥)。"""
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise HTTPException(400, f"platform 必须是 {PLATFORMS}")
    try:
        await scheduler.start_refresh(platform)
    except AlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"ok": True, "msg": "补全任务已启动,进度见左栏状态区"}


@router.get("/tasks/status")
def task_status():
    """各平台实时运行状态(内存进度)。"""
    return scheduler.status()


@router.get("/tasks", response_model=list[TaskOut])
def task_history(platform: str | None = None, limit: int = 20, db: Session = Depends(get_session)):
    q = db.query(CrawlTask).order_by(CrawlTask.id.desc()).limit(limit)
    if platform:
        q = db.query(CrawlTask).filter(CrawlTask.platform == platform).order_by(CrawlTask.id.desc()).limit(limit)
    rows = q.all()
    # 附加每个任务的关联岗位数(前端:删除确认与"空任务免确认"判断)
    if rows:
        from sqlalchemy import func

        counts = dict(
            db.query(Job.crawl_task_id, func.count(Job.id))
            .filter(Job.crawl_task_id.in_([t.id for t in rows]))
            .group_by(Job.crawl_task_id)
            .all()
        )
        for t in rows:
            t.job_count = counts.get(t.id, 0)
    return rows


@router.delete("/tasks/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_session)):
    """删除抓取任务及其关联的全部岗位数据(crawl_task_id 指向该任务的所有岗位)。

    运行中的任务不可删除(先停止)。删除岗位数随响应返回,供前端提示。
    """
    row = db.get(CrawlTask, task_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    if scheduler.running_task_id(row.platform) == task_id:
        raise HTTPException(409, "任务正在运行,请先停止抓取后再删除")
    n = (
        db.query(Job)
        .filter(Job.crawl_task_id == task_id)
        .delete(synchronize_session=False)
    )
    db.delete(row)
    db.commit()
    return {"ok": True, "deleted_jobs": n}