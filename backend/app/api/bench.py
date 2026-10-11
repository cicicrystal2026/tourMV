"""模型对比台与一键分发的接口。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import BenchRun, Project, StoryboardShot
from ..services import bench as svc
from ..services import distribute as dist
from ..services.projects import ProjectError

router = APIRouter(prefix="/api", tags=["bench"])


def _fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


# ------------------------------------------------------------------ 模型对比台
@router.get("/bench/options")
def options(db: Session = Depends(get_db)) -> dict:
    return svc.options(db)


@router.get("/bench/runs")
def runs(kind: str = "", db: Session = Depends(get_db)) -> list[dict]:
    return svc.list_runs(db, kind)


@router.get("/bench/shots")
def shots(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    """从已有项目里挑一句歌词来对比（沿用它的景点和画面描述）。"""
    rows = db.execute(select(StoryboardShot).where(StoryboardShot.project_id == project_id,
                                                   StoryboardShot.dup_of == 0)
                      .order_by(StoryboardShot.order)).scalars()
    return [{"id": r.id, "order": r.order, "line": r.line_text, "place": r.place, "prompt": r.prompt}
            for r in rows]


@router.post("/bench/image")
def start_image(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        run = svc.start_image(db, destination=payload.get("destination", ""), place=payload.get("place", ""),
                              line=payload.get("line", ""), prompt=payload.get("prompt", ""),
                              providers=payload.get("providers") or [], title=payload.get("title", ""))
    except ProjectError as e:
        _fail(e)
    return svc.run_payload(db, run)


@router.post("/bench/lyric")
def start_lyric(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        run = svc.start_lyric(db, destination=payload.get("destination", ""), genre=payload.get("genre", ""),
                              mood=payload.get("mood") or [], duration_s=int(payload.get("duration_s") or 0),
                              style_prompt=payload.get("style_prompt", ""),
                              models=payload.get("models") or [], title=payload.get("title", ""))
    except ProjectError as e:
        _fail(e)
    return svc.run_payload(db, run)


@router.get("/bench/runs/{run_id}")
def run_detail(run_id: str, db: Session = Depends(get_db)) -> dict:
    run = db.get(BenchRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="对比不存在")
    return svc.run_payload(db, run)


@router.put("/bench/results/{result_id}")
def rate(result_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        return svc.rate(db, result_id, score=payload.get("score"), picked=payload.get("picked"),
                        note=payload.get("note"))
    except ProjectError as e:
        _fail(e)


@router.get("/bench/summary")
def summary(db: Session = Depends(get_db)) -> dict:
    return svc.summary(db)


@router.get("/bench/export")
def export(db: Session = Depends(get_db)) -> PlainTextResponse:
    return PlainTextResponse(svc.export_csv(db), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="model_bench.csv"'})


# ------------------------------------------------------------------ 一键分发
@router.get("/distribute/config")
def dist_config() -> dict:
    return dist.get_config()


@router.put("/distribute/config")
def dist_save(payload: dict = Body(...)) -> dict:
    try:
        return dist.save_config(payload.get("root"), payload.get("channels"))
    except ProjectError as e:
        _fail(e)


@router.get("/distribute/projects")
def dist_projects(db: Session = Depends(get_db)) -> list[dict]:
    """可分发的项目：已交付的。"""
    rows = db.execute(select(Project).where(Project.locked_at.is_not(None))
                      .order_by(Project.updated_at.desc())).scalars()
    return [{"id": p.id, "name": p.name, "destination": p.destination} for p in rows]


@router.get("/distribute/projects/{project_id}")
def dist_plan(project_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return dist.public_plan(db, project_id)
    except ProjectError as e:
        _fail(e)


@router.post("/distribute/projects/{project_id}")
def dist_run(project_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    try:
        return dist.run(db, project_id, payload.get("channels"))
    except ProjectError as e:
        _fail(e)
