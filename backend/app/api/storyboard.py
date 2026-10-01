"""逐句配图（分镜）路由。

对应产品里新增的那道人工闸门：一句歌词 = 一张景区图，
出图后由人审核（通过 / 驳回重生成 / 上传自己的图），全部通过才进入卡点合成。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db import get_db
from ..queue import enqueue
from ..services import storyboard as svc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api/projects/{project_id}/storyboard", tags=["storyboard"])

MAX_UPLOAD = 40 * 1024 * 1024  # 上传的是「一张图」，给 40MB 足够，也避免误传视频


def _fail(e: Exception, code: int = 400):
    raise HTTPException(status_code=code, detail=str(e))


@router.get("")
def payload(project_id: str, db: Session = Depends(get_db)) -> dict:
    return svc.payload(db, project_id)


@router.post("/plan")
def plan(project_id: str, force: bool = False, db: Session = Depends(get_db)) -> dict:
    """按歌词逐句提炼真实景区（force=1 时重做，会覆盖现有分镜）。"""
    try:
        return svc.plan(db, project_id, force=force)
    except ProjectError as e:
        _fail(e)
    except Exception as e:  # noqa: BLE001
        _fail(e, 502)


@router.post("/generate")
def generate(project_id: str, provider: str = "", db: Session = Depends(get_db)) -> dict:
    """同步出图（会阻塞到全部出完）。

    适合「只重做几条」；整批建议走 `POST /enqueue`（进队列，有进度、不占请求）。
    """
    try:
        return svc.generate(db, project_id, provider=provider or None)
    except ProjectError as e:
        _fail(e)
    except Exception as e:  # noqa: BLE001
        _fail(e, 502)


@router.post("/enqueue")
def enqueue_generate(project_id: str, provider: str = "",
                     db: Session = Depends(get_db)) -> dict:
    """把「提炼 + 整批出图」丢进队列（推荐，18 句要跑十几到几十分钟）。"""
    j = enqueue(db, "storyboard_generate", queue="gpu", project_id=project_id,
                ref_id=project_id, name="逐句生成景区配图",
                payload={"provider": provider or ""})
    db.commit()
    return {"queued": True, "job_id": j.id, "provider": provider or "default"}


@router.post("/shots/{shot_id}/approve")
def approve(project_id: str, shot_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return svc.approve(db, shot_id)
    except ProjectError as e:
        _fail(e)


@router.post("/shots/{shot_id}/reject")
def reject(project_id: str, shot_id: str, note: str = "",
           db: Session = Depends(get_db)) -> dict:
    try:
        return svc.reject(db, shot_id, note)
    except ProjectError as e:
        _fail(e)


@router.post("/shots/{shot_id}/regenerate")
def regenerate(project_id: str, shot_id: str, prompt: str = "", provider: str = "",
               db: Session = Depends(get_db)) -> dict:
    """驳回重生成一张。走队列 —— 出一张图要 1–2 分钟，不该卡住请求。"""
    if prompt.strip():
        row = db.get(svc.StoryboardShot, shot_id)
        if row is not None:
            row.prompt = prompt.strip()[:1500]
            db.commit()
    j = enqueue(db, "storyboard_generate_one", queue="gpu", project_id=project_id,
                ref_id=shot_id, name="重生成一张配图",
                payload={"provider": provider or ""})
    db.commit()
    return {"queued": True, "job_id": j.id}


@router.post("/shots/{shot_id}/upload")
async def upload(project_id: str, shot_id: str,
                 file: UploadFile = File(...),
                 db: Session = Depends(get_db)) -> dict:
    """用你自己的图替换这一句的配图（视为已通过）。"""
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=400, detail="图片超过 40MB，请先压缩")
    try:
        return svc.upload(db, shot_id, file.filename or "upload", data)
    except ProjectError as e:
        _fail(e)
    except Exception as e:  # noqa: BLE001
        _fail(e, 502)


@router.post("/approve-all")
def approve_all(project_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return svc.approve_all(db, project_id)
    except ProjectError as e:
        _fail(e)


@router.delete("")
def reset(project_id: str, db: Session = Depends(get_db)) -> dict:
    return svc.reset(db, project_id)
