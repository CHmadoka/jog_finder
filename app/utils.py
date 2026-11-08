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

# ---------- 运行时延迟覆盖 ----------
# 各平台任务可携带自定义随机间隔范围(前端「随机间隔」设置框,默认 3~8 秒防风控)。
# key = 平台 id;调度器保证每平台同时仅一个任务,覆盖互不干扰,任务结束即清除。
_RUNTIME_DELAYS: dict[str, tuple[float, float]] = {}


def normalize_delay(lo, hi) -> tuple[float, float]:
    """规整延迟范围(秒):非法值回退 config 默认,保证 hi >= lo > 0。"""
    try:
        lo, hi = float(lo), float(hi)
    except (TypeError, ValueError):
        return config.DELAY_MIN, config.DELAY_MAX
    if lo <= 0 or hi <= 0:
        return config.DELAY_MIN, config.DELAY_MAX
    if lo > hi:
        lo, hi = hi, lo
    return lo, hi


def set_runtime_delay(platform: str, lo, hi) -> tuple[float, float]:
    """注册某平台本次任务的随机间隔范围,返回规整后的实际生效值。"""
    pair = normalize_delay(lo, hi)
    _RUNTIME_DELAYS[platform] = pair
    return pair


def clear_runtime_delay(platform: str) -> None:
    """任务结束时清除该平台的运行时延迟覆盖(回退 config 默认)。"""
    _RUNTIME_DELAYS.pop(platform, None)


# ---------- 人工验证等待标志 ----------
# 平台进入"等待人工完成验证"循环时置 True,退出(通过/停止/中止)置 False。
# 调度器 /tasks/status 透出 verify_wait,前端据此弹强提醒(上升沿一次)。
_VERIFY_WAIT: dict[str, bool] = {}


def set_verify_wait(platform: str, waiting: bool) -> None:
    _VERIFY_WAIT[platform] = waiting


def is_verify_wait(platform: str) -> bool:
    return _VERIFY_WAIT.get(platform, False)


async def human_delay(platform: str | None = None) -> None:
    """页面动作间的随机延迟,模拟人工节奏(任务级覆盖 > config 默认)。"""
    lo, hi = _RUNTIME_DELAYS.get(platform) or (config.DELAY_MIN, config.DELAY_MAX)
    await asyncio.sleep(random.uniform(lo, max(lo, hi)))


async def detail_delay(platform: str | None = None) -> None:
    """详情页访问间的随机延迟,模拟人工浏览(任务级覆盖 > config 默认)。"""
    lo, hi = _RUNTIME_DELAYS.get(platform) or (config.DETAIL_DELAY_MIN, config.DETAIL_DELAY_MAX)
    await asyncio.sleep(random.uniform(lo, max(lo, hi)))


# ---------- 意外等待的静默重试 ----------
# 节奏约定(全平台统一):意外等待(风控未识别/中断/无响应)期间**不反复刷新页面**,
# 每超过 5 秒即附加 0~5 秒随机静默后再尝试一次;连续 3 次失败自动中止任务。
RETRY_UNIT = 5.0
RETRY_ATTEMPTS = 3


async def silent_backoff(unit: float = RETRY_UNIT, should_stop=None) -> None:
    """静默退避等待:unit 秒观察 + rand(0, unit) 秒随机静默(全程不做任何导航/刷新)。

    should_stop: 可选停止检查,等待期间每秒检查一次,及时响应停止指令。
    """
    await interruptible_sleep(unit + random.uniform(0, unit), should_stop)


async def interruptible_sleep(seconds: float, should_stop=None) -> None:
    """可中断分段睡眠:每约 1 秒检查一次停止标志,长等待也能及时响应停止指令。"""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + seconds
    while True:
        remain = deadline - loop.time()
        if remain <= 0:
            return
        if should_stop is not None and should_stop():
            return
        await asyncio.sleep(min(1.0, remain))


# ---------- 跨平台全局导航闸 ----------
# 三个平台共用同一浏览器(同一出口 IP):各平台自身已有 3~8s 随机间隔,但多平台
# 并发抓取时叠加请求率仍可能过高。全局闸保证任意两次"主动请求"(goto/接口
# fetch)之间至少间隔 1.5~3 秒(随机),与各平台自身间隔相乘生效。
_NAV_GATE_LOCK: asyncio.Lock | None = None
_NAV_GATE_LAST = 0.0
_NAV_GATE_GAP = (1.5, 3.0)


