"""FastAPI 入口:API 路由 + 静态前端托管。"""
from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .api import api_router
from .config import config
from .models import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(config.DATA_DIR / "app.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("job_finder")


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.ensure_dirs()
    init_db()
    logger.info("数据库就绪: %s", config.DB_PATH)
    logger.info("Web 界面: http://%s:%d", config.HOST, config.PORT)
    yield
    # 退出时优雅关闭浏览器连接(不关闭用户浏览器进程)
    from .browser import browser_manager
    from .scheduler import scheduler

    for pid in list(scheduler.status().keys()):
        scheduler.request_stop(pid)
    await browser_manager.shutdown()


app = FastAPI(title="job_finder", version="0.1.0", lifespan=lifespan)
app.include_router(api_router)


@app.middleware("http")
async def no_cache_for_local_ui(request, call_next):
    """本地界面资源强制协商缓存(ETag/304)。

    StaticFiles/FileResponse 默认无 Cache-Control,浏览器按启发式规则强缓存
    app.js 等文件——改版后普通刷新仍跑旧脚本,曾导致"切表不刷新/新修复不生效"
    这类"改了没反应"的假象。no-cache = 可缓存但每次必须协商:文件没变 304
    (不重传),变了立即拿到新文件。本地单用户服务,协商开销可忽略。
    """
    resp = await call_next(request)
    p = request.url.path
    if p == "/" or p.startswith("/assets"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


if config.WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(config.WEB_DIR)), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(config.WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True}


def _port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    """探测服务端口是否已被占用(单例守卫)。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _install_console_binding() -> None:
    """Windows:把服务进程的生命周期强制绑定到可见控制台窗口(仅 Windows 生效)。

    背景:`启动.bat` 以 `start "job_finder-server" cmd /c "...python -m app.main & pause"`
    弹出独立控制台窗口。部分终端宿主(如 Windows Terminal)关闭窗口时只终止直接子进程
    cmd.exe,python 父子进程会成为孤儿继续监听端口,出现"窗口关了服务还在跑"。

    规则:窗口即声明——必须有可见控制台窗口,否则在启动阶段直接拒绝。隐藏窗口实例
    既看不见又没有停止入口,最容易遗留成杀不掉的常驻服务(实测 Start-Process
    -WindowStyle Hidden 拉起的实例,守卫初始挂载 2 个进程,永不触发退出)。两类拒绝:
    1. 无控制台(分离/计划任务启动,GetConsoleWindow 返回 NULL);
    2. 控制台存在但窗口隐藏(IsWindowVisible 为 FALSE——隐藏窗口句柄同样非 NULL,
       仅凭 GetConsoleWindow 区分不了可见与隐藏)。
    确需无窗口后台运行时,设置环境变量 JOB_FINDER_HEADLESS=1 显式声明。

    可见窗口场景启用两层保险:
    1. SetConsoleCtrlHandler:收到 CTRL_CLOSE/LOGOFF/SHUTDOWN 立即 os._exit,
       避免优雅关闭流程(lifespan 清理)拖过系统 5 秒宽限期后被强杀留下残余连接;
    2. 守护线程轮询 GetConsoleProcessList:控制台上挂载的进程数变少,说明宿主/壳进程
       (cmd 或终端 shell)已消失,整个服务进程树立即退出(启动器壳会随子进程退出)。
    """
    if os.name != "nt":
        return
    import ctypes
    import threading

    if os.environ.get("JOB_FINDER_HEADLESS", "").strip().lower() in ("1", "true", "yes"):
        logger.info("JOB_FINDER_HEADLESS=1,按后台服务运行:跳过可见窗口强制与生命周期守卫")
        return

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    hwnd = kernel32.GetConsoleWindow()

    def _refuse(reason: str) -> None:
        msg = (
            f"[job_finder] 拒绝启动:{reason}。"
            "服务生命周期强制绑定可见控制台窗口,请通过 启动.bat 启动;"
            "确需无窗口后台运行请设置环境变量 JOB_FINDER_HEADLESS=1。"
        )
        logger.warning(msg)
        print(msg, flush=True)
        raise SystemExit(2)

    if not hwnd:
        _refuse("当前无控制台(分离启动)")
    if not user32.IsWindowVisible(hwnd):
        _refuse("控制台窗口为隐藏状态(如 Start-Process -WindowStyle Hidden)")

    CTRL_CLOSE_EVENT = 2
    CTRL_LOGOFF_EVENT = 5
    CTRL_SHUTDOWN_EVENT = 6
    Handler = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def _on_ctrl(ctrl_type: int) -> int:
        if ctrl_type in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
            os._exit(0)
        return 0  # 其他事件(Ctrl+C 等)交给 Python 默认处理,走优雅关闭

    _on_ctrl_ref = Handler(_on_ctrl)  # 模块级持有引用,防止回调被 GC
    _install_console_binding._handler_ref = _on_ctrl_ref
    kernel32.SetConsoleCtrlHandler(_on_ctrl_ref, True)

    def _console_pids() -> list[int]:
        buf = (ctypes.c_uint * 64)()
        got = kernel32.GetConsoleProcessList(buf, 64)
        return list(buf[:got]) if got else []

    initial_count = len(_console_pids())

    def _watch() -> None:
        while True:
            time.sleep(1)
            if len(_console_pids()) < initial_count:
                logger.info("控制台窗口已关闭,服务进程退出")
                os._exit(0)

    threading.Thread(target=_watch, name="console-binding", daemon=True).start()
    logger.info("控制台生命周期绑定已启用(初始挂载 %d 个进程)", initial_count)


def main() -> None:
    import uvicorn

    # 单例守卫:端口已被监听说明服务已在运行,直接退出而非报绑定错误。
    # 注意 .venv\Scripts\python.exe 是启动器壳,还会派生基础解释器子进程,
    # 任务管理器里一个服务会显示为两个 python.exe,属正常现象。
    if _port_in_use(config.HOST, config.PORT):
        print(
            f"[job_finder] 服务已在运行: http://{config.HOST}:{config.PORT}"
            "(单例模式,本次启动直接退出)"
        )
        return

    # 生命周期绑定:控制台窗口关闭时,服务进程树必须一起退出。
    _install_console_binding()

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    main()