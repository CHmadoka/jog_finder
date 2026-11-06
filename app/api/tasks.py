"""平台信息与抓取任务控制。"""
from __future__ import annotations

import asyncio
import threading

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import CrawlTask, get_session
from ..models.schemas import PLATFORMS, TaskOut
from ..platforms import all_platforms
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

    body: {platform, filter_id?, gear_id?, keyword?, city?, max_pages?, all_keywords?}
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
            max_pages=body.get("max_pages"),
            all_keywords=bool(body.get("all_keywords", True)),
        )
    except AlreadyRunning as exc:
        raise HTTPException(409, str(exc)) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(400, str(exc)) from exc
    row = db.get(CrawlTask, task_id)
    return row


@router.post("/tasks/{platform}/stop")
def stop_task(platform: str):
    if platform not in PLATFORMS:
        raise HTTPException(400, f"platform 必须是 {PLATFORMS}")
    if scheduler.request_stop(platform):
        return {"ok": True, "msg": "已发送停止指令"}
    return {"ok": False, "msg": "该平台当前没有运行中的任务"}


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