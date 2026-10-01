"""歌词路由。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import serialize as S
from .. import state_machine as sm
from ..db import get_db
from ..models import LyricCheck
from ..services import lyrics as svc
from ..services.projects import ProjectError, get as get_project

router = APIRouter(prefix="/api", tags=["lyrics"])

# 只有「待确认歌词」阶段能改词。V1.0 在生曲之后仍能改词，
# 结果是候选曲唱的是旧版歌词、字幕却是新版 —— 要改就驳回到这一步。
EDITABLE_STATES = {"lyric_review"}


def _fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


def _require_lyric_stage(db: Session, project_id: str) -> None:
    p = get_project(db, project_id)
    if p.status not in EDITABLE_STATES:
        label = sm.STATES[p.status].label if p.status in sm.STATES else p.status
        raise ProjectError(f"当前阶段「{label}」不能修改歌词；需要改词请先驳回到「待确认歌词」")


def _payload(db: Session, ver) -> dict:
    checks = svc.checks_of(db, ver.id)
    return {"lyric": S.lyric(ver), "checks": [S.check(c) for c in checks],
            "summary": svc.check_summary(checks, ver)}


@router.get("/projects/{project_id}/lyrics")
def get_lyrics(project_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        ver = svc.latest(db, project_id)
    except ProjectError as e:
        _fail(e)
    if ver is None:
        return {"current": None, "checks": [], "summary": svc.check_summary([], None),
                "versions": []}
    checks = svc.checks_of(db, ver.id)
    return {
        "current": S.lyric(ver),
        "checks": [S.check(c) for c in checks],
        "summary": svc.check_summary(checks, ver),
        "versions": [S.lyric(v) for v in svc.versions(db, project_id)],
    }


@router.post("/projects/{project_id}/lyrics/generate")
def generate_lyrics(project_id: str, sync: bool = False, db: Session = Depends(get_db)) -> dict:
    """在「待确认歌词」阶段整首重写（同步）。首次写词请走「确认简报」推进，由后台任务完成。"""
    try:
        _require_lyric_stage(db, project_id)
        ver, real = svc.generate(db, project_id)
    except ProjectError as e:
        _fail(e)
    return {"mode": "sync", "lyric": S.lyric(ver), "llm": real}


@router.post("/projects/{project_id}/lyrics/check")
def recheck(project_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        _require_lyric_stage(db, project_id)
        ver = svc.latest(db, project_id)
        if ver is None:
            raise ProjectError("还没有歌词版本")
        svc.run_checks(db, ver)
    except ProjectError as e:
        _fail(e)
    return _payload(db, ver)


@router.put("/projects/{project_id}/lyrics/{version}")
def save_lyrics(project_id: str, version: int, payload: dict = Body(...),
                db: Session = Depends(get_db)) -> dict:
    """保存编辑 → 生成新版本（不覆盖历史，PRD F2-6）。"""
    sections = payload.get("sections")
    if not isinstance(sections, list):
        _fail(ProjectError("sections 必须是数组"))
    try:
        _require_lyric_stage(db, project_id)
        ver = svc.save_version(db, project_id, sections, source=payload.get("source", "manual"),
                               carry_from=svc.latest(db, project_id))
    except ProjectError as e:
        _fail(e)
    return _payload(db, ver)


@router.post("/projects/{project_id}/lyrics/rewrite")
def rewrite(project_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    try:
        _require_lyric_stage(db, project_id)
        ver = svc.rewrite(db, project_id, payload.get("targets") or [],
                          payload.get("instruction", ""))
    except ProjectError as e:
        _fail(e)
    return _payload(db, ver)


@router.post("/projects/{project_id}/lyrics/rollback/{version}")
def rollback(project_id: str, version: int, db: Session = Depends(get_db)) -> dict:
    try:
        _require_lyric_stage(db, project_id)
        ver = svc.rollback(db, project_id, version)
    except ProjectError as e:
        _fail(e)
    return _payload(db, ver)


def _check_project(db: Session, check_id: str) -> str:
    c = db.get(LyricCheck, check_id)
    if c is None:
        raise ProjectError("检查项不存在")
    return c.project_id


@router.post("/lyrics/checks/{check_id}/adopt")
def adopt(check_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    try:
        _require_lyric_stage(db, _check_project(db, check_id))
        ver = svc.adopt_suggestion(db, check_id, int(payload.get("index") or 0))
    except ProjectError as e:
        _fail(e)
    return _payload(db, ver)


@router.post("/lyrics/checks/{check_id}/keep")
def keep(check_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    """保留原文（PRD V1.1 F2-5：必须填写理由）。"""
    try:
        _require_lyric_stage(db, _check_project(db, check_id))
        c = svc.keep_original(db, check_id, str(payload.get("reason") or ""))
    except ProjectError as e:
        _fail(e)
    return S.check(c)


@router.post("/lyrics/checks/{check_id}/resolve")
def resolve(check_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    """V1.0 兼容入口，等同 /keep，同样要求理由。"""
    return keep(check_id, payload, db)
