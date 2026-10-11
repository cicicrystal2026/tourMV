"""项目与看板路由。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serialize as S
from .. import state_machine as sm
from ..config import settings
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
            duration_s=int(payload.get("duration_s") or settings().song_default_seconds),
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
        # 是否允许占位（mock）结果参与选定：生产环境为 False
        "allow_mock": settings().allow_mock,
        # 选定的曲目是不是占位音频（测试环境里成片会是「嗡嗡声」，界面要明确提示）
        "music_is_mock": _selected_is_mock(db, d["project"]),
        # 音频分析失败（如对齐不可用）：界面给「重试 / 按字数估算继续」两个出口
        "analyze_error": _analyze_error(db, d["project"]),
    }


def _analyze_error(db, p) -> str:
    from ..models import Job
    if p.status != "audio_analyzing":
        return ""
    j = db.execute(select(Job).where(Job.project_id == p.id, Job.type == "audio_analyze")
                   .order_by(Job.created_at.desc()).limit(1)).scalar_one_or_none()
    return (j.error or "失败") if j is not None and j.status == "failed" else ""


def _selected_is_mock(db: Session, p) -> bool:
    from ..models import MusicCandidate
    c = db.get(MusicCandidate, p.selected_candidate_id) if p.selected_candidate_id else None
    return bool(c and c.is_mock)


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


@router.get("/projects/{project_id}/review")
def project_review(project_id: str, db: Session = Depends(get_db)) -> dict:
    """项目复盘（F1-11）：各人工节点工时、费用、体验指标。"""
    from ..services import metrics
    try:
        return metrics.review(db, project_id)
    except ProjectError as e:
        _fail(e)


@router.post("/projects/{project_id}/worklog/heartbeat")
def worklog_heartbeat(project_id: str, payload: dict = Body(default={}),
                      db: Session = Depends(get_db)) -> dict:
    """页面在前台且有操作时，前端每 30 秒报一次，用来计有效工时。"""
    from ..services import metrics
    try:
        return metrics.heartbeat(db, project_id, int(payload.get("seconds") or metrics.HEARTBEAT_S))
    except ProjectError as e:
        _fail(e)


@router.put("/projects/{project_id}/worklog/{node}")
def worklog_manual(project_id: str, node: str, payload: dict = Body(...),
                   db: Session = Depends(get_db)) -> dict:
    """补录工时（保留自动记录的原值）。"""
    from ..services import metrics
    try:
        return metrics.set_manual(db, project_id, node, float(payload.get("minutes") or 0),
                                  payload.get("note", ""))
    except (ProjectError, ValueError) as e:
        _fail(e)


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
    from ..models import MusicCandidate

    by_kind: dict[str, dict] = {}
    by_model: dict[str, dict] = {}
    for c in rows:
        g = by_kind.setdefault(c.kind, {"calls": 0, "cost": 0.0, "elapsed_s": 0.0, "errors": 0,
                                        "input_tokens": 0, "output_tokens": 0})
        g["calls"] += 1
        g["cost"] = round(g["cost"] + c.cost, 4)
        g["elapsed_s"] = round(g["elapsed_s"] + c.elapsed_s, 2)
        g["errors"] += 1 if c.status == "error" else 0
        g["input_tokens"] += c.input_tokens or 0
        g["output_tokens"] += c.output_tokens or 0
        if c.status == "ok":
            m = by_model.setdefault(f"{c.kind}:{c.model}", {"kind": c.kind, "model": c.model, "calls": 0, "cost": 0.0})
            m["calls"] += 1
            m["cost"] = round(m["cost"] + c.cost, 4)
    # 生曲费用记在候选曲目上（按首计价），不在调用记录里；占位曲不花钱
    songs = [x for x in db.execute(select(MusicCandidate).where(MusicCandidate.project_id == project_id)).scalars()
             if x.status == "done" and not x.is_mock]
    if songs:
        by_kind["music"] = {"calls": len(songs), "cost": round(sum(x.cost or 0 for x in songs), 4),
                            "elapsed_s": round(sum(x.elapsed_s or 0 for x in songs), 2), "errors": 0,
                            "input_tokens": 0, "output_tokens": 0}
        for x in songs:
            m = by_model.setdefault(f"music:{x.model_name or x.model_key}",
                                    {"kind": "music", "model": x.model_name or x.model_key, "calls": 0, "cost": 0.0})
            m["calls"] += 1
            m["cost"] = round(m["cost"] + (x.cost or 0), 4)
    # 累计费用 = 全部环节之和。V1.1 之前这里只返回 project.total_cost，而它只累计了生曲，
    # 生图、写词、预审的钱都没算进去（一支歌显示 ¥0.32，实际约 ¥3.1）
    total = round(sum(v["cost"] for v in by_kind.values()), 4)
    return {"project_total_cost": total, "by_kind": by_kind,
            "by_model": sorted(by_model.values(), key=lambda v: -v["cost"]),
            "calls": [S.ai_call(c) for c in rows[:200]]}
