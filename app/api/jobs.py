"""岗位列表/详情查询。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_
from sqlalchemy.orm import Session
from urllib.parse import urlencode

from ..models import Job, get_session
from ..models.schemas import JobPage
from ..utils import active_label

router = APIRouter(prefix="/jobs", tags=["jobs"])


def _fill_labels(page: dict) -> dict:
    """为每个岗位生成活跃状态标签(与存储同一套估算规则)。"""
    for j in page["items"]:
        j.hr_active_label = active_label(j.hr_active_at)
    return page


def _filtered_query(
    db: Session,
    platform: str | None,
    gear_id: int | None,
    fit: str | None,
    city: str | None,
    has_remark: int | None,
    q: str | None,
    hr_online: int | None = None,
):
    """按筛选参数构造查询(fit 除外,供统计接口复用)。"""
    query = db.query(Job)
    if platform:
        query = query.filter(Job.platform == platform)
    if gear_id:
        query = query.filter(Job.gear_id == gear_id)
    if city:
        query = query.filter(Job.city.like(f"%{city}%"))
    if has_remark == 1:
        query = query.filter(Job.remark != "")
    if hr_online == 1:
        query = query.filter(Job.hr_online.is_(True))
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Job.title.like(like), Job.company.like(like)))
    return query


@router.get("", response_model=JobPage)
def list_jobs(
    platform: str | None = None,
    gear_id: int | None = None,
    fit: str | None = None,          # "1"=匹配 "0"=不匹配 "none"=未分析 空=全部
    city: str | None = None,
    has_remark: int | None = None,   # 1=仅看有备注
    hr_online: int | None = None,    # 1=仅看HR当前在线
    q: str | None = None,            # 标题/公司模糊搜索
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    order: str = Query("first_seen", pattern="^(first_seen|published|hr_active)$"),
    db: Session = Depends(get_session),
):
    query = _filtered_query(db, platform, gear_id, fit, city, has_remark, q, hr_online)
    if fit == "1":
        query = query.filter(Job.fit.is_(True))
    elif fit == "0":
        query = query.filter(Job.fit.is_(False))
    elif fit == "none":
        query = query.filter(Job.fit.is_(None))
    total = query.count()
    order_col = {
        "first_seen": Job.first_seen_at.desc(),
        "published": Job.published_at.desc(),
        "hr_active": Job.hr_active_at.desc(),
    }[order]
    items = (
        query.order_by(order_col)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return _fill_labels({"total": total, "items": items})


@router.get("/stats")
def jobs_stats(
    platform: str | None = None,
    gear_id: int | None = None,
    city: str | None = None,
    has_remark: int | None = None,
    q: str | None = None,
    db: Session = Depends(get_session),
):
    """当前筛选口径下的判定统计:总数/契合/不符/未析/HR在线。"""
    base = _filtered_query(db, platform, gear_id, None, city, has_remark, q)
    return {
        "total": base.count(),
        "fit": base.filter(Job.fit.is_(True)).count(),
        "unfit": base.filter(Job.fit.is_(False)).count(),
        "unknown": base.filter(Job.fit.is_(None)).count(),
        "online": base.filter(Job.hr_online.is_(True)).count(),
    }


@router.get("/{job_id}")
def job_detail(job_id: int, db: Session = Depends(get_session)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    out = _fill_labels({"items": [job]})["items"][0]
    return out


@router.post("/{job_id}/open")
async def open_job_in_browser(job_id: int, db: Session = Depends(get_session)):
    """在已登录的抓取浏览器窗口中打开岗位原始页面(保留登录态/真实指纹)。

    Boss 详情直链必须携带 securityId/lid,否则会被平台重定向/触发风控验证页
    (验证页会反复自动刷新)。入库时已把 open_params 存进 extra,此处自动拼接;
    旧数据缺失参数时仍尝试打开,由验证页检测兜底(触发即关闭并报错)。
    """
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    if not job.url:
        raise HTTPException(400, "该岗位没有页面链接")

    url = job.url
    extra_params = {k: v for k, v in (job.extra or {}).items() if v}
    if extra_params:
        url = url + ("&" if "?" in url else "?") + urlencode(extra_params)

    # 站内 referer 降低风控触发概率;旧数据(无 securityId)若仍落到验证页,
    # 由前端引导用户完成滑块验证(通过后平台自动跳回岗位,且短期内放行其他岗位)
    referrer = "https://www.zhipin.com/" if job.platform == "boss" else None

    from ..browser import browser_manager

    try:
        res = await browser_manager.open_url_native(url, referrer=referrer)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"在浏览器中打开失败: {exc}") from exc
    if res.get("need_verify"):
        return {
            "ok": True,
            "need_verify": True,
            "url": res["url"],
            "msg": "已打开安全验证页:请在浏览器中完成滑块验证,通过后会自动跳回岗位详情。"
                   "同一浏览器验证一次后,短期内其他岗位均可直接打开;"
                   "重新执行一次抓取也可让岗位自动带上防风控参数",
        }
    return {"ok": True, "msg": "已在已登录浏览器中打开", "url": res["url"]}


@router.delete("/{job_id}")
def delete_job(job_id: int, db: Session = Depends(get_session)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "岗位不存在")
    db.delete(job)
    db.commit()
    return {"ok": True}


@router.post("/clear_all")
def clear_all_jobs(db: Session = Depends(get_session)):
    """清空岗位库(保留档位与条件配置)。"""
    from ..scheduler import scheduler

    n = db.query(Job).count()
    db.query(Job).delete()
    db.commit()
    return {"ok": True, "deleted": n, "hint": "运行中的任务不受影响,但已抓数据不可恢复"}