"""API 路由汇总。"""
from __future__ import annotations

from fastapi import APIRouter

from . import export, events, filters, gears, jobs, tasks

api_router = APIRouter(prefix="/api")
api_router.include_router(gears.router)
api_router.include_router(filters.router)
api_router.include_router(tasks.router)
api_router.include_router(jobs.router)
api_router.include_router(events.router)
api_router.include_router(export.router)

__all__ = ["api_router"]