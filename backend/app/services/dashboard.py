"""生产看板聚合（PRD §6.1：汇总指标 / 待办 / GPU 队列 / 模型战绩）。"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import state_machine as sm
from ..serialize import dt as iso
from ..config import settings
from ..models import Job, Project, RenderJob, TrendCard
from . import assets as assets_svc
from . import budget
from .music import list_models

PIPELINE_STATES = list(sm.STATES)


def summary(db: Session) -> dict:
    projects = list(db.execute(select(Project)).scalars())
    running = [p for p in projects if p.status not in ("delivered",)]
    samples = [p for p in running if p.type == "sample"]
    demos = [p for p in running if p.type == "demo"]

    todos = [p for p in projects if sm.is_human(p.status)]
    todo_by_type: dict[str, int] = {}
    for p in todos:
        t = sm.TODO_TYPE.get(p.status, "其他")
        todo_by_type[t] = todo_by_type.get(t, 0) + 1

    week_start = dt.datetime.now() - dt.timedelta(days=7)
    renders = list(db.execute(select(RenderJob).where(RenderJob.status == "done")).scalars())
    recent = [r for r in renders if (r.updated_at or r.created_at) >= week_start]
    landscape = len([r for r in recent if r.variant in ("landscape", "clean", "sub_zh", "sub_zh_en")])
    portrait = len([r for r in recent if r.variant in ("portrait", "short", "highlight30", "essence60")])

    ast = assets_svc.stats(db)

    return {
        "cards": {
            "running_projects": {"total": len(running), "sample": len(samples), "demo": len(demos)},
            "pending_human": {"total": len(todos), "by_type": todo_by_type},
            "week_renders": {"total": len(recent), "landscape": landscape, "portrait": portrait},
            "assets": ast,
        },
        "todos": [_todo(p) for p in sorted(todos, key=lambda x: x.updated_at, reverse=True)],
        "columns": _columns(projects),
        "gpu_queue": gpu_queue(db),
        "device": device_status(db),
        "models": list_models(db),
        "budget": budget.status(db),
    }


def _todo(p: Project) -> dict:
    return {
        "project_id": p.id, "type": sm.TODO_TYPE.get(p.status, "其他"),
        "title": p.name, "status": p.status, "state_label": sm.STATES[p.status].label,
        "destination": p.destination, "updated_at": iso(p.updated_at),
        "hint": sm.STATES[p.status].enter_note,
    }


def _columns(projects: list[Project]) -> list[dict]:
    cols = []
    for name in sm.COLUMNS:
        items = [p for p in projects if sm.column_of(p.status) == name]
        items.sort(key=lambda p: p.updated_at, reverse=True)
        cols.append({
            "name": name,
            "count": len(items),
            "items": [{
                "id": p.id, "name": p.name, "type": p.type, "language": p.language,
                "status": p.status, "state_label": sm.STATES[p.status].label,
                "status_note": p.status_note, "destination": p.destination,
                "waiting_human": sm.is_human(p.status),
                "updated_at": iso(p.updated_at),
            } for p in items],
        })
    return cols


def gpu_queue(db: Session, limit: int = 30) -> list[dict]:
    rows = db.execute(
        select(Job).order_by(Job.created_at.desc()).limit(limit)
    ).scalars().all()
    out = []
    for j in rows:
        out.append({
            "id": j.id, "queue": j.queue, "type": j.type, "name": j.name,
            "status": j.status, "progress": j.progress, "vram_mb": j.vram_mb,
            "error": j.error, "retries": j.retries, "project_id": j.project_id,
            "started_at": iso(j.started_at), "finished_at": iso(j.finished_at),
            "created_at": iso(j.created_at),
        })
    return out


def device_status(db: Session) -> dict:
    """本地 Worker 心跳：以最近一次任务活动时间近似（PRD F1-7，超 60 秒未上报视为离线）。"""
    last = db.execute(
        select(Job).where(Job.finished_at.is_not(None)).order_by(Job.finished_at.desc()).limit(1)
    ).scalar_one_or_none()
    running = db.execute(select(Job).where(Job.status == "running")).scalars().all()
    now = dt.datetime.now(dt.timezone.utc)
    last_dt = (last.finished_at if last else None)
    if last_dt is not None and last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=dt.timezone.utc)
    idle_s = (now - last_dt).total_seconds() if last_dt else None
    return {
        "name": "本地 4090 Worker",
        "concurrency": {"api": settings().api_concurrency, "gpu": settings().gpu_concurrency},
        "running": len(running),
        "queued": len(db.execute(select(Job).where(Job.status == "queued")).scalars().all()),
        "last_activity_s": None if idle_s is None else round(idle_s),
        "online": bool(running) or idle_s is None or idle_s < 60,
        "gpu_vram_mb": max([j.vram_mb for j in running], default=0),
    }


def trends_summary(db: Session) -> dict:
    from .trends import weeks

    return {"weeks": weeks(db), "card_count": len(db.execute(select(TrendCard)).scalars().all())}
