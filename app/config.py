"""全局配置:从 .env 读取,提供默认值。"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, "") or default)
    except ValueError:
        return default


class Config:
    # 浏览器
    BROWSER_PATH: str = os.getenv("BROWSER_PATH", "")
    CDP_PORT: int = _env_int("CDP_PORT", 9222)
    BROWSER_PROFILE_DIR: str = os.getenv(
        "BROWSER_PROFILE_DIR", str(BASE_DIR / "data" / "browser_profile")
    )
    # 专用抓取浏览器是否禁用系统代理(国内招聘站直连更稳;代理出口 IP 易触发风控)
    BROWSER_NO_PROXY: bool = os.getenv("BROWSER_NO_PROXY", "1") != "0"

    # 抓取行为
    # 随机间隔默认 3~8 秒防风控;前端「随机间隔」设置框按任务覆盖(见 utils.set_runtime_delay)
    DELAY_MIN: float = float(os.getenv("DELAY_MIN", "3") or 3)
    DELAY_MAX: float = float(os.getenv("DELAY_MAX", "8") or 8)
    DETAIL_DELAY_MIN: float = float(os.getenv("DETAIL_DELAY_MIN", "3") or 3)  # 详情页间隔下限(秒,兜底)
    DETAIL_DELAY_MAX: float = float(os.getenv("DETAIL_DELAY_MAX", "8") or 8)  # 详情页间隔上限(秒,兜底)
    MAX_PAGES_PER_TASK: int = _env_int("MAX_PAGES_PER_TASK", 10)
    HR_ACTIVE_DAYS: int = _env_int("HR_ACTIVE_DAYS", 7)
    JOB_PUBLISH_DAYS: int = _env_int("JOB_PUBLISH_DAYS", 7)
    # 单会话(任务)安全预算:单登录态的列表页/详情页请求上限,达限任务优雅收尾。
    # 防止"账号基线偏离"——真人日浏览量有限,短时间数百上千次请求无论间隔多
    # 随机都显著偏离历史分位。触发过风控的平台由调度器按次数折减(见 runner)。
    SESSION_PAGE_BUDGET: int = _env_int("SESSION_PAGE_BUDGET", 40)
    SESSION_DETAIL_BUDGET: int = _env_int("SESSION_DETAIL_BUDGET", 120)

    # 服务
    HOST: str = os.getenv("HOST", "127.0.0.1")
    PORT: int = _env_int("PORT", 8765)

    # 路径
    DATA_DIR: Path = BASE_DIR / "data"
    DB_PATH: Path = DATA_DIR / "job_finder.db"
    SNAPSHOT_DIR: Path = DATA_DIR / "snapshots"
    EXPORT_DIR: Path = DATA_DIR / "exports"
    WEB_DIR: Path = BASE_DIR / "web"

    @classmethod
    def ensure_dirs(cls) -> None:
        for p in (cls.DATA_DIR, cls.SNAPSHOT_DIR, cls.EXPORT_DIR, cls.DB_PATH.parent):
            p.mkdir(parents=True, exist_ok=True)

    @property
    def cdp_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.CDP_PORT}"


config = Config()