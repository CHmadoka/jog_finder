"""FastAPI 入口:API 路由 + 静态前端托管。"""
from __future__ import annotations

import logging
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

if config.WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(config.WEB_DIR)), name="assets")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(config.WEB_DIR / "index.html")


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "llm": config.llm_enabled}


def _port_in_use(host: str, port: int, timeout: float = 1.0) -> bool:
    """探测服务端口是否已被占用(单例守卫)。"""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


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

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    main()