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


def main() -> None:
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":
    main()