"""音乐模型与候选曲目路由。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
# 本模块下有名为 select 的路由处理函数（POST /music/select），
# 会遮蔽裸的 select，故 SQLAlchemy 的 select 一律用别名 sa_select。
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from .. import serialize as S
from ..db import get_db
from .deps import guard_locked
from ..models import MusicModel, Rating
from ..services import music as svc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api", tags=["music"], dependencies=[Depends(guard_locked)])


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
    """mode=production（默认，生产模式）/ selection（选型轮，强制盲听）。"""
    mode = payload.get("mode") or "production"
    try:
        out = svc.generate(
            db, project_id,
            model_keys=payload.get("models") or [],
            variants=int(payload.get("variants") or (2 if mode == "selection" else 1)),
            style_prompt=payload.get("style_prompt"),
            mode=mode,
            round_id=payload.get("round_id") or None,
        )
    except ProjectError as e:
        _fail(e)
    return out


@router.get("/music/selection-report")
def selection_report(format: str = "md", db: Session = Depends(get_db)):
    """选型结论导出（F1-10）：md / csv。"""
    from fastapi.responses import PlainTextResponse

    from ..services import metrics
    body, mime = metrics.selection_report(db, "csv" if format == "csv" else "md")
    name = "selection-report." + ("csv" if format == "csv" else "md")
    return PlainTextResponse(body, media_type=mime,
                             headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/projects/{project_id}/music/candidates")
def candidates(project_id: str, round: int | None = None, round_id: str | None = None,
               blind: bool | None = None, db: Session = Depends(get_db)) -> dict:
    """候选列表。是否盲听由**轮次状态**决定（选型轮未揭晓即盲听），
    与 blind 参数无关 —— 客户端不能通过 blind=false 把模型信息要回来。"""
    return svc.candidates_payload(db, project_id, round_id=round_id, round_no=round)


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
