"""项目与看板路由。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serialize as S
from .. import state_machine as sm
from ..db import get_db
from ..models import AiCall, Project, PublishMetric
from ..services import dashboard as dash
from ..services import projects as svc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api", tags=["projects"])


def _fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


@router.get("/state-machine")
def state_machine() -> dict:
    return {
        "states": sm.describe(),
        "columns": sm.COLUMNS,
        "human_states": sm.HUMAN_STATES,
        "reject_targets": sm.REJECT_TARGETS,
        "todo_types": sm.TODO_TYPE,
    }


@router.get("/dashboard/summary")
def dashboard_summary(db: Session = Depends(get_db)) -> dict:
    return dash.summary(db)


@router.get("/projects")
def list_projects(status: str | None = None, limit: int = Query(200, le=500),
                  db: Session = Depends(get_db)) -> list[dict]:
    return [S.project(p) for p in svc.list_projects(db, status, limit)]


@router.post("/projects")
def create_project(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        p = svc.create(
            db,
            name=payload.get("name") or "未命名样片",
            destination=payload.get("destination", ""),
            type_=payload.get("type", "sample"),
            language=payload.get("language", "zh"),
            trend_card_ids=payload.get("trend_card_ids") or [],
            mood=payload.get("mood") or [],
            genre=payload.get("genre", ""),
            bpm_min=int(payload.get("bpm_min") or 70),
            bpm_max=int(payload.get("bpm_max") or 100),
            duration_s=int(payload.get("duration_s") or 180),
            vocal=payload.get("vocal", "female"),
            style_prompt=payload.get("style_prompt", ""),
            forbidden=payload.get("forbidden", ""),
        )
    except ProjectError as e:
        _fail(e)
    return S.project(p)


@router.get("/projects/{project_id}")
def project_detail(project_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        d = svc.detail(db, project_id)
    except ProjectError as e:
        _fail(e)
    metrics = list(db.execute(select(PublishMetric).where(PublishMetric.project_id == project_id)).scalars())
    return {
        "project": S.project(d["project"]),
        "brief": S.brief(d["brief"]),
        "lyric": S.lyric(d["lyric"]),
        "timeline": S.timeline(d["timeline"]) if d["timeline"] else None,
        "renders": [S.render_job(r) for r in d["renders"]],
        "metrics": [S.publish_metric(m) for m in metrics],
        "state": d["state"],
        "gate": d["gate"],
    }


@router.post("/projects/{project_id}/advance")
def advance(project_id: str, payload: dict | None = Body(default=None),
            db: Session = Depends(get_db)) -> dict:
    try:
        r = svc.advance(db, project_id, payload or {})
    except svc.GateError as e:
        # 409：闸门不满足。detail 是字符串（兼容旧前端），reasons 给逐条原因
        raise HTTPException(status_code=409, detail=str(e)) from e
    except (ProjectError, sm.StateTransitionError) as e:
        _fail(e)
    return {**r, "project": S.project(svc.get(db, project_id))}


@router.post("/projects/{project_id}/revise")
def revise(project_id: str, db: Session = Depends(get_db)) -> dict:
    """已交付项目新建修订版（PRD V1.1 §4.2）。"""
    try:
        p = svc.revise(db, project_id)
    except ProjectError as e:
        _fail(e)
    return S.project(p)


@router.post("/projects/{project_id}/reject")
def reject(project_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        r = svc.reject(db, project_id, payload.get("target", ""))
    except (ProjectError, sm.StateTransitionError) as e:
        _fail(e)
    return {**r, "project": S.project(svc.get(db, project_id))}


@router.post("/projects/{project_id}/metrics")
def add_metric(project_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    m = PublishMetric(
        project_id=project_id, platform=payload.get("platform", ""),
        url=payload.get("url", ""), published_at=payload.get("published_at", ""),
        views_7d=int(payload.get("views_7d") or 0),
        completion=float(payload.get("completion") or 0),
        engagement=float(payload.get("engagement") or 0),
        note=payload.get("note", ""),
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return S.publish_metric(m)


@router.get("/projects/{project_id}/calls")
def project_calls(project_id: str, db: Session = Depends(get_db)) -> dict:
    """PRD §12.1：每支样片的 API 费用与本地耗时可查询。"""
    rows = list(db.execute(select(AiCall).where(AiCall.project_id == project_id)
                           .order_by(AiCall.created_at.desc())).scalars())
    p = svc.get(db, project_id)
    by_kind: dict[str, dict] = {}
    for c in rows:
        g = by_kind.setdefault(c.kind, {"calls": 0, "cost": 0.0, "elapsed_s": 0.0, "errors": 0})
        g["calls"] += 1
        g["cost"] = round(g["cost"] + c.cost, 4)
        g["elapsed_s"] = round(g["elapsed_s"] + c.elapsed_s, 2)
        g["errors"] += 1 if c.status == "error" else 0
    return {"project_total_cost": p.total_cost, "by_kind": by_kind,
            "calls": [S.ai_call(c) for c in rows[:200]]}
