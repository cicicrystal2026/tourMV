"""素材库服务：上传 / 抽帧 / 打标 / 向量化 / 语义检索 / 授权管理。"""
from __future__ import annotations

import csv
import hashlib
import io
import shutil
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..adapters import align as align_ad
from ..adapters import clip as clip_ad, media, vlm
from ..adapters import image as image_ad
from ..adapters import llm
from ..config import settings
from ..models import Asset, AssetTag, TimelineClip
from ..serialize import basename
from . import prompts
from .projects import ProjectError

TAG_DIMS = ["place", "season", "time", "weather", "mood", "camera", "subject"]
CONF_THRESHOLD = 0.7  # PRD F4-2


def _safe_name(name: str) -> str:
    keep = "".join(c for c in name if c.isalnum() or c in "._-（）()　 ")
    return (keep or "asset").strip()[:120]


def _place_slug(place: str) -> str:
    """把景点名压成安全的文件名。

    取前 40 字（够区分「宁远县九嶷山舜帝陵·三分石远眺」这类长名），
    再挂一个短哈希防截断后撞名 —— 两个景点前 40 字相同的情况很少但存在。
    """
    base = "".join(c for c in place if c.isalnum() or c in "·-（）()").strip()[:40] or "place"
    return f"{base}-{hashlib.md5(place.encode('utf-8')).hexdigest()[:5]}"


