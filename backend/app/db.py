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
    added = _add_missing_columns()
    _backfill_v11(added)


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
    # ---- PRD V1.1 ----
    "project": {"locked_at": "DATETIME", "parent_project_id": "VARCHAR(36)"},
    "lyric_version": {"checks_done": "BOOLEAN DEFAULT 0"},
    "lyric_check": {"keep_reason": "TEXT DEFAULT ''", "needs_manual": "BOOLEAN DEFAULT 0"},
    "music_candidate": {"round_id": "VARCHAR(36)", "is_mock": "BOOLEAN DEFAULT 0",
                        "audio_token": "VARCHAR(40) DEFAULT ''"},
    "asset": {"source": "VARCHAR(10) DEFAULT 'shot'", "is_seed": "BOOLEAN DEFAULT 0"},
    "trend_card": {"is_seed": "BOOLEAN DEFAULT 0"},
}


def _add_missing_columns() -> set[tuple[str, str]]:
    """补列，返回本次新加的 (表, 列)，供回填判断「是不是第一次迁移」。"""
    added: set[tuple[str, str]] = set()
    if engine.dialect.name != "sqlite":
        return added
    from sqlalchemy import text

    with engine.begin() as conn:
        for table, cols in _ADDED_COLUMNS.items():
            have = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if not have:
                continue      # 表还不存在（理论上不会）
            for name, ddl in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
                    added.add((table, name))
    return added


def _backfill_v11(added: set[tuple[str, str]]) -> None:
    """把 V1.0 存量数据按 V1.1 口径归位（PRD V1.1 附录 B）。全部幂等。

    只打标记、不删数据：mock 候选与其评分保留可追溯，靠 is_mock 排除出统计与交付。
    """
    import uuid

    from sqlalchemy import select, text

    from . import models as M

    with session_scope() as db:
        # 1. 列是本次新加的 → 存量歌词版本当时都跑过检查，标为已检查，
        #    否则老项目全部会显示「检查中」
        if ("lyric_version", "checks_done") in added:
            db.execute(text("UPDATE lyric_version SET checks_done = 1"))

        # 2. 已交付项目锁定
        for p in db.execute(select(M.Project).where(
                M.Project.status == "delivered", M.Project.locked_at.is_(None))).scalars():
            p.locked_at = p.updated_at

        # 3. 候选：归入轮次、标 mock、发放音频 token
        rounds: dict[tuple[str, int], M.MusicRound] = {
            (r.project_id, r.round_no): r for r in db.execute(select(M.MusicRound)).scalars()}
        for c in db.execute(select(M.MusicCandidate)).scalars():
            if not c.audio_token:
                c.audio_token = uuid.uuid4().hex
            if not c.is_mock and "mock-" in (c.audio_path or "").replace("\\", "/").rsplit("/", 1)[-1]:
                c.is_mock = True
            if c.round_id:
                continue
            key = (c.project_id, c.round)
            r = rounds.get(key)
            if r is None:
                # V1.0 没有选型轮的概念，存量轮次一律视为生产模式（不计入战绩）
                r = M.MusicRound(project_id=c.project_id, kind="production", round_no=c.round,
                                 model_keys=[], variants=1)
                db.add(r)
                db.flush()
                rounds[key] = r
            c.round_id = r.id
            if c.model_key not in (r.model_keys or []):
                r.model_keys = [*(r.model_keys or []), c.model_key]
        for p in db.execute(select(M.Project).where(M.Project.selected_candidate_id.is_not(None))).scalars():
            sel = db.get(M.MusicCandidate, p.selected_candidate_id)
            if sel is not None and sel.round_id:
                r = db.get(M.MusicRound, sel.round_id)
                if r is not None and not r.winner_candidate_id:
                    r.winner_candidate_id = sel.id

        # 4. 素材：AI 生成的改来源与授权口径；种子演示素材打标
        from .services.assets import DEMO_ASSET_NAMES

        for a in db.execute(select(M.Asset)).scalars():
            path = (a.file_path or "").replace("\\", "/")
            is_ai = (a.licensor or "").startswith("AI 生成") or "/storyboard/" in path
            if is_ai and a.source != "ai":
                a.source = "ai"
                a.license_status = "ai_generated"
            if not is_ai and not a.is_seed and a.name in DEMO_ASSET_NAMES:
                a.is_seed = True

        # 5. 演示素材卡
        db.execute(text("UPDATE trend_card SET is_seed = 1 WHERE video_id LIKE 'demo%' AND is_seed = 0"))
