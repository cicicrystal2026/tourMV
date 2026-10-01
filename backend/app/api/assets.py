"""素材库路由。"""
from __future__ import annotations

import json

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import serialize as S
from ..db import get_db
from ..models import Asset, AssetTag
from ..queue import enqueue
from ..services import assets as svc
from ..services.projects import ProjectError

router = APIRouter(prefix="/api/assets", tags=["assets"])

MAX_BYTES = 4 * 1024 * 1024 * 1024  # PRD F4-1：单文件 ≤ 4GB
ALLOWED = (".mp4", ".mov", ".m4v", ".mkv", ".webm")


def _fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


def _detail_or_404(db: Session, asset_id: str) -> Asset:
    a = db.get(Asset, asset_id)
    if a is None:
        _fail(ProjectError("素材不存在"))
    return a


@router.get("/export")
def export_csv(db: Session = Depends(get_db)) -> PlainTextResponse:
    return PlainTextResponse(
        svc.export_license_csv(db), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="license_list.csv"'},
    )


@router.get("/facets")
def facets(db: Session = Depends(get_db)) -> dict:
    return {"facets": svc.facets(db), "stats": svc.stats(db), "dims": svc.TAG_DIMS}


@router.post("/seed")
def seed(db: Session = Depends(get_db), overwrite: bool = False) -> dict:
    return svc.seed_demo_assets(db, overwrite=overwrite)


@router.post("/generate-ai")
def generate_ai(payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    """用文生图把目的地做成一批真实景点素材，替换占位色块。

    同步执行（6 张约 1 分钟）：单张文生图约 6 秒，条数已限制在 12 以内。
    """
    dest = (payload.get("destination") or "").strip()
    if not dest:
        raise HTTPException(status_code=400, detail="请提供 destination（目的地）")
    try:
        from ..adapters import image as image_ad
        from ..services import budget

        n = max(1, min(12, int(payload.get("count") or 6)))
        budget.ensure_budget(db, n * image_ad.price(), what=f"{n} 张 AI 素材")
        return svc.generate_ai_assets(
            db,
            destination=dest,
            theme=(payload.get("theme") or "").strip(),
            count=int(payload.get("count") or 6),
            seconds=float(payload.get("seconds") or 8),
            overwrite=bool(payload.get("overwrite")),
        )
    except ProjectError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"{type(e).__name__}: {e}") from e


@router.get("")
def search(q: str = "", filters: str = "", only_usable: bool = False,
           limit: int = 48, db: Session = Depends(get_db)) -> dict:
    try:
        parsed = json.loads(filters) if filters else {}
    except json.JSONDecodeError:
        parsed = {}
    items = svc.search(db, q=q, filters=parsed, only_usable=only_usable, limit=limit)
    return {"items": items, "total": len(items), "query": q, "facets": svc.facets(db)}


@router.post("/upload")
async def upload(
    files: list[UploadFile] = File(...),
    destination: str = Form(""),
    license_status: str = Form("licensed"),
    licensor: str = Form(""),
    license_no: str = Form(""),
    scope: str = Form(""),
    valid_until: str = Form(""),
    portrait: str = Form(""),
    hint: str = Form(""),
    db: Session = Depends(get_db),
) -> dict:
    created, failed = [], []
    for f in files:
        name = f.filename or "asset.mp4"
        if not name.lower().endswith(ALLOWED):
            failed.append({"name": name, "error": f"不支持的格式（仅 {', '.join(ALLOWED)}）"})
            continue
        content = await f.read()
        if len(content) > MAX_BYTES:
            failed.append({"name": name, "error": "文件超过 4GB"})
            continue
        try:
            a = svc.upload(db, name, content, destination=destination,
                           hint=hint, license_status=license_status, licensor=licensor,
                           license_no=license_no, scope=scope, valid_until=valid_until,
                           portrait=portrait)
            created.append({"id": a.id, "name": a.name})
        except Exception as e:  # noqa: BLE001 - 单个文件失败不影响批量
            failed.append({"name": name, "error": str(e)})
    return {"created": created, "failed": failed, "total": len(created)}


@router.get("/{asset_id}")
def detail(asset_id: str, db: Session = Depends(get_db)) -> dict:
    try:
        return svc.detail(db, asset_id)
    except ProjectError as e:
        _fail(e)


@router.put("/{asset_id}")
def update(asset_id: str, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    _detail_or_404(db, asset_id)
    try:
        svc.update(
            db, asset_id,
            name=payload.get("name"), destination=payload.get("destination"),
            usable=payload.get("usable"), license_status=payload.get("license_status"),
            licensor=payload.get("licensor"), license_no=payload.get("license_no"),
            scope=payload.get("scope"), valid_until=payload.get("valid_until"),
            portrait=payload.get("portrait"),
            add_tags=payload.get("add_tags"), del_tags=payload.get("del_tags"),
        )
    except ProjectError as e:
        _fail(e)
    return svc.detail(db, asset_id)


@router.post("/{asset_id}/retag")
def retag(asset_id: str, payload: dict = Body(default={}), db: Session = Depends(get_db)) -> dict:
    _detail_or_404(db, asset_id)
    return svc.tag_asset(db, asset_id, hint=payload.get("hint", ""))


@router.post("/{asset_id}/reembed")
def reembed(asset_id: str, db: Session = Depends(get_db)) -> dict:
    _detail_or_404(db, asset_id)
    svc.embed_asset(db, asset_id)
    return {"ok": True}


@router.delete("/{asset_id}")
def remove(asset_id: str, db: Session = Depends(get_db)) -> dict:
    a = _detail_or_404(db, asset_id)
    for t in list(db.execute(select(AssetTag).where(AssetTag.asset_id == asset_id)).scalars()):
        db.delete(t)
    db.delete(a)
    db.commit()
    return {"deleted": asset_id}


@router.get("/{asset_id}/usage")
def usage(asset_id: str, db: Session = Depends(get_db)) -> list[dict]:
    _detail_or_404(db, asset_id)
    return svc.detail(db, asset_id)["usage"]


@router.post("/{asset_id}/retag-async")
def retag_async(asset_id: str, db: Session = Depends(get_db)) -> dict:
    _detail_or_404(db, asset_id)
    j = enqueue(db, "asset_tag", ref_id=asset_id, name=f"重新打标 {asset_id[:8]}")
    return {"job_id": j.id}
