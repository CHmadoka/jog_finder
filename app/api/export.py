"""岗位导出 Excel。"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from ..config import config
from ..models import Job, get_session

router = APIRouter(prefix="/export", tags=["export"])

_COLS = [
    ("platform", "平台"), ("title", "职位"), ("company", "公司"),
    ("salary", "薪资"), ("city", "城市"), ("district", "区域"),
    ("experience", "经验"), ("education", "学历"),
    ("hr_name", "HR"), ("hr_active_at", "HR活跃时间"),
    ("published_at", "发布时间"), ("first_seen_at", "入库时间"),
    ("url", "链接"), ("remark", "备注(不符合原因)"),
    ("description", "职位描述"),
]


@router.get("")
def export_jobs(platform: str | None = None, db: Session = Depends(get_session)):
    query = db.query(Job).order_by(Job.first_seen_at.desc())
    if platform:
        query = query.filter(Job.platform == platform)
    rows = query.all()

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "岗位"
    ws.append([c[1] for c in _COLS])
    for j in rows:
        values = []
        for attr, _ in _COLS:
            v = getattr(j, attr)
            if hasattr(v, "strftime"):
                v = v.strftime("%Y-%m-%d %H:%M")
            values.append(v)
        ws.append(values)
    # 列宽
    for idx, (_, label) in enumerate(_COLS, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = 14
    ws.column_dimensions["N"].width = 40  # remark
    ws.column_dimensions["O"].width = 60  # description

    config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{platform}" if platform else "_all"
    path = config.EXPORT_DIR / f"jobs{suffix}_{ts}.xlsx"
    wb.save(path)
    return FileResponse(
        path,
        filename=path.name,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )