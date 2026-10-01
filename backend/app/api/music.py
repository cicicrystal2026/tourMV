"""音乐模型与候选曲目路由。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
# 本模块下有名为 select 的路由处理函数（POST /music/select），
# 会遮蔽裸的 select，故 SQLAlchemy 的 select 一律用别名 sa_select。
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from .. import serialize as S
from ..db import get_db
from ..models import MusicModel, Rating
from ..services import music as svc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api", tags=["music"])


def _fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


@router.get("/music/models")
def models(db: Session = Depends(get_db)) -> list[dict]:
    return svc.list_models(db)


@router.put("/music/models/{key}")
def update_model(key: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    m = db.execute(sa_select(MusicModel).where(MusicModel.key == key)).scalar_one_or_none()
    if m is None:
        _fail(ProjectError("模型不存在"))
    if "enabled" in payload:
        m.enabled = bool(payload["enabled"])
    if "note" in payload:
        m.note = payload["note"]
    db.commit()
    return next(x for x in svc.list_models(db) if x["key"] == key)


@router.post("/projects/{project_id}/music/generate")
def generate(project_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.generate(
            db, project_id,
            model_keys=payload.get("models") or [],
            variants=int(payload.get("variants") or 1),
            style_prompt=payload.get("style_prompt"),
        )
    except ProjectError as e:
        _fail(e)
    return out


@router.get("/projects/{project_id}/music/candidates")
def candidates(project_id: str, round: int | None = None, db: Session = Depends(get_db)) -> dict:
    return {"round": round or svc.current_round(db, project_id),
            "items": svc.candidates(db, project_id, round)}


@router.put("/music/candidates/{candidate_id}/rating")
def rate(candidate_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        return svc.rate(db, candidate_id,
                        diction=int(payload.get("diction") or 0),
                        melody=int(payload.get("melody") or 0),
                        arrangement=int(payload.get("arrangement") or 0),
                        rater=payload.get("rater", "default"))
    except ProjectError as e:
        _fail(e)


@router.get("/music/candidates/{candidate_id}/rating")
def get_rating(candidate_id: str, db: Session = Depends(get_db)) -> dict:
    r = db.execute(sa_select(Rating).where(Rating.candidate_id == candidate_id,
                                           Rating.rater == "default")).scalar_one_or_none()
    if r is None:
        return {"diction": 0, "melody": 0, "arrangement": 0, "total": 0}
    return {"diction": r.diction, "melody": r.melody, "arrangement": r.arrangement, "total": r.total}


@router.post("/projects/{project_id}/music/select")
def select(project_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.select(db, project_id, payload.get("candidate_id", ""))
    except ProjectError as e:
        _fail(e)
    return {**out, "models": svc.list_models(db)}


@router.post("/projects/{project_id}/music/regenerate")
def regenerate(project_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    try:
        return svc.regenerate_with_winner(db, project_id, int(payload.get("variants") or 1))
    except ProjectError as e:
        _fail(e)
