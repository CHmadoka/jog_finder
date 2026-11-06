"""岗位档位 CRUD。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..models import Gear, get_session
from ..models.schemas import GearIn, GearOut

router = APIRouter(prefix="/gears", tags=["gears"])


@router.get("", response_model=list[GearOut])
def list_gears(db: Session = Depends(get_session)):
    return db.query(Gear).order_by(Gear.id).all()


@router.post("", response_model=GearOut)
def create_gear(body: GearIn, db: Session = Depends(get_session)):
    if db.query(Gear).filter(Gear.name == body.name).first():
        raise HTTPException(409, f"档位已存在: {body.name}")
    gear = Gear(name=body.name, keywords=body.keywords)
    db.add(gear)
    db.commit()
    db.refresh(gear)
    return gear


@router.put("/{gear_id}", response_model=GearOut)
def update_gear(gear_id: int, body: GearIn, db: Session = Depends(get_session)):
    gear = db.get(Gear, gear_id)
    if not gear:
        raise HTTPException(404, "档位不存在")
    dup = db.query(Gear).filter(Gear.name == body.name, Gear.id != gear_id).first()
    if dup:
        raise HTTPException(409, f"档位名冲突: {body.name}")
    gear.name = body.name
    gear.keywords = body.keywords
    db.commit()
    db.refresh(gear)
    return gear


@router.delete("/{gear_id}")
def delete_gear(gear_id: int, db: Session = Depends(get_session)):
    gear = db.get(Gear, gear_id)
    if not gear:
        raise HTTPException(404, "档位不存在")
    db.delete(gear)
    db.commit()
    return {"ok": True}