def _nav_gate_lock() -> asyncio.Lock:
    global _NAV_GATE_LOCK
    if _NAV_GATE_LOCK is None:      # 惰性创建:绑定到当前事件循环
        _NAV_GATE_LOCK = asyncio.Lock()
    return _NAV_GATE_LOCK


async def global_nav_gate() -> None:
    """跨平台导航/请求全局闸:发起 goto/接口 fetch 前调用。"""
    global _NAV_GATE_LAST
    async with _nav_gate_lock():
        loop = asyncio.get_event_loop()
        gap = random.uniform(*_NAV_GATE_GAP)
        wait = _NAV_GATE_LAST + gap - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        _NAV_GATE_LAST = loop.time()


class SessionPacer:
    """会话节奏器:在常规随机间隔之外,每处理一批(页/岗位)插入一次长休息。

    恒定短节奏(即使带随机)持续数十次仍会被平台统计识别——真人会中途离开/
    阅读。批次大小与休息时长均随机;批次未耗尽时 tick 直接返回不休息。
    计数跨任务累计(每次任务开始不重置):只会更保守,无副作用。
    """

    def __init__(self, *, batch: tuple[int, int] = (2, 4), idle: tuple[float, float] = (15, 40),
                 label: str = "翻页") -> None:
        self.batch = batch
        self.idle = idle
        self.label = label
        self._left = random.randint(*batch)

    async def tick(self, platform: str, progress=None, should_stop=None) -> None:
        """每完成一页/一个岗位调用一次;批次耗尽时插入一次长休息(可被停止打断)。"""
        self._left -= 1
        if self._left > 0:
            return
        self._left = random.randint(*self.batch)
        wait = random.uniform(*self.idle)
        if progress:
            progress(f"防风控会话休息 {wait:.0f} 秒({self.label}批次完成,模拟真人离开后返回)")
        await interruptible_sleep(wait, should_stop)


async def warmup(page, platform: str) -> None:
    """任务启动预热:载体页静默停留 + 轻滚动,模拟真人进入后先浏览再操作,
    避免任务启动瞬间即高频发请求(猎聘/前程无忧短时触发验证的常见诱因)。"""
    await human_delay(platform)
    try:
        await human_scroll(page, 2)
    except Exception:  # noqa: BLE001  (载体页异常不阻断任务)
        pass


async def patient_poll(
    check, *, rounds: int = RETRY_ATTEMPTS, unit: float = RETRY_UNIT, should_stop=None
) -> bool:
    """静默观察式等待(不刷新页面):每轮静默等待 5~10 秒后执行一次 check()。

    check() 返回真值 → 立即成功返回 True;check() 抛出的异常原样向上传播
    (如风控判定需立即中止任务)。rounds 轮全失败 → 返回 False,由调用方决定中止或跳过。
    """
    for _ in range(rounds):
        if should_stop is not None and should_stop():
            return False
        await silent_backoff(unit, should_stop)
        if await check():
            return True
    return False


async def patient_call(
    op, *, attempts: int = RETRY_ATTEMPTS, unit: float = RETRY_UNIT
) -> tuple[bool, object]:
    """静默重试(不刷新页面):单次操作失败后静默等待 5~10 秒再试同一操作。

    返回 (True, 结果) 或 (False, 最后一次异常)。op 的业务性"拒绝"
    (如接口返回拒绝码)应作为正常返回值交给调用方判定,不走重试(避免连发)。
    """
    last: object = None
    for i in range(attempts):
        try:
            return True, await op()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < attempts - 1:
                await silent_backoff(unit)
    return False, last


async def page_responsive(page) -> bool:
    """页面是否仍可正常响应(标题非空且脚本可执行)。

    用于区分"平台正常响应但岗位失效"(重定向到列表/首页,可跳过)与
    "页面无响应/被反自动化清空"(风控未识别或中断,应中止任务)。
    """
    try:
        if not ((await page.title()) or "").strip():
            return False
        await page.evaluate("() => 1")
        return True
    except Exception:  # noqa: BLE001
        return False


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
    if re.fullmatch(r"(当前|正在)?在线", t):          # 猎聘"当前在线"=刚刚
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
    m = re.search(r"(\d+)\s*周前", t)
    if m:
        return fmt_dt(now - timedelta(weeks=int(m.group(1)))) or ""
    m = re.search(r"(\d+)\s*个?月前", t)
    if m:
        return fmt_dt(now - timedelta(days=30 * int(m.group(1)))) or ""
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