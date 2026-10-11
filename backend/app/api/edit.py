"""剪辑、时间线、合成与投放数据路由。"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serialize as S
from ..db import get_db
from .deps import guard_locked
from ..models import Asset, Project, RenderJob, TimelineClip
from ..queue import enqueue
from ..services import edit as svc
from ..services import render as rsvc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api", tags=["edit"], dependencies=[Depends(guard_locked)])


def _fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


@router.get("/render/variants")
def variants() -> list[dict]:
    return rsvc.variant_list()


@router.get("/projects/{project_id}/timeline")
def get_timeline(project_id: str, db: Session = Depends(get_db)) -> dict:
    return svc.timeline_payload(db, project_id)


@router.post("/projects/{project_id}/timeline/auto")
def auto_timeline(project_id: str, payload: dict = Body(default={}),
                  db: Session = Depends(get_db)) -> dict:
    """自动粗剪 / 重新卡点。sync=true 时同步执行，便于联调。"""
    try:
        if payload.get("sync"):
            p = db.get(Project, project_id)
            if p is None:
                _fail(ProjectError("项目不存在"))
            from .. import state_machine as sm

            tl = svc.analyze(db, project_id) if payload.get("analyze_only") else svc.auto_cut(db, project_id)
            if p.status in ("music_review",):
                p.status = "rough_cutting"
            if p.status == "rough_cutting":
                p.status = "edit_review"
                p.status_note = sm.STATES["edit_review"].enter_note
            db.commit()
            return svc.timeline_payload(db, project_id)
        j = enqueue(db, "audio_analyze", queue="gpu", project_id=project_id,
                    ref_id=project_id, name="音频分析与自动粗剪")
    except ProjectError as e:
        _fail(e)
    return {"job_id": j.id, **svc.timeline_payload(db, project_id)}


@router.post("/projects/{project_id}/timeline/resnap")
def resnap(project_id: str, db: Session = Depends(get_db)) -> dict:
    j = enqueue(db, "resnap", queue="gpu", project_id=project_id, ref_id=project_id,
                name="重新自动卡点（保留人工锁定）")
    return {"job_id": j.id}


@router.post("/projects/{project_id}/timeline/align")
def realign(project_id: str, db: Session = Depends(get_db)) -> dict:
    """重做歌词强制对齐（音画同步），对齐完自动按新时间轴重新卡点。

    单独给一个入口的原因：字幕不跟唱时，需要在不走状态机的前提下重算。
    强制对齐要跑一次 ASR（CPU 上 40~90 秒），所以走队列而不是同步返回。
    """
    if db.get(Project, project_id) is None:
        _fail(ProjectError("项目不存在"))
    # 卡点由对齐任务成功后再派发（then_cut）：原来两个任务一起入队，
    # 对齐失败时卡点照跑，拿旧时间轴白白重排一遍
    j1 = enqueue(db, "audio_analyze", queue="gpu", project_id=project_id,
                 ref_id=project_id, name="重做歌词强制对齐", payload={"then_cut": True})
    db.commit()
    return {"job_id": j1.id}


@router.post("/projects/{project_id}/timeline/analyze")
def retry_analyze(project_id: str, estimate: bool = False, db: Session = Depends(get_db)) -> dict:
    """对齐失败后的两个出口：重试强制对齐，或由人明确选择「按字数估算继续」（F3-1）。"""
    p = db.get(Project, project_id)
    if p is None:
        _fail(ProjectError("项目不存在"))
    if p.status != "audio_analyzing":
        _fail(ProjectError("项目不在「音频分析」阶段；已有时间线时请用「重做歌词对齐」"))
    p.status_note = "按字数估算歌词时间轴（人工选择）" if estimate else "重试歌词对齐"
    j = enqueue(db, "audio_analyze", queue="gpu", project_id=project_id, ref_id=project_id,
                name="按字数估算歌词时间轴" if estimate else "重试歌词强制对齐",
                payload={"allow_estimate": bool(estimate)})
    db.commit()
    return {"job_id": j.id}


@router.get("/projects/{project_id}/timeline/clips/{clip_id}/candidates")
def clip_candidates(project_id: str, clip_id: str, top: int = 4,
                    db: Session = Depends(get_db)) -> dict:
    try:
        return {"items": svc.clip_candidates(db, clip_id, top_k=max(1, min(10, top)))}
    except ProjectError as e:
        _fail(e)


@router.put("/projects/{project_id}/timeline/clips/{clip_id}")
def update_clip(project_id: str, clip_id: str, payload: dict = Body(...),
                db: Session = Depends(get_db)) -> dict:
    try:
        out = svc.update_clip(
            db, clip_id,
            asset_id=payload.get("asset_id"),
            start_s=payload.get("start_s"),
            end_s=payload.get("end_s"),
            locked=payload.get("locked"),
        )
    except ProjectError as e:
        _fail(e)
    asset = db.get(Asset, out["asset_id"]) if out.get("asset_id") else None
    c = db.get(TimelineClip, clip_id)
    return S.clip(c, asset)


@router.post("/projects/{project_id}/render")
def render(project_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    variants = payload.get("variants") or list(rsvc.VARIANTS)
    if payload.get("sync"):
        out = []
        for v in variants:
            out.append(S.render_job(rsvc.render_variant(db, project_id, v)))
        return {"mode": "sync", "renders": out}
    j = enqueue(db, "render_all", queue="gpu", project_id=project_id, ref_id=project_id,
                name="多版本合成", payload={"variants": variants})
    return {"mode": "job", "job_id": j.id}


@router.get("/projects/{project_id}/preview")
def get_preview(project_id: str, db: Session = Depends(get_db)) -> dict:
    """F3-7 内部评审预览（720p）：最近一次导出结果 + 是否在导出中。"""
    from ..models import Job

    r = db.execute(select(RenderJob).where(RenderJob.project_id == project_id,
                                           RenderJob.variant == rsvc.PREVIEW_VARIANT)).scalar_one_or_none()
    j = db.execute(select(Job).where(Job.project_id == project_id, Job.type == "render_preview")
                   .order_by(Job.created_at.desc()).limit(1)).scalar_one_or_none()
    return {"render": S.render_job(r) if r else None,
            "job": {"status": j.status, "progress": j.progress, "error": j.error,
                    "result": j.result or {}} if j else None}


@router.post("/projects/{project_id}/preview")
def make_preview(project_id: str, db: Session = Depends(get_db)) -> dict:
    from ..models import Job, Timeline

    p = db.get(Project, project_id)
    if p is None:
        _fail(ProjectError("项目不存在"))
    if db.execute(select(Timeline).where(Timeline.project_id == project_id)).scalar_one_or_none() is None:
        _fail(ProjectError("还没有时间线，先完成粗剪"))
    busy = db.execute(select(Job).where(Job.project_id == project_id, Job.type == "render_preview",
                                        Job.status.in_(("queued", "running")))).scalars().first()
    if busy is not None:
        return {"job_id": busy.id, "duplicate": True}
    j = enqueue(db, "render_preview", queue="gpu", project_id=project_id, ref_id=project_id,
                name="导出 720p 预览")
    db.commit()
    return {"job_id": j.id}


@router.get("/projects/{project_id}/renders")
def renders(project_id: str, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(select(RenderJob).where(RenderJob.project_id == project_id,
                                              RenderJob.variant != rsvc.PREVIEW_VARIANT)).scalars().all()
    order = list(rsvc.VARIANTS)
    rows = sorted(rows, key=lambda r: order.index(r.variant) if r.variant in order else 99)
    return [S.render_job(r) for r in rows]
