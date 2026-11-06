"""SQLite 引擎与会话管理。"""
from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import config

config.ensure_dirs()

# 注意:不能用 StaticPool(全局单连接)——FastAPI 线程池并发请求共享同一连接
# 会造成 sqlite3 C 层死锁。默认池为每个会话提供独立连接,配合 WAL 允许读写并发。
engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False, "timeout": 15},
)


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _record) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA busy_timeout=10000")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


_NEW_COLUMNS = {
    "job": [
        ("industry", "VARCHAR(100) DEFAULT ''"),
        ("company_stage", "VARCHAR(50) DEFAULT ''"),
        ("company_scale", "VARCHAR(50) DEFAULT ''"),
        ("welfare", "VARCHAR(300) DEFAULT ''"),
        ("hr_online", "BOOLEAN DEFAULT 0"),
        ("hr_active_text", "VARCHAR(50) DEFAULT ''"),
        ("extra", "JSON DEFAULT '{}'"),
        # 抓取来源外键与数据表归属(SQLite 的 ALTER 限制:带 REFERENCES 的新列默认值必须为 NULL)
        ("crawl_task_id", "INTEGER"),
        ("table_id", "INTEGER"),
    ],
    "crawl_task": [
        ("keywords", "TEXT DEFAULT ''"),
        ("city", "VARCHAR(50) DEFAULT ''"),
        ("salary_range", "VARCHAR(50) DEFAULT ''"),
        ("skip_stats", "JSON DEFAULT '{}'"),
    ],
    "app_event": [
        ("url", "VARCHAR(500) DEFAULT ''"),
    ],
}

DEFAULT_TABLE = (1, "默认表", "default")


def init_db() -> None:
    from .entities import Base

    Base.metadata.create_all(engine)
    _migrate_columns()
    _seed_default_table()


def _seed_default_table() -> None:
    """保证默认数据表(id=1)存在,并把未归属的岗位归入默认表。"""
    from sqlalchemy import text

    with engine.connect() as conn:
        conn.execute(text(
            "INSERT OR IGNORE INTO job_table (id, name, slug, created_at) "
            "VALUES (:id, :name, :slug, datetime('now', 'localtime'))"
        ), {"id": DEFAULT_TABLE[0], "name": DEFAULT_TABLE[1], "slug": DEFAULT_TABLE[2]})
        conn.execute(text("UPDATE job SET table_id = 1 WHERE table_id IS NULL"))
        conn.commit()


def _migrate_columns() -> None:
    """为旧库补齐新增列并做一次性数据规范化(SQLite ALTER TABLE ADD COLUMN)。"""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    with engine.connect() as conn:
        for table, cols in _NEW_COLUMNS.items():
            if table not in inspector.get_table_names():
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, ddl in cols:
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
        # 旧 hr_active_at 曾以 ISO 微秒格式存储(datetime 列),统一规范到分钟级(幂等)
        conn.execute(text(
            "UPDATE job SET hr_active_at = substr(hr_active_at, 1, 16) "
            "WHERE length(hr_active_at) > 16 AND hr_active_at LIKE '____-__-__ __:__%'"
        ))
        # 一次性:历史 UTC 时间统一转为本地时间(UTC+8)。user_version 守卫防止重复偏移。
        ver = conn.execute(text("PRAGMA user_version")).scalar() or 0
        if ver < 2:
            for tbl, col in _LOCAL_TIME_COLUMNS:
                conn.execute(text(
                    f"UPDATE {tbl} SET {col} = datetime(substr({col}, 1, 19), '+8 hours') "
                    f"WHERE ifnull({col}, '') != ''"
                ))
            conn.execute(text("PRAGMA user_version = 2"))
        # 一次性:历史任务的词条/城市从快照回填到专用列(幂等:仅填空值)
        conn.execute(text(
            "UPDATE crawl_task SET "
            "keywords = ifnull(json_extract(filter_snapshot, '$.keyword'), ''), "
            "city = ifnull(json_extract(filter_snapshot, '$.city'), '') "
            "WHERE ifnull(keywords, '') = '' AND filter_snapshot IS NOT NULL"
        ))
        conn.commit()


# (表, 列):历史以 UTC 存储的时间列,启动时一次性转为本地时间
_LOCAL_TIME_COLUMNS = (
    ("job", "first_seen_at"),
    ("job", "updated_at"),
    ("crawl_task", "started_at"),
    ("crawl_task", "finished_at"),
    ("gear", "created_at"),
    ("search_filter", "created_at"),
)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖:每请求一个会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()