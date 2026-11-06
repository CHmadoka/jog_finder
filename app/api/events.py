"""运行期异常事件:抓取过程中字段解析失败等警告,手动清除前持久存在。

每条异常可携带 url,前端点击后在已登录的抓取浏览器中打开该页面。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
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


@router.post("/{event_id}/open")
async def open_event(event_id: int, db: Session = Depends(get_session)):
    """在已登录的抓取浏览器中打开异常相关页面(排查字段问题用)。"""
    ev = db.get(AppEvent, event_id)
    if not ev:
        raise HTTPException(404, "异常记录不存在")
    if not ev.url:
        raise HTTPException(400, "该异常没有可跳转的链接")
    referrer = "https://www.zhipin.com/" if ev.platform == "boss" else None
    from ..browser import browser_manager
    from ..scheduler import scheduler

    try:
        res = await browser_manager.open_url_native(ev.url, referrer=referrer)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, f"在浏览器中打开失败: {exc}") from exc
    res = await browser_manager.diagnose_open(
        ev.platform, res, platform_busy=scheduler.is_running(ev.platform) if ev.platform else False
    )
    if res.get("need_login"):
        return {"ok": False, "need_login": True, "msg": res["msg"]}
    if res.get("need_verify"):
        return {
            "ok": True,
            "need_verify": True,
            "url": res["url"],
            "msg": "已打开安全验证页:请在浏览器中完成滑块验证后重试。",
        }
    return {"ok": True, "msg": "已在已登录浏览器中打开", "url": res["url"]}