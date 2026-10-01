"""SQLAlchemy 会话与引擎。SQLite 起步，预留 PostgreSQL 迁移（改 DATABASE_URL 即可）。"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import settings

_s = settings()


def _url() -> str:
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit
    return f"sqlite:///{_s.db_path.as_posix()}"


engine = create_engine(
    _url(),
    future=True,
    connect_args={"check_same_thread": False, "timeout": 30},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _rec):  # pragma: no cover - 驱动层钩子
    if engine.dialect.name != "sqlite":
        return
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA busy_timeout=30000")
    cur.close()


def get_db() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """给后台 worker 用：自管事务。"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401  确保模型已注册

    models.Base.metadata.create_all(engine)
    _add_missing_columns()


# 轻量迁移：SQLite 的 create_all 只建表、**不会给已存在的表加列**。
# 项目没有引入 alembic（单体 MVP 不值得那套复杂度），
# 所以这里用「PRAGMA 读现有列 → ALTER TABLE 补缺」的方式做增量迁移。
# 新增字段时往这里加一条即可，幂等、可重复执行。
_ADDED_COLUMNS: dict[str, dict[str, str]] = {
    "timeline": {
        # 歌词对齐的来源与质量（engine / note / match_ratio / vocal_span）。
        # 有了它，界面才能如实告诉用户「这条时间轴是唱出来的还是估出来的」。
        "align_meta": "JSON",
    },
}


def _add_missing_columns() -> None:
    if engine.dialect.name != "sqlite":
        return
    from sqlalchemy import text

    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            have = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not have:
                continue      # 表还不存在（理论上不会）
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
