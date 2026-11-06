"""通用工具:人工节奏延迟、相对时间解析、HTML 快照。"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta
from pathlib import Path

from .config import config

logger = logging.getLogger(__name__)


async def human_delay() -> None:
    """页面动作间的随机延迟,模拟人工节奏。"""
    lo, hi = config.DELAY_MIN, config.DELAY_MAX
    await asyncio.sleep(random.uniform(lo, max(lo, hi)))


async def human_type(page, selector: str, text: str) -> None:
    """模拟真人输入:逐字延迟。"""
    await page.click(selector)
    await page.keyboard.press("Control+A")
    await page.keyboard.type(text, delay=random.randint(60, 140))


async def human_scroll(page, times: int = 3) -> None:
    """随机滚动,触发懒加载并模拟真人浏览。"""
    for _ in range(times):
        await page.mouse.wheel(0, random.randint(300, 900))
        await asyncio.sleep(random.uniform(0.4, 1.2))


def save_snapshot(platform: str, name: str, html: str) -> Path:
    """保存页面 HTML 快照,便于选择器失效后排查。"""
    config.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = config.SNAPSHOT_DIR / f"{platform}_{name}_{ts}.html"
    path.write_text(html, encoding="utf-8")
    logger.warning("已保存页面快照: %s", path)
    return path


_CN_NUM = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_relative_time(text: str, now: datetime | None = None) -> datetime | None:
    """解析"3分钟前/刚刚/2小时前/昨天/09-28/2023-09-28 10:20"等中文相对时间。"""
    if not text:
        return None
    now = now or datetime.now()
    t = text.strip()
    t = t.replace("发布于", "").replace("更新于", "").strip()

    if re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{2}:\d{2}(:\d{2})?)?", t):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(t, fmt)
            except ValueError:
                continue
    m = re.search(r"(\d+|[一两二三四五六七八九十]+)\s*分钟前", t)
    if m:
        n = _CN_NUM.get(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else 1)
        return now - timedelta(minutes=n)
    m = re.search(r"(\d+|[一两二三四五六七八九十]+)\s*小时前", t)
    if m:
        n = _CN_NUM.get(m.group(1)) or (int(m.group(1)) if m.group(1).isdigit() else 1)
        return now - timedelta(hours=n)
    if re.search(r"今天|刚刚|刚刚活跃|刚刚更新", t):
        return now
    if re.search(r"昨天", t):
        return now - timedelta(days=1)
    m = re.search(r"(\d{1,2})-(\d{1,2})", t)  # "09-28" 视为今年
    if m:
        try:
            dt = datetime(now.year, int(m.group(1)), int(m.group(2)))
            return dt if dt <= now else dt.replace(year=now.year - 1)
        except ValueError:
            return None
    m = re.search(r"(\d+)\s*天前", t)
    if m:
        return now - timedelta(days=int(m.group(1)))
    m = re.search(r"(\d+)\s*周前", t)
    if m:
        return now - timedelta(weeks=int(m.group(1)))
    m = re.search(r"(\d+)\s*月前", t)
    if m:
        return now - timedelta(days=30 * int(m.group(1)))
    return None


_DT_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
)


def parse_dt_loose(value) -> datetime | None:
    """宽松解析时间:兼容 datetime 对象、估算字符串("…前/后/活跃"后缀)与旧库 ISO 微秒格式。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    for suffix in ("活跃", "后", "前"):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            break
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):  # 日期型("今日/昨日活跃"):视为当天 00:00
        s += " 00:00"
    for fmt in _DT_FORMATS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def fmt_dt(dt: datetime | None) -> str | None:
    """入库统一格式:'YYYY-MM-DD HH:MM'(可按字典序排序)。"""
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def active_estimate_from_text(text: str, now: datetime | None = None) -> str:
    """把平台活跃文案换算为存储字符串 —— 全项目统一的估算规则。

    基准时刻 = 抓取/入库时刻(活跃度判定发生在入库前,两者同刻)。
    存储格式(后缀直接携带方向):
    - "YYYY-MM-DD HH:MM后" : 真实活跃不早于该时刻(文案只给下界,如 本周/本月/N日内活跃),
                             est = 入库时间 - 该时长(取区间最坏端);
    - "YYYY-MM-DD HH:MM前" : 真实活跃不晚于该时刻(如 三个月前/长期未活跃);
    - "YYYY-MM-DD HH:MM"   : 约等于该时刻(如 N分钟/N小时/N天前活跃,误差数小时内);
    - "YYYY-MM-DD 活跃"    : 今日/昨日活跃,只知道日期,不伪造时刻。
    """
    now = now or datetime.now()
    t = (text or "").strip()
    if not t:
        return ""

    if t.startswith(("刚刚", "在线")):
        return fmt_dt(now) or ""
    if t.startswith("今日"):
        return now.strftime("%Y-%m-%d") + " 活跃"
    if t.startswith("昨日"):
        return (now - timedelta(days=1)).strftime("%Y-%m-%d") + " 活跃"
    m = re.search(r"(\d+)\s*分钟前", t)
    if m:
        return fmt_dt(now - timedelta(minutes=int(m.group(1)))) or ""
    m = re.search(r"(\d+)\s*小时前", t)
    if m:
        return fmt_dt(now - timedelta(hours=int(m.group(1)))) or ""
    m = re.search(r"(\d+)\s*天前", t)
    if m:
        return fmt_dt(now - timedelta(days=int(m.group(1)))) or ""
    if "本周" in t or "近一周" in t:
        return fmt_dt(now - timedelta(days=7)) + "后"
    if "近两周" in t:
        return fmt_dt(now - timedelta(days=14)) + "后"
    if "本月" in t:
        return fmt_dt(now - timedelta(days=30)) + "后"
    if "上月" in t:
        return fmt_dt(now - timedelta(days=60)) + "后"
    m = re.search(r"(\d+)\s*日内活跃", t)
    if m:
        return fmt_dt(now - timedelta(days=int(m.group(1)))) + "后"
    m = re.search(r"(\d+)\s*周内活跃", t)
    if m:
        return fmt_dt(now - timedelta(weeks=int(m.group(1)))) + "后"
    m = re.search(r"(\d+)\s*月内活跃", t)
    if m:
        return fmt_dt(now - timedelta(days=30 * int(m.group(1)))) + "后"
    if "三个月" in t:
        return fmt_dt(now - timedelta(days=95)) + "前"
    if "半年" in t:
        return fmt_dt(now - timedelta(days=185)) + "前"
    if "长期未活跃" in t:
        return fmt_dt(now - timedelta(days=200)) + "前"
    return ""


def active_label(est, now: datetime | None = None) -> str:
    """根据存储的估算字符串生成活跃状态标签(与存储同一套规则,网页直接展示)。"""
    s = (str(est) if est is not None else "").strip()
    dt = parse_dt_loose(s)
    if not dt:
        return ""
    now = now or datetime.now()
    days = (now.date() - dt.date()).days
    if s.endswith("后"):                     # 真实活跃 ≥ est → "N天内活跃"
        if days <= 0:
            return "今日活跃"
        if days == 1:
            return "昨日活跃"
        return f"{days}天内活跃"
    if s.endswith("前"):                     # 真实活跃 ≤ est → "N天前活跃"
        return f"{max(days, 1)}天前活跃"
    if s.endswith("活跃"):                   # 日期型 → 今日/昨日
        if days <= 0:
            return "今日活跃"
        if days == 1:
            return "昨日活跃"
        return f"{days}天前活跃"
    hours = (now - dt).total_seconds() / 3600
    if hours < 1:
        return "刚刚活跃"
    if hours < 24:
        return f"{int(hours)}小时前活跃"
    if days == 1:
        return "昨日活跃"
    return f"{days}天前活跃"