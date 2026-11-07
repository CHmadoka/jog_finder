"""活跃度/新鲜度窗口过滤。"""
from __future__ import annotations

from datetime import datetime, timedelta

from ..config import config
from ..utils import parse_dt_loose


def within_active_window(
    hr_active_at,
    published_at: datetime | None,
    hr_days: int | None = None,
    pub_days: int | None = None,
) -> tuple[bool, str]:
    """HR 最近活跃 / 岗位最近发布 是否落在阈值窗口内。

    hr_active_at 为存储的估算字符串,后缀携带方向:
    - "…后"  : 真实活跃不早于该时刻(文案如"本周活跃"),est ≥ 窗口下界即可通过;
    - "…前"  : 真实活跃不晚于该时刻(文案如"长期未活跃"),保守直接判定超窗;
    - "…活跃": 今日/昨日活跃,按该日 00:00 与窗口比较;
    - 无后缀 : 约精确时刻(文案如"N天前活跃"),按普通时刻比较。

    - 两个字段都缺失时不误杀(保留入库,待人工判断);
    - hr_active_at 优先判断,其次 published_at;
    - 阈值可显式传入(测试用),默认取 .env 配置。
    """
    now = datetime.now()
    hr_days = config.HR_ACTIVE_DAYS if hr_days is None else hr_days
    pub_days = config.JOB_PUBLISH_DAYS if pub_days is None else pub_days

    if hr_active_at:
        s = str(hr_active_at).strip()
        est = parse_dt_loose(s)
        if est is not None:
            lower = now - timedelta(days=hr_days)
            if s.endswith("后"):
                # 分钟级截断最多丢失 59 秒,加 1 分钟容忍避免"本周活跃"在窗口边界被误杀
                if est + timedelta(minutes=1) >= lower:
                    return True, ""
                return False, f"HR活跃不早于{est:%Y-%m-%d},超出{hr_days}天窗口"
            if s.endswith("前"):
                return False, f"HR活跃不晚于{est:%Y-%m-%d},已超出{hr_days}天窗口"
            if est >= lower:
                return True, ""
            return False, f"HR最近活跃已超过{hr_days}天({est:%Y-%m-%d})"
    if published_at:
        if published_at.replace(tzinfo=None) >= now - timedelta(days=pub_days):
            return True, ""
        return False, f"岗位发布已超过{pub_days}天({published_at:%Y-%m-%d})"
    return True, "无活跃/发布时间信息,保留待人工判断"