# ------------------------------------------------------------------ 上传
def upload(db: Session, filename: str, content: bytes, *, destination: str = "",
           hint: str = "", license_status: str = "licensed", licensor: str = "",
           license_no: str = "", scope: str = "", valid_until: str = "",
           portrait: str = "") -> Asset:
    s = settings()
    dest_dir = s.data_dir / "assets"
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = _safe_name(filename)
    path = dest_dir / f"{uuid.uuid4().hex[:8]}_{safe}"
    path.write_bytes(content)

    info = media.probe(path)
    thumb = s.data_dir / "thumbs" / f"{path.stem}.jpg"
    media.make_thumb(path, thumb, at_s=min(1.0, max(0.1, info["duration_s"] / 4)))

    a = Asset(
        name=Path(safe).stem, file_path=str(path), destination=destination,
        duration_s=info["duration_s"], width=info["width"], height=info["height"], fps=info["fps"],
        thumb_path=str(thumb) if thumb.exists() else "",
        license_status=license_status, licensor=licensor, license_no=license_no,
        scope=scope, valid_until=valid_until, portrait=portrait,
        has_portrait=bool(portrait), usable=True,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    tag_asset(db, a.id, hint=hint or safe)
    return a


def tag_asset(db: Session, asset_id: str, hint: str = "") -> dict:
    a = db.get(Asset, asset_id)
    if a is None:
        raise ProjectError("素材不存在")
    frames = []
    if a.file_path and Path(a.file_path).exists():
        tmp = settings().data_dir / "thumbs" / f"_frame_{a.id[:8]}.jpg"
        for t in (0.5, 2.0, 4.0):
            p = tmp.with_name(f"_frame_{a.id[:8]}_{t}.jpg")
            got = media.make_thumb(a.file_path, p, at_s=t)
            if got:
                frames.append(got)
    result = vlm.tag_frames(frames, hint=hint or a.name)
    conf = float(result.get("confidence") or 0.0)

    db.query(AssetTag).filter(AssetTag.asset_id == a.id).delete()
    for dim in TAG_DIMS:
        for val in (result.get(dim) or [])[:3]:
            db.add(AssetTag(asset_id=a.id, dim=dim, value=str(val), source="ai"))
    a.tag_confidence = conf
    db.commit()
    embed_asset(db, a.id)
    return {"asset_id": a.id, "tags": {d: result.get(d, []) for d in TAG_DIMS},
            "confidence": conf, "need_review": conf < CONF_THRESHOLD,
            "has_identifiable_person": bool(result.get("has_identifiable_person")),
            "engine": result.get("engine", "")}


def embed_asset(db: Session, asset_id: str) -> None:
    a = db.get(Asset, asset_id)
    if a is None:
        return
    tags = [t.value for t in db.execute(select(AssetTag).where(AssetTag.asset_id == a.id)).scalars()]
    text = " ".join([a.name, a.destination, *tags])
    a.embedding = clip_ad.encoder.encode_image(a.file_path or a.name, extra_text=text)
    db.commit()


# ------------------------------------------------------------------ 检索
def tag_map(db: Session, asset_ids: list[str]) -> dict[str, dict[str, list[str]]]:
    out: dict[str, dict[str, list[str]]] = {}
    if not asset_ids:
        return out
    for t in db.execute(select(AssetTag).where(AssetTag.asset_id.in_(asset_ids))).scalars():
        out.setdefault(t.asset_id, {}).setdefault(t.dim, []).append(t.value)
    return out


def search(db: Session, *, q: str = "", filters: dict[str, list[str]] | None = None,
           only_usable: bool = False, limit: int = 48) -> list[dict]:
    rows = list(db.execute(select(Asset)).scalars())
    if only_usable:
        rows = [a for a in rows if a.usable]
    filters = filters or {}
    tmap = tag_map(db, [a.id for a in rows])

    def match(a: Asset) -> bool:
        tags = tmap.get(a.id, {})
        for dim, values in filters.items():
            if not values:
                continue
            if dim == "license_status":
                if a.license_status not in values:
                    return False
            elif dim == "usable":
                if str(a.usable).lower() not in [v.lower() for v in values]:
                    return False
            elif dim == "destination":
                if a.destination not in values:
                    return False
            else:
                have = set(tags.get(dim, []))
                if not have & set(values):
                    return False
        return True

    rows = [a for a in rows if match(a)]

    scores: dict[str, float] = {}
    if q.strip():
        order = clip_ad.rank(q, [(a.id, a.embedding or []) for a in rows], top_k=limit)
        scores = dict(order)
        rows = [a for a in rows if a.id in scores]
        rows.sort(key=lambda a: scores.get(a.id, 0), reverse=True)
    else:
        rows.sort(key=lambda a: a.created_at, reverse=True)
        rows = rows[:limit]

    out = []
    for a in rows:
        out.append({
            "id": a.id, "name": a.name,
            "thumb_url": f"/media/thumbs/{basename(a.thumb_path)}" if a.thumb_path else "",
            "video_url": f"/media/assets/{basename(a.file_path)}" if a.file_path else "",
            "destination": a.destination, "duration_s": a.duration_s,
            "width": a.width, "height": a.height,
            "license_status": a.license_status, "licensor": a.licensor,
            "valid_until": a.valid_until, "usable": a.usable,
            "tag_confidence": a.tag_confidence,
            "need_review": a.tag_confidence < CONF_THRESHOLD,
            "has_portrait": a.has_portrait,
            "tags": tmap.get(a.id, {}),
            "score": round(scores.get(a.id, 0.0), 4),
        })
    return out


def facets(db: Session) -> dict:
    rows = list(db.execute(select(Asset)).scalars())
    tmap = tag_map(db, [a.id for a in rows])
    facets: dict[str, list[str]] = {d: [] for d in TAG_DIMS}
    for tags in tmap.values():
        for dim, vals in tags.items():
            if dim in facets:
                facets[dim] = sorted(set(facets[dim]) | set(vals))
    statuses = sorted({a.license_status for a in rows})
    dests = sorted({a.destination for a in rows if a.destination})
    return {"tags": facets, "license_status": statuses, "destination": dests}


# ------------------------------------------------------------------ 编辑
def update(db: Session, asset_id: str, *, name: str | None = None, destination: str | None = None,
           usable: bool | None = None, license_status: str | None = None,
           licensor: str | None = None, license_no: str | None = None, scope: str | None = None,
           valid_until: str | None = None, portrait: str | None = None,
           add_tags: list[dict] | None = None, del_tags: list[dict] | None = None) -> Asset:
    a = db.get(Asset, asset_id)
    if a is None:
        raise ProjectError("素材不存在")
    for field, value in (("name", name), ("destination", destination), ("usable", usable),
                         ("license_status", license_status), ("licensor", licensor),
                         ("license_no", license_no), ("scope", scope),
                         ("valid_until", valid_until), ("portrait", portrait)):
        if value is not None:
            setattr(a, field, value)
    if portrait is not None:
        a.has_portrait = bool(portrait)
    db.commit()

    if del_tags:
        for t in list(db.execute(select(AssetTag).where(AssetTag.asset_id == a.id)).scalars()):
            if {"dim": t.dim, "value": t.value} in del_tags:
                db.delete(t)
        db.commit()
    if add_tags:
        for t in add_tags:
            db.add(AssetTag(asset_id=a.id, dim=t.get("dim", ""), value=t.get("value", ""), source="manual"))
        db.commit()
    if del_tags or add_tags:
        embed_asset(db, a.id)  # 标签变更后重新向量化（PRD F4-6）
    return a


def detail(db: Session, asset_id: str) -> dict:
    a = db.get(Asset, asset_id)
    if a is None:
        raise ProjectError("素材不存在")
    tmap = tag_map(db, [a.id]).get(a.id, {})
    usage = list(db.execute(select(TimelineClip).where(TimelineClip.asset_id == a.id)).scalars())
    return {
        "id": a.id, "name": a.name, "destination": a.destination,
        # 只给文件名：完整路径是服务器目录结构（PRD V1.1 §11）
        "file_path": basename(a.file_path),
        "source": a.source, "is_seed": a.is_seed,
        "thumb_url": f"/media/thumbs/{basename(a.thumb_path)}" if a.thumb_path else "",
        "video_url": f"/media/assets/{basename(a.file_path)}" if a.file_path else "",
        "duration_s": a.duration_s, "width": a.width, "height": a.height, "fps": a.fps,
        "license_status": a.license_status, "licensor": a.licensor, "license_no": a.license_no,
        "scope": a.scope, "valid_until": a.valid_until, "portrait": a.portrait,
        "has_portrait": a.has_portrait, "usable": a.usable,
        "tag_confidence": a.tag_confidence, "need_review": a.tag_confidence < CONF_THRESHOLD,
        "tags": tmap,
        "usage": [{"project_id": c.project_id, "clip_id": c.id, "lyric_line": c.lyric_line}
                  for c in usage],
        "expiring_soon": _expiring_soon(a.valid_until),
    }


def _expiring_soon(valid_until: str) -> bool:
    if not valid_until:
        return False
    try:
        d = datetime.strptime(valid_until[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return date.today() <= d <= date.today() + timedelta(days=30)


def stats(db: Session) -> dict:
    rows = list(db.execute(select(Asset)).scalars())
    shot = [a for a in rows if a.source != "ai"]
    # 「已授权」只统计实拍素材：AI 生成的画面没有第三方授权可言（PRD V1.1 F4-10）
    licensed = len([a for a in shot if a.license_status == "licensed"])
    expiring = len([a for a in shot if _expiring_soon(a.valid_until)])
    missing = len([a for a in shot if a.license_status == "missing"])
    review = len([a for a in rows if a.tag_confidence < CONF_THRESHOLD])
    return {"total": len(rows), "licensed": licensed, "expiring": expiring,
            "missing": missing, "unusable": len([a for a in rows if not a.usable]),
            "need_review": review, "shot": len(shot), "ai": len(rows) - len(shot),
            "seed": len([a for a in rows if a.is_seed])}


def export_license_csv(db: Session) -> str:
    rows = list(db.execute(select(Asset)).scalars())
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["素材名", "文件", "来源", "目的地", "授权状态", "授权方", "授权编号",
                "授权范围", "有效期", "肖像权", "可用", "使用项目数"])
    for a in rows:
        n = len(list(db.execute(select(TimelineClip).where(TimelineClip.asset_id == a.id)).scalars()))
        # 只导出相对路径：绝对路径会泄露服务器目录结构（PRD V1.1 F4-9）
        p = (a.file_path or "").replace("\\", "/")
        rel = p.split("/data/", 1)[1] if "/data/" in p else p.rsplit("/", 1)[-1]
        w.writerow([a.name, rel, "AI 生成" if a.source == "ai" else ("演示" if a.is_seed else "实拍"),
                    a.destination, a.license_status, a.licensor,
                    a.license_no, a.scope, a.valid_until, a.portrait,
                    "是" if a.usable else "否", n])
    return buf.getvalue()


# ------------------------------------------------------------------ 示例素材
DEMO_ASSETS = [
    ("黄昏_古塔_慢推_秋_晴.mp4", "杭州·雷峰塔", 8.0, "杭州文旅", "夕阳 古塔 慢推 秋 晴"),
    ("清晨_江河_航拍_雾_宁静.mp4", "桂林·漓江", 9.0, "桂林文旅", "清晨 江河 航拍 雾 宁静"),
    ("夜晚_城市天际线_延时_热闹.mp4", "上海·外滩", 7.0, "自有素材", "夜晚 城市天际线 延时 热闹"),
    ("古镇_老街_手持_雨_怀旧.mp4", "苏州·平江路", 8.5, "苏州文旅", "古镇 老街 手持 雨 怀旧"),
    ("草原_航拍_夏_壮阔_无人.mp4", "呼伦贝尔", 10.0, "内蒙古文旅", "草原 航拍 夏 壮阔"),
    ("雪原_延时_冬_苍凉_无人.mp4", "吉林·长白山", 8.0, "长白山文旅", "雪原 延时 冬 苍凉"),
    ("梯田_清晨_航拍_春_治愈.mp4", "云南·元阳", 9.0, "云南文旅", "梯田 清晨 航拍 春 治愈"),
    ("寺院_慢摇_秋_神秘.mp4", "山西·五台山", 7.5, "五台山文旅", "寺院 慢摇 秋 神秘"),
    ("花木_慢推_春_治愈.mp4", "武汉·东湖", 6.5, "东湖文旅", "花木 慢推 春 治愈"),
    ("海岸_环绕_夏_晴_壮阔.mp4", "青岛·崂山", 9.5, "青岛文旅", "海岸 环绕 夏 晴 壮阔"),
    ("村落_固定_清晨_雾.mp4", "安徽·宏村", 7.0, "黄山文旅", "村落 固定 清晨 雾"),
    ("大桥_夜晚_延时_神秘.mp4", "重庆·千厮门", 8.0, "重庆文旅", "大桥 夜晚 延时 神秘"),
]
# 种子素材名（含下面那条肖像权验证用的素材）。它们是 ffmpeg 合成的占位镜头，
# 授权方与编号也是虚构的，迁移时据此打 is_seed，避免被当成真实授权素材进入样片。
DEMO_ASSET_NAMES = {Path(f).stem for f, *_ in DEMO_ASSETS} | {"人物_街景_手持_晴"}


def seed_demo_assets(db: Session, overwrite: bool = False) -> dict:
    if overwrite:
        for a in list(db.execute(select(Asset)).scalars()):
            db.delete(a)
        db.commit()
    if db.execute(select(Asset).limit(1)).scalar_one_or_none() is not None:
        return {"created": 0, "note": "素材库已有内容，跳过"}
    made = 0
    for i, (filename, dest, secs, licensor, hint) in enumerate(DEMO_ASSETS):
        path = settings().data_dir / "assets" / filename
        if not path.exists():
            media.generate_test_clip(path, seconds=secs, width=1920, height=1080, label=hint)
        a = Asset(
            name=Path(filename).stem, file_path=str(path), destination=dest,
            duration_s=secs, width=1920, height=1080, fps=25.0,
            license_status="expiring" if i % 5 == 3 else "licensed",
            licensor=licensor, license_no=f"WL-2026-{i + 1:04d}",
            scope="线上投放（含海外平台）",
            valid_until=(date.today() + timedelta(days=20 if i % 5 == 3 else 900)).isoformat(),
            portrait="", has_portrait=False, usable=True, is_seed=True,
        )
        db.add(a)
        db.commit()
        db.refresh(a)
        media.make_thumb(path, settings().data_dir / "thumbs" / f"{Path(filename).stem}.jpg", at_s=1.0)
        a.thumb_path = str(settings().data_dir / "thumbs" / f"{Path(filename).stem}.jpg")
        db.commit()
        tag_asset(db, a.id, hint=hint)
        made += 1
    # 一条肖像权未登记的素材，用于验证「不进入镜头候选」规则
    p = settings().data_dir / "assets" / "人物_街景_手持_晴.mp4"
    if not p.exists():
        media.generate_test_clip(p, seconds=6.0, width=1920, height=1080, label="人物 街景 手持")
    a = Asset(name="人物_街景_手持_晴", file_path=str(p), destination="成都·宽窄巷子",
              duration_s=6.0, width=1920, height=1080, fps=25.0, license_status="licensed",
              licensor="成都文旅", license_no="WL-2026-0999", scope="线上投放",
              valid_until=(date.today() + timedelta(days=400)).isoformat(),
              portrait="", has_portrait=True, usable=True, is_seed=True)
    db.add(a)
    db.commit()
    # 缩略图要真的抽出来并回写：这条不能只写 thumb_path 常量，
    # 否则素材库卡片是空白的（之前就是这样漏掉的）。
    thumb = settings().data_dir / "thumbs" / "人物_街景_手持_晴.jpg"
    media.make_thumb(p, thumb, at_s=1.0)
    a.thumb_path = str(thumb) if thumb.exists() else ""
    db.commit()
    return {"created": made + 1}


# ------------------------------------------------------- 文生图生成真实素材
def generate_ai_assets(db: Session, *, destination: str, theme: str = "",
                       count: int = 6, seconds: float = 20.0,
                       overwrite: bool = False, on_progress=None) -> dict:
    """按目的地用文生图生成一批真实景点画面，转成缓动视频片段后注册进素材库。

    流程：LLM 拆景点（真实可考）→ 逐张文生图 → 图转缓慢推拉视频 → 入库并打标。

    为什么转成视频而不是直接用图片：合成管线按视频片段剪切素材
    （render.py 用 -ss/-t 截取），静图进去会是一张不动的画面。
    """
    if not destination.strip():
        raise ProjectError("请先填写目的地")
    if not image_ad.available():
        raise ProjectError("文生图未就绪：需要 LLM_API_KEY（DashScope）且 FORCE_MOCK=0")

    count = max(1, min(12, int(count or 6)))

    payload, real = llm.json_chat(
        prompts.image_prompts_messages(destination, theme, count),
        fallback=prompts.image_prompts_fallback(destination, count),
        purpose="image_prompts",
    )
    shots = [s for s in (payload.get("shots") or []) if (s.get("prompt") or "").strip()][:count]
    if not shots:
        raise ProjectError("未能生成景点清单，请重试")

    made: list[dict] = []
    failed: list[dict] = []
    for i, shot in enumerate(shots, 1):
        place = (shot.get("place") or f"{destination}·{i}").strip()
        prompt = shot["prompt"].strip()
        keywords = [str(k).strip() for k in (shot.get("keywords") or []) if str(k).strip()]
        if on_progress:
            on_progress(i, len(shots), place)
        # 缓存键按「景点名」而不是「第几张」：
        # 用序号命名时，只要 LLM 换一次景点清单/顺序，旧图就会被当成新景点的产物复用，
        # 出现「文件名叫九嶷山、画面是女书」这种张冠李戴。
        # 按景点名命名后，同名景点天然命中缓存（不重复花钱），换了景点也只新增文件。
        slug = _place_slug(place)
        try:
            img = settings().data_dir / "images" / f"{slug}.png"
            clip = settings().data_dir / "assets" / f"{slug}.mp4"
            # 图片已生成过就复用，不重复调用文生图（省钱省时；重跑只做转视频那一步）。
            # 但必须验证图片完整 —— 中断的下载会留下损坏 PNG，ffmpeg 读它会卡死。
            if not media.is_valid_image(img):
                image_ad.text_to_image(prompt, img)
                if not media.is_valid_image(img):
                    raise RuntimeError("文生图产出的图片不完整，已跳过该景点")
            media.image_to_clip(img, clip, seconds=seconds, width=1920, height=1080)
            thumb = settings().data_dir / "thumbs" / f"{slug}.jpg"
            media.make_thumb_from_image(img, thumb)
            a = _upsert_ai_asset(
                db, name=place, clip=str(clip), thumb=str(thumb),
                destination=destination, seconds=seconds,
                prompt=prompt, keywords=keywords, overwrite=overwrite,
            )
            db.commit()
            made.append({"id": a.id, "name": place, "clip": str(clip)})
        except Exception as e:  # noqa: BLE001
            db.rollback()
            failed.append({"place": place, "error": f"{type(e).__name__}: {e}"[:200]})

    return {
        "created": len(made), "failed": len(failed),
        "items": made, "errors": failed, "llm": real,
        "note": f"共 {len(shots)} 个景点：成功 {len(made)}，失败 {len(failed)}",
    }


def _upsert_ai_asset(db: Session, *, name: str, clip: str, thumb: str,
                     destination: str, seconds: float, prompt: str,
                     keywords: list[str], overwrite: bool) -> Asset:
    """写入/更新一条 AI 生成素材，并把 prompt 落到标签上（匹配时要用）。"""
    # 缓存键已改为按「景点名」生成（见 generate_ai_assets），所以这里优先按 name 去重：
    # 同一景点重复生成时直接复用原记录，不会堆积僵尸行。
    # 再兜底按产物路径查一次（兼容早期按序号命名的历史数据）。
    exist = db.execute(select(Asset).where(Asset.name == name)).scalar_one_or_none()
    if exist is None:
        exist = db.execute(select(Asset).where(Asset.file_path == clip)).scalar_one_or_none()
    if exist is not None and not overwrite:
        # 片段文件可能被重新生成过（例如把 8 秒延长到 20 秒），
        # 时长必须按**实际文件**刷新 —— auto_cut 是按这个值算 in/out 点的，
        # 记小了会让每段都被截短，累积起来成片比音频短一大截。
        real = float(media.probe(Path(clip)).get("duration_s") or 0)
        if real and abs(real - float(exist.duration_s or 0)) > 0.2:
            exist.duration_s = round(real, 2)
            db.commit()
        return exist

    a = exist or Asset(name=name)
    a.file_path = clip
    a.thumb_path = thumb if Path(thumb).exists() else ""
    a.destination = destination
    a.duration_s = round(float(media.probe(Path(clip)).get("duration_s") or seconds), 2)
    a.width, a.height, a.fps = 1920, 1080, 25.0
    # AI 生成产物没有第三方授权，单列来源与授权口径，不计入「已授权」（PRD V1.1 F4-10）
    a.source = "ai"
    a.license_status = "ai_generated"
    a.licensor = "AI 生成（通义万相）"
    a.license_no = f"AI-{date.today().strftime('%Y%m%d')}"
    a.scope = "线上投放（含海外平台）"
    a.valid_until = ""
    a.usable = True
    a.portrait, a.has_portrait = "", False
    a.tag_confidence = 1.0
    if exist is None:
        db.add(a)
    db.flush()

    db.execute(delete(AssetTag).where(AssetTag.asset_id == a.id))
    rows = [("place", destination), ("subject", prompt[:120])]
    rows += [("mood", k) for k in keywords[:4]]
    for dim, val in rows:
        if val:
            db.add(AssetTag(asset_id=a.id, dim=dim, value=val, source="ai"))
    db.flush()
    return a


def asset_descriptions(db: Session, limit: int = 200) -> list[dict]:
    """给匹配用：把每条可用素材压成一句可比较的描述。

    描述 = 名称 + 目的地 + prompt/关键词标签。
    """
    assets = db.execute(select(Asset).where(Asset.usable.is_(True))).scalars().all()
    tags = tag_map(db, [a.id for a in assets])
    out = []
    for a in assets[:limit]:
        t = tags.get(a.id, {})
        parts = [a.name, a.destination or ""]
        for dim in ("subject", "place", "mood", "time", "weather", "camera", "season"):
            parts += (t.get(dim) or [])
        desc = " ".join(x for x in parts if x).strip()
        out.append({"id": a.id, "name": a.name, "desc": desc[:200]})
    return out
