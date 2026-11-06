"""数据表(岗位分组容器)CRUD。默认表(id=1)不可删除。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from ..models import DEFAULT_TABLE, Job, JobTable, get_session
from ..models.schemas import TableIn

router = APIRouter(prefix="/tables", tags=["tables"])


@router.get("")
def list_tables(db: Session = Depends(get_session)):
    """全部数据表(含岗位计数;历史无归属岗位计入默认表)。"""
    rows = db.query(JobTable).order_by(JobTable.id).all()
    counts: dict = dict(
        db.query(Job.table_id, func.count(Job.id)).group_by(Job.table_id).all()
    )
    null_count = (
        db.query(Job).filter(or_(Job.table_id.is_(None))).count()
    )
    out = []
    for t in rows:
        n = int(counts.get(t.id, 0) or 0)
        if t.id == DEFAULT_TABLE[0]:
            n += null_count
        out.append({
            "id": t.id,
            "name": t.name,
            "slug": t.slug,
            "created_at": t.created_at,
            "job_count": n,
        })
    return out


@router.post("")
def create_table(body: TableIn, db: Session = Depends(get_session)):
    """新建数据表:表名可缺省,自动命名为 数据表_{id}(可稍后重命名)。"""
    name = (body.name or "").strip()
    if name and db.query(JobTable).filter(JobTable.name == name).first():
        raise HTTPException(409, f"表名已存在: {name}")
    slug = (body.slug or "").strip()
    if slug and db.query(JobTable).filter(JobTable.slug == slug).first():
        raise HTTPException(409, f"标识符已存在: {slug}")
    row = JobTable(name=name or "__pending__", slug=slug or "__pending__")
    db.add(row)
    db.flush()          # 取自增 id(未提交)
    if not name:
        row.name = f"数据表_{row.id}"
    if not slug:
        row.slug = f"tbl_{row.id}"
    db.commit()
    db.refresh(row)
    return row


@router.put("/{table_id}")
def rename_table(table_id: int, body: TableIn, db: Session = Depends(get_session)):
    row = db.get(JobTable, table_id)
    if not row:
        raise HTTPException(404, "数据表不存在")
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "表名不能为空")
    dup = db.query(JobTable).filter(JobTable.name == name, JobTable.id != table_id).first()
    if dup:
        raise HTTPException(409, f"表名已存在: {name}")
    row.name = name
    slug = (body.slug or "").strip()
    if slug and slug != row.slug:
        if db.query(JobTable).filter(JobTable.slug == slug, JobTable.id != table_id).first():
            raise HTTPException(409, f"标识符已存在: {slug}")
        row.slug = slug
    db.commit()
    db.refresh(row)
    return row


@router.post("/{table_id}/clear")
def clear_table(table_id: int, db: Session = Depends(get_session)):
    """清空数据表内全部岗位(表本身保留;默认表也可清空)。"""
    row = db.get(JobTable, table_id)
    if not row:
        raise HTTPException(404, "数据表不存在")
    q = db.query(Job)
    if table_id == DEFAULT_TABLE[0]:
        q = q.filter(or_(Job.table_id == 1, Job.table_id.is_(None)))
    else:
        q = q.filter(Job.table_id == table_id)
    n = q.count()
    q.delete(synchronize_session=False)
    db.commit()
    return {"ok": True, "deleted": n}


@router.delete("/{table_id}")
def delete_table(table_id: int, db: Session = Depends(get_session)):
    """删除数据表;其下岗位自动归入默认表。默认表不可删除。"""
    if table_id == DEFAULT_TABLE[0]:
        raise HTTPException(403, "默认表不可删除")
    row = db.get(JobTable, table_id)
    if not row:
        raise HTTPException(404, "数据表不存在")
    db.query(Job).filter(Job.table_id == table_id).update(
        {Job.table_id: DEFAULT_TABLE[0]}, synchronize_session=False
    )
    db.delete(row)
    db.commit()
    return {"ok": True}