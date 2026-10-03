"""逐句配图（分镜）路由。

对应产品里新增的那道人工闸门：一句歌词 = 一张景区图，
出图后由人审核（通过 / 驳回重生成 / 上传自己的图），全部通过才进入卡点合成。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from ..db import get_db
from .deps import guard_locked
from ..queue import enqueue
from ..services import storyboard as svc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api/projects/{project_id}/storyboard", tags=["storyboard"],
                   dependencies=[Depends(guard_locked)])

MAX_UPLOAD = 40 * 1024 * 1024  # 上传的是「一张图」，给 40MB 足够，也避免误传视频


def _fail(e: Exception, code: int = 400):
    raise HTTPException(status_code=code, detail=str(e))


def _image_budget(db: Session, project_id: str, count: int | None, provider: str = "") -> None:
    """出图按张计费：派发前按张数估算，超出当日上限就拒绝（PRD V1.1 §11）。"""
    from ..adapters import image as image_ad
    from ..services import budget

    if count is None:
        # 副歌复唱句不单独出图：按去重后的句数估算
        try:
            count = svc.unique_line_count(db, project_id)
        except ProjectError:
            count = 0
    # 每张图 + 一次视觉预审（约 ¥0.004）
    budget.ensure_budget(db, count * (image_ad.price(provider or None) + 0.004), what=f"{count} 张配图")


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
        _image_budget(db, project_id, None, provider)
        return svc.generate(db, project_id, provider=provider or None)
    except ProjectError as e:
        _fail(e)
    except Exception as e:  # noqa: BLE001
        _fail(e, 502)


@router.post("/enqueue")
def enqueue_generate(project_id: str, provider: str = "",
                     db: Session = Depends(get_db)) -> dict:
    """把「提炼 + 整批出图」丢进队列（推荐，18 句要跑十几到几十分钟）。"""
    try:
        _image_budget(db, project_id, None, provider)
    except ProjectError as e:
        _fail(e)
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
    try:
        _image_budget(db, project_id, 1, provider)
    except ProjectError as e:
        _fail(e)
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


@router.post("/shots/{shot_id}/revert")
def revert(project_id: str, shot_id: str, db: Session = Depends(get_db)) -> dict:
    """用回上一版画面（逐句换模型对比后，留效果最好的那版）。"""
    try:
        return svc.revert(db, shot_id)
    except ProjectError as e:
        _fail(e)


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


@router.post("/approve-suggested")
def approve_suggested(project_id: str, db: Session = Depends(get_db)) -> dict:
    """一键通过机器预审「建议通过」的句子，低分的留给人看。"""
    return svc.approve_suggested(db, project_id)


@router.post("/shots/{shot_id}/use-library")
def use_library(project_id: str, shot_id: str, asset_id: str = "",
                db: Session = Depends(get_db)) -> dict:
    """把这句换成素材库实拍（默认用系统推荐的那条）。"""
    try:
        return svc.use_library(db, shot_id, asset_id)
    except ProjectError as e:
        _fail(e)


@router.post("/approve-all")
def approve_all(project_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return svc.approve_all(db, project_id)
    except ProjectError as e:
        _fail(e)


@router.delete("")
def reset(project_id: str, db: Session = Depends(get_db)) -> dict:
    return svc.reset(db, project_id)
