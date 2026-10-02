"""文化知识库路由（PRD V1.1 §9：GET / POST / PUT / DELETE /api/kb）。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from ..db import get_db
from ..services import kb as svc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api/kb", tags=["kb"])


def _fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


@router.get("")
def list_entries(destination: str = "", q: str = "", verified: bool | None = None,
                 db: Session = Depends(get_db)) -> dict:
    rows = svc.list_entries(db, destination, q, verified)
    return {"items": [svc.serialize(e) for e in rows], "source_types": svc.SOURCE_TYPES,
            "coverage": svc.coverage(db, destination) if destination else None}


@router.get("/coverage")
def coverage(destination: str, db: Session = Depends(get_db)) -> dict:
    return svc.coverage(db, destination)


@router.post("")
def create(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        return svc.serialize(svc.create(db, payload))
    except ProjectError as e:
        _fail(e)


@router.put("/{entry_id}")
def update(entry_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        return svc.serialize(svc.update(db, entry_id, payload))
    except ProjectError as e:
        _fail(e)


@router.post("/{entry_id}/verify")
def verify(entry_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    try:
        return svc.serialize(svc.verify(db, entry_id, payload.get("source_name", ""),
                                        payload.get("source_url", ""), payload.get("source_type", "")))
    except ProjectError as e:
        _fail(e)


@router.delete("/{entry_id}")
def delete(entry_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        svc.delete(db, entry_id)
    except ProjectError as e:
        _fail(e)
    return {"deleted": True}


@router.post("/draft")
def draft(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    """AI 起草条目（标「待核实」，核实前不会被引用）。按次计 LLM 费用，量很小。"""
    try:
        rows = svc.draft(db, payload.get("destination", ""), int(payload.get("count") or 8))
    except ProjectError as e:
        _fail(e)
    return {"items": [svc.serialize(e) for e in rows]}


@router.post("/from-check/{check_id}")
def from_check(check_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        e = svc.from_check(db, check_id, payload.get("destination", ""), payload.get("content", ""),
                           payload.get("source_name", ""))
    except ProjectError as e:
        _fail(e)
    return svc.serialize(e)
