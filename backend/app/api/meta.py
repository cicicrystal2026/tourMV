"""热歌监控、任务队列与系统信息路由。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serialize as S
from ..config import settings
from ..db import get_db
from ..models import Job, TrendCard, TrendSnapshot
from ..queue import enqueue, registered, requeue
from ..services import trends as tsvc

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/trends/cards")
def cards(week: str | None = None, db: Session = Depends(get_db)) -> dict:
    rows = tsvc.list_cards(db, week)
    return {"weeks": tsvc.weeks(db), "week": week or (tsvc.weeks(db) or [None])[0],
            "items": [S.trend_card(c) for c in rows]}


@router.get("/trends/cards/{card_id}")
def card(card_id: str, db: Session = Depends(get_db)) -> dict:
    c = db.get(TrendCard, card_id)
    if c is None:
        raise HTTPException(status_code=404, detail="素材卡不存在")
    return S.trend_card(c)


@router.get("/trends/status")
def trends_status(db: Session = Depends(get_db)) -> dict:
    st = tsvc.source_status()
    st["snapshot_rows"] = len(db.execute(select(TrendSnapshot)).scalars().all())
    st["card_rows"] = len(db.execute(select(TrendCard)).scalars().all())
    st["week"] = tsvc.iso_week()
    return st


@router.post("/trends/sync")
def trends_sync(payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    payload = payload or {}
    if payload.get("sync"):
        try:
            return tsvc.sync_from_monitor(db, days=int(payload.get("days") or 30),
                                          top=int(payload.get("top") or 10))
        except FileNotFoundError as e:
            seeded = tsvc.seed_demo(db)
            return {"error": str(e), "seeded": seeded}
    j = enqueue(db, "trend_sync", name="同步热歌监控数据",
                payload={"days": int(payload.get("days") or 30), "top": int(payload.get("top") or 10)})
    return {"job_id": j.id}


@router.post("/trends/seed")
def trends_seed(db: Session = Depends(get_db)) -> dict:
    return tsvc.seed_demo(db)


@router.get("/jobs")
def jobs(status: str | None = None, limit: int = 50, db: Session = Depends(get_db)) -> dict:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Job.status == status)
    rows = db.execute(stmt).scalars().all()
    return {"items": [S.job(j) for j in rows], "handlers": registered()}


@router.post("/jobs/{job_id}/retry")
def retry(job_id: str, db: Session = Depends(get_db)) -> dict:
    j = db.get(Job, job_id)
    if j is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return S.job(requeue(db, j))


@router.get("/system/info")
def system_info(db: Session = Depends(get_db)) -> dict:
    s = settings()
    from ..adapters import align as align_ad
    from ..adapters import clip as clip_ad
    from ..adapters import media
    from ..adapters.music import base as mbase

    from ..auth import enabled as auth_enabled
    from ..services import budget

    # PRD V1.1 §9：只返回就绪状态，不返回 project_root / data_dir / db_path 等服务器路径
    return {
        "force_mock": s.force_mock,
        "allow_mock": s.allow_mock,
        "auth_enabled": auth_enabled(),
        "budget": budget.status(db),
        "llm_ready": s.llm_ready,
        "llm_model": s.llm_model,
        "ffmpeg": media.has_ffmpeg(),
        "ffprobe": media.has_ffprobe(),
        "clip_encoder": "chinese-clip" if clip_ad.encoder.is_real else "hash-fallback",
        "align_engine": s.align_engine,
        # 歌词强制对齐的能力与状态（音画同步的关键）
        "align": align_ad.status(),
        "queue_concurrency": {"api": s.api_concurrency, "gpu": s.gpu_concurrency},
        "handlers": registered(),
        "music_models": [
            {"key": a.key, "name": a.name, "run_mode": a.run_mode, "ready": a.ready,
             "effective": mbase.resolve(a.key).key, "capabilities": a.capabilities(),
             "note": a.note()}
            for a in mbase.all_adapters()
        ],
    }


@router.get("/system/align")
def align_status() -> dict:
    """歌词强制对齐状态：能不能用、模型在不在、要不要下载。"""
    from ..adapters import align as align_ad

    return align_ad.status()
