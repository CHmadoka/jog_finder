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
    """新建岗位设置。

    名称缺省 → 自动取「新岗位设置_n」(n 从 1 递增,取首个未占用序号);
    词条可缺省(空列表)。
    """
    name = (body.name or "").strip()
    if name and db.query(Gear).filter(Gear.name == name).first():
        raise HTTPException(409, f"档位已存在: {name}")
    gear = Gear(name=name or "__pending__", keywords=body.keywords or [])
    db.add(gear)
    db.flush()          # 先落行(name 非空唯一),再补自动命名
    if not name:
        taken = {r for (r,) in db.query(Gear.name).all()}
        n = 1
        while f"新岗位设置_{n}" in taken:
            n += 1
        gear.name = f"新岗位设置_{n}"
    db.commit()
    db.refresh(gear)
    return gear


@router.put("/{gear_id}", response_model=GearOut)
def update_gear(gear_id: int, body: GearIn, db: Session = Depends(get_session)):
    gear = db.get(Gear, gear_id)
    if not gear:
        raise HTTPException(404, "档位不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "设置名不能为空")
    dup = db.query(Gear).filter(Gear.name == name, Gear.id != gear_id).first()
    if dup:
        raise HTTPException(409, f"档位名冲突: {name}")
    gear.name = name
    gear.keywords = body.keywords or []
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