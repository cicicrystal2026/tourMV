"""歌词路由。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import serialize as S
from ..db import get_db
from ..queue import enqueue
from ..services import lyrics as svc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api", tags=["lyrics"])


def _fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


@router.get("/projects/{project_id}/lyrics")
def get_lyrics(project_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        ver = svc.latest(db, project_id)
    except ProjectError as e:
        _fail(e)
    if ver is None:
        return {"current": None, "checks": [], "summary": {"total": 0, "pending": 0, "by_type": {}},
                "versions": []}
    checks = svc.checks_of(db, ver.id)
    return {
        "current": S.lyric(ver),
        "checks": [S.check(c) for c in checks],
        "summary": svc.check_summary(checks),
        "versions": [S.lyric(v) for v in svc.versions(db, project_id)],
    }


@router.post("/projects/{project_id}/lyrics/generate")
def generate_lyrics(project_id: str, sync: bool = False, db: Session = Depends(get_db)) -> dict:
    try:
        if sync:
            ver, real = svc.generate(db, project_id)
            return {"mode": "sync", "lyric": S.lyric(ver), "llm": real}
        j = enqueue(db, "lyric_generate", project_id=project_id, ref_id=project_id, name="生成歌词")
    except ProjectError as e:
        _fail(e)
    return {"mode": "job", "job_id": j.id}


@router.post("/projects/{project_id}/lyrics/check")
def recheck(project_id: str, db: Session = Depends(get_db)) -> dict:
    ver = svc.latest(db, project_id)
    if ver is None:
        _fail(ProjectError("还没有歌词版本"))
    checks = svc.run_checks(db, ver)
    return {"checks": [S.check(c) for c in checks], "summary": svc.check_summary(checks)}


@router.put("/projects/{project_id}/lyrics/{version}")
def save_lyrics(project_id: str, version: int, payload: dict = Body(...),
                db: Session = Depends(get_db)) -> dict:
    """保存编辑 → 生成新版本（不覆盖历史，PRD F2-6）。"""
    sections = payload.get("sections")
    if not isinstance(sections, list):
        _fail(ProjectError("sections 必须是数组"))
    try:
        ver = svc.save_version(db, project_id, sections, source=payload.get("source", "manual"))
    except ProjectError as e:
        _fail(e)
    checks = svc.checks_of(db, ver.id)
    return {"lyric": S.lyric(ver), "checks": [S.check(c) for c in checks],
            "summary": svc.check_summary(checks)}


@router.post("/projects/{project_id}/lyrics/rewrite")
def rewrite(project_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        ver = svc.rewrite(db, project_id, payload.get("targets") or [],
                          payload.get("instruction", ""))
    except ProjectError as e:
        _fail(e)
    checks = svc.checks_of(db, ver.id)
    return {"lyric": S.lyric(ver), "checks": [S.check(c) for c in checks],
            "summary": svc.check_summary(checks)}


@router.post("/projects/{project_id}/lyrics/rollback/{version}")
def rollback(project_id: str, version: int, db: Session = Depends(get_db)) -> dict:
    try:
        ver = svc.rollback(db, project_id, version)
    except ProjectError as e:
        _fail(e)
    return {"lyric": S.lyric(ver)}


@router.post("/lyrics/checks/{check_id}/adopt")
def adopt(check_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    try:
        ver = svc.adopt_suggestion(db, check_id, int(payload.get("index") or 0))
    except ProjectError as e:
        _fail(e)
    checks = svc.checks_of(db, ver.id)
    return {"lyric": S.lyric(ver), "checks": [S.check(c) for c in checks],
            "summary": svc.check_summary(checks)}


@router.post("/lyrics/checks/{check_id}/resolve")
def resolve(check_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        c = svc.resolve_check(db, check_id)
    except ProjectError as e:
        _fail(e)
    return S.check(c)
