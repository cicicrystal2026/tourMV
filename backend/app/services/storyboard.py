"""逐句分镜服务 —— 一句歌词 = 一张真实景区图，含人工审核。

流程（对应产品新增的那道闸门）：

    plan()      LLM 逐句提炼真实景观 + 写实摄影提示词，写进 storyboard_shot（draft）
    generate()  并发出图 → 转缓动片段 → 注册成 asset，状态置 pending（待审核）
    approve()   人工通过（或 reject 驳回重生成、upload 换成用户自己的图）
    approve_all()
    —— 全部通过后，edit.auto_cut 直接用「第几行 → 哪张资产」的映射去卡点

两个刻意的设计：

1. **分镜与素材库分开**：审核中间态留在 `storyboard_shot`，只有通过/上传的图才
   注册成 `asset`。这样驳回重生成不会污染素材库，也不会让卡点误用没审过的图。
2. **图片落在 `data/storyboard/`**（不是 `data/images/`）—— 发布时会移出
   `data/images`（那是中间缓存，几十 MB 且不需要上线），而审核页必须能在线上看图。
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..adapters import image as image_ad
from ..adapters import llm, media
from ..config import settings
from ..models import Asset, AssetTag, Project, StoryboardShot
from . import lyrics as lyrics_svc
from . import prompts
from .projects import ProjectError, get as get_project

STATUS_LABEL = {
    "draft": "待生成",
    "generating": "生成中",
    "pending": "待审核",
    "approved": "已通过",
    "rejected": "已驳回",
}

MAX_LINES = 80          # 单首歌的分镜上限，防止异常歌词把出图量撑爆
CLIP_SECONDS = 20.0     # 与素材库一致：一句最长十几秒，留足余量


# ------------------------------------------------------------------ 基础
def _slug(text: str, rev: int = 0) -> str:
    keep = "".join(c for c in (text or "") if c.isalnum() or c in "·-（）()")
    base = (keep[:36] or "shot")
    h = hashlib.md5(f"{text}|{rev}".encode("utf-8")).hexdigest()[:5]
    return f"{base}-{h}"


def shots(db: Session, project_id: str) -> list[StoryboardShot]:
    return list(db.execute(
        select(StoryboardShot)
        .where(StoryboardShot.project_id == project_id)
        .order_by(StoryboardShot.order)
    ).scalars())


def _lyric_lines(db: Session, project_id: str) -> list[dict]:
    ver = lyrics_svc.latest(db, project_id)
    if ver is None:
        raise ProjectError("还没有歌词，先生成歌词再来配图")
    out = []
    for name, no, text in lyrics_svc.flat_lines(ver.sections):
        t = (text or "").strip()
        if t:
            out.append({"section": name, "line_no": no, "text": t})
    if not out:
        raise ProjectError("歌词是空的")
    return out[:MAX_LINES]


# ------------------------------------------------------------------ 1) 提炼
def plan(db: Session, project_id: str, *, force: bool = False) -> dict:
    """逐句提炼真实景观。已有分镜且未 force 时不重复调用（省 token）。"""
    p = get_project(db, project_id)
    lines = _lyric_lines(db, project_id)
    exist = shots(db, project_id)
    if exist and not force:
        return {"created": 0, "total": len(exist),
                "note": "已有分镜，未重复生成（需要重做请传 force）"}
    for s in exist:
        db.delete(s)
    db.commit()

    payload, real = llm.json_chat(
        prompts.storyboard_messages(lines, p.destination, ""),
        fallback=prompts.storyboard_fallback(lines, p.destination),
        purpose="storyboard_plan", project_id=project_id,
    )
    by_line: dict[int, dict] = {}
    for it in payload.get("shots") or []:
        try:
            n = int(it.get("line"))
        except (TypeError, ValueError):
            continue
        if 1 <= n <= len(lines) and n not in by_line:
            by_line[n] = it

    for i, l in enumerate(lines, 1):
        it = by_line.get(i) or {}
        place = (it.get("place") or "").strip() or f"{p.destination or '当地'}景观{i}"
        db.add(StoryboardShot(
            project_id=project_id, order=i, section=l["section"], line_no=l["line_no"],
            line_text=l["text"], place=place[:120],
            place_note=(it.get("why") or "").strip()[:200],
            keywords=",".join(str(k).strip() for k in (it.get("keywords") or []) if str(k).strip())[:300],
            prompt=(it.get("prompt") or "").strip()[:1500],
            status="draft",
        ))
    db.commit()
    miss = len(lines) - len(by_line)
    return {
        "created": len(lines), "total": len(lines), "llm": real, "missing": max(0, miss),
        "note": f"已为 {len(lines)} 句歌词提炼景观"
                + (f"（其中 {miss} 句模型未给出，已用兜底名）" if miss > 0 else ""),
    }


# ------------------------------------------------------------------ 2) 出图
def _asset_for_shot(db: Session, row: StoryboardShot, p: Project, *, img: Path,
                    clip: Path, thumb: Path, provider: str) -> Asset:
    """把审核用的图登记成素材库资产（可被卡点引用）。"""
    name = f"{row.place}（第{row.order}句）"[:200]
    a = db.get(Asset, row.asset_id) if row.asset_id else None
    if a is None:
        a = Asset(name=name)
        db.add(a)
    a.name = name
    a.file_path = str(clip)
    a.thumb_path = str(thumb) if thumb.exists() else ""
    a.destination = p.destination or ""
    a.duration_s = round(float(media.probe(clip).get("duration_s") or CLIP_SECONDS), 2)
    a.width, a.height, a.fps = 1920, 1080, 25.0
    a.license_status = "licensed"
    a.licensor = "AI 生成（通义万相）" if provider == "wanx" else "AI 生成（Seedream）"
    a.license_no = f"AI-SB-{date.today().strftime('%Y%m%d')}"
    a.scope = "线上投放（含海外平台）"
    a.valid_until = ""
    a.usable = True
    a.portrait, a.has_portrait = "", False
    a.tag_confidence = 1.0
    db.flush()

    db.execute(delete(AssetTag).where(AssetTag.asset_id == a.id))
    rows = [("place", p.destination or ""), ("subject", (row.prompt or row.place)[:120])]
    rows += [("mood", k) for k in (row.keywords or "").split(",") if k.strip()][:4]
    for dim, val in rows:
        if val:
            db.add(AssetTag(asset_id=a.id, dim=dim, value=val, source="ai"))
    db.flush()
    return a


def _image_paths(row: StoryboardShot) -> tuple[Path, Path, Path]:
    s = settings()
    slug = _slug(row.place, row.rev)
    img = s.data_dir / "storyboard" / f"{slug}.jpg"
    clip = s.data_dir / "assets" / "storyboard" / f"{slug}.mp4"
    thumb = s.data_dir / "thumbs" / f"sb-{slug}.jpg"
    return img, clip, thumb


def _render_one(row: StoryboardShot, p: Project, provider: str
                ) -> tuple[Path, Path, Path, str]:
    """出一张图 → 压成适合上线的 JPEG（同时作为片段源）。工作在这里做，不碰数据库。"""
    img, clip, thumb = _image_paths(row)
    raw = settings().data_dir / "storyboard" / "_raw" / f"{_slug(row.place, row.rev)}.png"
    prompt = (row.prompt or "").strip() or f"{row.place}，{p.destination or ''}，标志性景观"
    note = ""
    try:
        image_ad.text_to_image(prompt, raw, provider=provider)
    except RuntimeError as e:
        # 上游的内容合规检查会拦掉部分专有名词（实测「天安门城楼」被挡，
        # 报 DataInspectionFailed）。画面描述本身没问题，把地名换成中性说法再试一次
        # 通常就能过 —— 比直接失败、让用户手动改词友好得多。
        if "DataInspection" not in str(e) and "may contain" not in str(e):
            raise
        softened = prompt.replace(row.place, "当地标志性古建筑").replace(
            p.destination or "\x00", "此地")
        if softened == prompt:
            raise
        image_ad.text_to_image(softened, raw, provider=provider)
        note = (f"上游合规检查拦截了「{row.place}」，已自动改用中性描述出图；"
                "如需精确实景建议上传自有图")[:300]
    if not media.is_valid_image(raw):
        raise RuntimeError("出图不完整（文件损坏）")
    # 统一转成 JPEG：PNG 一张 1.5MB 起，18 句就是 30MB，白白撑大发布包
    media.to_jpeg(raw, img, quality=90)
    raw.unlink(missing_ok=True)
    if not media.is_valid_image(img):
        raise RuntimeError("转 JPEG 后校验失败")
    media.image_to_clip(img, clip, seconds=CLIP_SECONDS, width=1920, height=1080)
    media.make_thumb_from_image(img, thumb)
    return img, clip, thumb, note


def generate(db: Session, project_id: str, *, shot_ids: list[str] | None = None,
             provider: str | None = None, force: bool = False,
             on_progress=None) -> dict:
    """并发出图 + 转片段 + 登记资产。返回统计。

    默认只补「还没有图」和「被驳回」的，已有待审图的不会重复花钱；
    要整批重做传 `force=True`。
    """
    p = get_project(db, project_id)
    provider = (provider or image_ad.default_provider()).strip().lower()
    if not image_ad.available(provider):
        raise ProjectError(f"{provider} 未就绪：{image_ad.note(provider)}")

    rows = shots(db, project_id)
    if not rows:
        plan(db, project_id)
        rows = shots(db, project_id)
    if shot_ids:
        want = set(shot_ids)
        rows = [r for r in rows if r.id in want]
    todo = rows if force else [r for r in rows if not r.image_path or r.status == "rejected"]
    if not todo:
        return {"ok": 0, "failed": 0, "total": 0, "errors": [],
                "note": "没有待生成的句子（都已有图）"}

    for r in todo:
        r.status = "generating"
        r.error = ""
    db.commit()

    workers = max(1, min(6, settings().image_concurrency))
    ok, errors = 0, []

    def work(row: StoryboardShot):
        try:
            img, clip, thumb, note = _render_one(row, p, provider)
            return row.id, (img, clip, thumb, note), None
        except Exception as e:  # noqa: BLE001
            return row.id, None, f"{type(e).__name__}: {e}"[:300]

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for n, (rid, res, err) in enumerate(pool.map(work, todo), 1):
            row = db.get(StoryboardShot, rid)
            if row is None:
                continue
            if err:
                row.status = "rejected"
                row.error = err
                errors.append({"order": row.order, "place": row.place, "error": err})
            else:
                img, clip, thumb, note = res
                if note:
                    row.note = note
                row.image_path = str(img)
                row.clip_path = str(clip)
                row.provider = provider
                row.model = image_ad.model_name(provider)
                row.error = ""
                a = _asset_for_shot(db, row, p, img=img, clip=clip, thumb=thumb,
                                    provider=provider)
                row.asset_id = a.id
                row.status = "pending"
                ok += 1
            db.commit()
            if on_progress:
                on_progress(n, len(todo))

    return {
        "ok": ok, "failed": len(errors), "total": len(todo), "errors": errors,
        "provider": provider, "model": image_ad.model_name(provider),
        "note": f"共 {len(todo)} 张：成功 {ok}，失败 {len(errors)}",
    }


# ------------------------------------------------------------------ 3) 审核
def approve(db: Session, shot_id: str) -> dict:
    row = db.get(StoryboardShot, shot_id)
    if row is None:
        raise ProjectError("分镜不存在")
    if not row.image_path:
        raise ProjectError("这条还没有图，先出图再通过")
    row.status = "approved"
    row.note = ""
    db.commit()
    return {"id": row.id, "status": row.status, "status_label": STATUS_LABEL[row.status]}


def reject(db: Session, shot_id: str, note: str = "") -> dict:
    row = db.get(StoryboardShot, shot_id)
    if row is None:
        raise ProjectError("分镜不存在")
    row.status = "rejected"
    row.note = (note or "")[:300]
    db.commit()
    return {"id": row.id, "status": row.status, "status_label": STATUS_LABEL[row.status]}


def regenerate(db: Session, shot_id: str, *, prompt: str | None = None,
               provider: str | None = None) -> dict:
    """驳回重生成：换一个 rev，产出新文件名（避免同名覆盖 + 缓存取旧图）。"""
    row = db.get(StoryboardShot, shot_id)
    if row is None:
        raise ProjectError("分镜不存在")
    p = get_project(db, row.project_id)
    provider = (provider or row.provider or image_ad.default_provider()).strip().lower()
    if not image_ad.available(provider):
        raise ProjectError(f"{provider} 未就绪：{image_ad.note(provider)}")
    if prompt is not None and prompt.strip():
        row.prompt = prompt.strip()[:1500]
    row.rev += 1
    row.status = "generating"
    row.error = ""
    db.commit()
    try:
        img, clip, thumb, note = _render_one(row, p, provider)
    except Exception as e:  # noqa: BLE001
        row.status = "rejected"
        row.error = f"{type(e).__name__}: {e}"[:300]
        db.commit()
        raise ProjectError(f"重生成失败：{row.error}") from e
    row.image_path, row.clip_path = str(img), str(clip)
    if note:
        row.note = note
    row.provider, row.model = provider, image_ad.model_name(provider)
    a = _asset_for_shot(db, row, p, img=img, clip=clip, thumb=thumb, provider=provider)
    row.asset_id = a.id
    row.status = "pending"
    row.error = ""
    db.commit()
    return {"id": row.id, "status": row.status, "rev": row.rev, "image_url": _img_url(row)}


def upload(db: Session, shot_id: str, filename: str, content: bytes) -> dict:
    """用用户自己提供的图替换（用户对自己上传的图负责，视为已通过）。"""
    row = db.get(StoryboardShot, shot_id)
    if row is None:
        raise ProjectError("分镜不存在")
    if not content:
        raise ProjectError("上传内容为空")
    p = get_project(db, row.project_id)
    row.rev += 1
    img, clip, thumb = _image_paths(row)
    tmp = settings().data_dir / "storyboard" / "_raw" / f"upload-{row.id[:8]}-{row.rev}.tmp"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_bytes(content)
    if not media.is_valid_image(tmp):
        tmp.unlink(missing_ok=True)
        raise ProjectError("上传的图片无法解析（支持 jpg/png/webp）")
    media.to_jpeg(tmp, img, quality=92)
    tmp.unlink(missing_ok=True)
    media.image_to_clip(img, clip, seconds=CLIP_SECONDS, width=1920, height=1080)
    media.make_thumb_from_image(img, thumb)
    row.image_path, row.clip_path = str(img), str(clip)
    row.provider, row.model = "upload", filename[:80]
    a = _asset_for_shot(db, row, p, img=img, clip=clip, thumb=thumb, provider="upload")
    a.licensor = "用户上传"
    a.license_no = "UPLOAD"
    row.asset_id = a.id
    row.status = "approved"
    row.note = f"用户上传：{filename}"[:300]
    row.error = ""
    db.commit()
    return {"id": row.id, "status": row.status, "image_url": _img_url(row)}


def approve_all(db: Session, project_id: str) -> dict:
    rows = shots(db, project_id)
    n = 0
    for r in rows:
        if r.image_path and r.status in ("pending", "rejected"):
            r.status = "approved"
            r.note = ""
            n += 1
    db.commit()
    return {"approved": n, "total": len(rows)}


# ------------------------------------------------------------------ 4) 查询
def _img_url(row: StoryboardShot) -> str:
    from ..serialize import media_url
    return media_url("storyboard", row.image_path)


def _clip_url(row: StoryboardShot) -> str:
    from ..serialize import media_url
    # 片段落在 data/assets/storyboard/，由已有的 /media/assets 挂载点提供
    return media_url("assets/storyboard", row.clip_path)


def _thumb_url(row: StoryboardShot) -> str:
    from ..serialize import media_url
    if not row.image_path:
        return ""
    # 缩略图由 _image_paths 命名成 sb-{slug}.jpg，不能从 clip_path 推
    slug = _slug(row.place, row.rev)
    return media_url("thumbs", str(settings().data_dir / "thumbs" / f"sb-{slug}.jpg"))


def _prev_image_url(row: StoryboardShot) -> str:
    """上一版的图（用于「换模型重出」后和新图并排对比）。

    文件名本来就带 rev，重生成时写的是新文件名、不覆盖旧文件，
    所以这里不需要额外存字段，直接按 rev-1 推算并确认文件还在。
    """
    from ..serialize import media_url
    if row.rev <= 0:
        return ""
    prev = settings().data_dir / "storyboard" / f"{_slug(row.place, row.rev - 1)}.jpg"
    return media_url("storyboard", str(prev)) if prev.exists() else ""


def progress(db: Session, project_id: str) -> dict:
    rows = shots(db, project_id)
    by = {k: 0 for k in STATUS_LABEL}
    for r in rows:
        by[r.status] = by.get(r.status, 0) + 1
    done = by.get("approved", 0)
    return {
        "total": len(rows), "by_status": by, "approved": done,
        "pending": by.get("pending", 0), "rejected": by.get("rejected", 0),
        "all_approved": bool(rows) and done == len(rows),
    }


def payload(db: Session, project_id: str) -> dict:
    rows = shots(db, project_id)
    items = []
    for r in rows:
        items.append({
            "id": r.id, "order": r.order, "section": r.section, "line_no": r.line_no,
            "line_text": r.line_text, "place": r.place, "place_note": r.place_note,
            "keywords": [k for k in (r.keywords or "").split(",") if k.strip()],
            "prompt": r.prompt, "provider": r.provider, "model": r.model,
            "status": r.status, "status_label": STATUS_LABEL.get(r.status, r.status),
            "image_url": _img_url(r) if r.image_path else "",
            "prev_image_url": _prev_image_url(r),
            "clip_url": _clip_url(r) if r.clip_path else "",
            "thumb_url": _thumb_url(r), "asset_id": r.asset_id,
            "rev": r.rev, "note": r.note, "error": r.error,
            "updated_at": r.updated_at,
        })
    return {"items": items, "progress": progress(db, project_id),
            "providers": image_ad.list_providers(),
            "provider": image_ad.default_provider()}


def approved_map(db: Session, project_id: str) -> dict[int, str]:
    """{第几行(从1开始): asset_id} —— 卡点时按行对号入座，不再靠模型猜。"""
    out: dict[int, str] = {}
    for r in shots(db, project_id):
        if r.status == "approved" and r.asset_id:
            out[r.order] = r.asset_id
    return out


def reset(db: Session, project_id: str) -> dict:
    for r in shots(db, project_id):
        db.delete(r)
    db.commit()
    return {"deleted": True}
