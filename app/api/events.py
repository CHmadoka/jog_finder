"""运行期异常事件:抓取过程中字段解析失败等警告,手动清除前持久存在。"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..models import AppEvent, get_session

router = APIRouter(prefix="/events", tags=["events"])


@router.get("")
def list_events(limit: int = 50, db: Session = Depends(get_session)):
    """最近的异常事件(新→旧)。"""
    rows = (
        db.query(AppEvent)
        .order_by(AppEvent.id.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )
    return rows


@router.delete("")
def clear_events(db: Session = Depends(get_session)):
    """手动清除全部异常记录(用户明确触发,否则持久保留)。"""
    n = db.query(AppEvent).count()
    db.query(AppEvent).delete()
    db.commit()
    return {"ok": True, "deleted": n}