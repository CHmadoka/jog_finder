"""搜索条件 CRUD。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import Gear, SearchFilter, get_session
from ..models.schemas import PLATFORMS, SearchFilterIn, SearchFilterOut

router = APIRouter(prefix="/filters", tags=["filters"])


@router.get("", response_model=list[SearchFilterOut])
def list_filters(platform: str | None = None, db: Session = Depends(get_session)):
    q = db.query(SearchFilter).order_by(SearchFilter.id)
    if platform:
        q = q.filter(SearchFilter.platform == platform)
    return q.all()


@router.post("", response_model=SearchFilterOut)
def create_filter(body: SearchFilterIn, db: Session = Depends(get_session)):
    if body.platform not in PLATFORMS:
        raise HTTPException(400, f"platform 必须是 {PLATFORMS}")
    if body.gear_id and not db.get(Gear, body.gear_id):
        raise HTTPException(400, "关联档位不存在")
    flt = SearchFilter(**body.model_dump())
    db.add(flt)
    db.commit()
    db.refresh(flt)
    return flt


@router.put("/{filter_id}", response_model=SearchFilterOut)
def update_filter(filter_id: int, body: SearchFilterIn, db: Session = Depends(get_session)):
    flt = db.get(SearchFilter, filter_id)
    if not flt:
        raise HTTPException(404, "条件不存在")
    if body.platform not in PLATFORMS:
        raise HTTPException(400, f"platform 必须是 {PLATFORMS}")
    for k, v in body.model_dump().items():
        setattr(flt, k, v)
    db.commit()
    db.refresh(flt)
    return flt


@router.delete("/{filter_id}")
def delete_filter(filter_id: int, db: Session = Depends(get_session)):
    flt = db.get(SearchFilter, filter_id)
    if not flt:
        raise HTTPException(404, "条件不存在")
    db.delete(flt)
    db.commit()
    return {"ok": True}