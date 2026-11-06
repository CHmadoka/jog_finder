"""平台信息与抓取任务控制。"""
from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import CrawlTask, get_session
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

    body: {platform, filter_id?, gear_id?, keyword?, city?, salary?, table_id?, max_pages?, all_keywords?}
    条件优先级:filter_id > gear_id > keyword
    """
    platform = body.get("platform")
    if platform not in PLATFORMS:
        raise HTTPException(400, f"platform 必须是 {PLATFORMS}")
    try:
        task_id = await scheduler.start(
            platform,
            filter_id=body.get("filter_id"),
            gear_id=body.get("gear_id"),
            keyword=body.get("keyword"),
            city=body.get("city"),
            salary=body.get("salary"),
            table_id=body.get("table_id"),
            max_pages=body.get("max_pages"),
            all_keywords=bool(body.get("all_keywords", True)),
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
    return q.all()