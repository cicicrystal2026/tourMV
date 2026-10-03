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


def is_stale(db: Session, project_id: str) -> bool:
    """分镜是否对应的是旧歌词（逐句比对文本）。"""
    rows = shots(db, project_id)
    if not rows:
        return False
    try:
        cur = [x["text"] for x in _lyric_lines(db, project_id)]
    except ProjectError:
        return False
    return [r.line_text for r in rows] != cur


# ------------------------------------------------------------------ 1) 提炼
def plan(db: Session, project_id: str, *, force: bool = False) -> dict:
    """逐句提炼真实景观。已有分镜且未 force 时不重复调用（省 token）。"""
    p = get_project(db, project_id)
    lines = _lyric_lines(db, project_id)
    exist = shots(db, project_id)
    if exist and not force and not is_stale(db, project_id):
        return {"created": 0, "total": len(exist),
                "note": "已有分镜，未重复生成（需要重做请传 force）"}
    # 歌词改过（驳回重写 / 修订版）时旧分镜作废：否则会拿上一版歌词的画面去卡点
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
    mark_dups(db, project_id)
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
    # 只复用本句自己登记的 AI 资产。上一版若来自素材库（实拍 / 演示素材），
    # row.asset_id 指向的是库里的那条资产，绝不能拿来改写成 AI 画面。
    own = Path(settings().data_dir / "assets" / "storyboard").resolve()
    if a is not None and not (a.source == "ai" and own in Path(a.file_path or "").resolve().parents):
        a = None
    if a is None:
        a = Asset(name=name)
        db.add(a)
    a.name = name
    a.file_path = str(clip)
    a.thumb_path = str(thumb) if thumb.exists() else ""
    a.destination = p.destination or ""
    a.duration_s = round(float(media.probe(clip).get("duration_s") or CLIP_SECONDS), 2)
    a.width, a.height, a.fps = 1920, 1080, 25.0
    a.source = "ai"
    a.license_status = "ai_generated"
    a.licensor = f"AI 生成（{image_ad.PROVIDERS.get(provider, {}).get('label', provider)}）"
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
        image_ad.text_to_image(prompt, raw, provider=provider, project_id=p.id)
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
        image_ad.text_to_image(softened, raw, provider=provider, project_id=p.id)
        note = (f"上游合规检查拦截了「{row.place}」，已自动改用中性描述出图；"
                "如需精确实景建议上传自有图")[:300]
    # 统一转成 JPEG：PNG 一张 1.5MB 起，18 句就是 30MB，白白撑大发布包。
    # 偶发下载不完整会让 ffmpeg 转码失败（E2E 实测），这里整张重出一次再放弃
    for attempt in range(2):
        try:
            if not media.is_valid_image(raw):
                raise RuntimeError("出图不完整（文件损坏）")
            media.to_jpeg(raw, img, quality=90)
            break
        except Exception:  # noqa: BLE001
            if attempt == 1:
                raise RuntimeError("出图文件损坏，重出一次仍失败") from None
            raw.unlink(missing_ok=True)
            image_ad.text_to_image(prompt, raw, provider=provider, project_id=p.id)
    raw.unlink(missing_ok=True)
    if not media.is_valid_image(img):
        raise RuntimeError("转 JPEG 后校验失败")
    media.image_to_clip(img, clip, seconds=CLIP_SECONDS, width=1920, height=1080)
    media.make_thumb_from_image(img, thumb)
    return img, clip, thumb, note


# ------------------------------------------------------------------ 镜头来源与审核减负
# 2026-10-01 产品决定（修订 PRD V1.1 D1）：「AI 先出分镜，有实拍再替换」。
#   · 生图模型就绪：每句（去重后）先出 AI 图 → 视觉模型预审打分 → 运营只看低分的；
#     素材库里有同目的地实拍时，卡片提示「有实拍可替换」，一键换上；
#   · 生图模型未就绪：退回素材库检索（实拍 → 已有 AI 画面 → 演示素材），
#     演示模式再兜底占位画面；生产环境如实标「缺镜头」。
# AI 画面占比 30% 只作提醒（看板 / 审核页显示），不做硬拦截。
SOURCE_LABEL = {
    "library": "素材库实拍", "library_ai": "复用素材库 AI 画面", "library_seed": "演示素材（非实拍）",
    "wanx": "AI 生成·通义万相", "seedream": "AI 生成·豆包 Seedream", "jimeng": "AI 生成·即梦",
    "placeholder": "占位画面（演示模式）", "upload": "用户上传",
    "borrowed": "沿用前句画面（AI 出图失败）",
}
AI_SOURCES = {"wanx", "seedream", "jimeng", "library_ai", "borrowed"}
# 预审分数 ≥ 该值标「建议通过」，人只需抽查；低于的标红、必须人看
SUGGEST_PASS = 75


def _dest_core(destination: str) -> str:
    """「吉林·长白山」→「长白山」：素材库里目的地写法不统一，按核心地名匹配。"""
    return (destination or "").replace("•", "·").split("·")[-1].strip()


def _norm_line(text: str) -> str:
    return "".join(c for c in (text or "") if c.isalnum())


def mark_dups(db: Session, project_id: str) -> int:
    """副歌复唱的句子只审一次：同样的歌词跟随第一次出现的那句（母句）。返回需要审的句数。"""
    first: dict[str, int] = {}
    masters = 0
    for r in shots(db, project_id):
        k = _norm_line(r.line_text)
        if k and k in first:
            r.dup_of = first[k]
        else:
            r.dup_of = 0
            masters += 1
            if k:
                first[k] = r.order
    db.commit()
    return masters


def unique_line_count(db: Session, project_id: str) -> int:
    rows = shots(db, project_id)
    if rows:
        return len([r for r in rows if not r.dup_of]) or len(rows)
    lines = _lyric_lines(db, project_id)
    return len({_norm_line(x["text"]) for x in lines})


def _tokens(row: StoryboardShot) -> list[str]:
    toks = [k.strip() for k in (row.keywords or "").split(",") if k.strip()]
    place = (row.place or "").replace("•", "·")
    toks += [t for t in place.replace("的", "·").split("·") if len(t.strip()) >= 2]
    return list(dict.fromkeys(toks))


def _library_pick(db: Session, p: Project, row: StoryboardShot, used: set[str],
                  tier: str) -> Asset | None:
    """在素材库里给这句找一个镜头。tier: shot（实拍）/ ai（已有 AI 画面）/ seed（演示素材）。"""
    from .assets import tag_map

    core = _dest_core(p.destination)
    if not core:
        return None
    cands = []
    for a in db.execute(select(Asset).where(Asset.usable.is_(True))).scalars():
        if a.id in used or a.license_status == "missing":
            continue
        if a.has_portrait and not (a.portrait or "").strip():
            continue      # 肖像权未登记不进候选（PRD F4 交互规则）
        if a.licensor and a.licensor.startswith("占位画面"):
            continue      # 系统生成的占位画面不能当素材推荐
        kind = "ai" if a.source == "ai" else ("seed" if a.is_seed else "shot")
        if kind != tier:
            continue
        if core not in (a.destination or "") and core not in (a.name or ""):
            continue
        cands.append(a)
    if not cands:
        return None
    tags = tag_map(db, [a.id for a in cands])
    toks = _tokens(row)

    def score(a: Asset) -> int:
        text = (a.name or "") + " " + " ".join(v for vs in tags.get(a.id, {}).values() for v in vs)
        return sum(1 for t in toks if t and t in text)

    return max(cands, key=score)


def _use_library(db: Session, row: StoryboardShot, a: Asset, source: str) -> None:
    """把素材库镜头挂到这句上：抽一帧做审核图，片段直接复用原素材（不复制、不新建资产）。"""
    img, _clip, thumb = _image_paths(row)
    img.parent.mkdir(parents=True, exist_ok=True)
    src = Path(a.file_path or "")
    if src.exists():
        media.make_thumb(src, img, at_s=min(2.0, max(0.5, (a.duration_s or 4) / 3)))
    elif a.thumb_path and Path(a.thumb_path).exists():
        img.write_bytes(Path(a.thumb_path).read_bytes())
    if img.exists():
        media.make_thumb_from_image(img, thumb)
        row.image_path = str(img)
    row.clip_path = a.file_path
    row.asset_id = a.id
    row.provider, row.model = source, a.name[:80]
    row.note = f"{SOURCE_LABEL[source]}：{a.name}（{a.licensor or '未登记授权方'}）"[:300]
    row.error = ""
    row.status = "pending"
    row.ai_score, row.ai_reason = -1, ""
    if row.suggest_asset_id == a.id:
        row.suggest_asset_id = ""


def _placeholder(db: Session, row: StoryboardShot, p: Project) -> None:
    """演示模式的最后兜底：带地名字样的占位画面，界面明确标注，生产环境闸门不放行。"""
    img, clip, thumb = _image_paths(row)
    media.generate_test_clip(clip, seconds=8.0, label=f"{row.place} · 占位")
    media.make_thumb(clip, img, at_s=1.0)
    media.make_thumb_from_image(img, thumb)
    row.image_path, row.clip_path = str(img), str(clip)
    a = _asset_for_shot(db, row, p, img=img, clip=clip, thumb=thumb, provider="placeholder")
    a.source, a.is_seed = "shot", True
    a.license_status, a.licensor, a.license_no = "licensed", "占位画面（演示模式，非真实）", "PLACEHOLDER"
    row.asset_id = a.id
    row.provider, row.model = "placeholder", "ffmpeg"
    row.note = "素材库无匹配镜头、生图模型未配置：已生成占位画面（仅演示模式可用）"
    row.error = ""
    row.status = "pending"
    row.ai_score, row.ai_reason = -1, ""


def _borrow(db: Session, p: Project, row: StoryboardShot, reason: str) -> bool:
    """AI 出不了图时不留空卡（产品决定）：先找素材库，再借用最近一句已有画面。

    借来的画面标「沿用前句画面」、不打预审分（必须人看），随时可以「重新出一张」。
    找不到任何可借的画面时返回 False，保持「需重做」。
    """
    used = {r.asset_id for r in shots(db, p.id) if r.asset_id and r.id != row.id}
    for tier in ["shot", "ai"] + (["seed"] if settings().allow_mock else []):
        a = _library_pick(db, p, row, used, tier)
        if a is not None:
            _use_library(db, row, a, {"shot": "library", "ai": "library_ai", "seed": "library_seed"}[tier])
            row.note = f"AI 出图失败，已从素材库匹配：{a.name}"[:300]
            return True
    donors = [r for r in shots(db, p.id)
              if r.id != row.id and not r.dup_of and r.image_path and r.status != "rejected"
              and Path(r.image_path).exists()]
    if not donors:
        return False
    # 同一段落里最近的前一句优先，其次全曲最近的前一句，再次最近的后一句
    same = [r for r in donors if r.section == row.section and r.order < row.order]
    before = [r for r in donors if r.order < row.order]
    pick = (max(same, key=lambda r: r.order) if same else
            max(before, key=lambda r: r.order) if before else
            min(donors, key=lambda r: r.order))
    row.image_path, row.clip_path, row.asset_id = pick.image_path, pick.clip_path, pick.asset_id
    row.provider, row.model = "borrowed", f"第{pick.order}句"
    row.status = "pending"
    row.ai_score, row.ai_reason, row.suggest_asset_id = -1, "", ""
    row.error = ""
    row.note = f"AI 出图失败（{reason[:60]}），暂用第 {pick.order} 句画面代替；可点「重新出一张」再试"[:300]
    return True


def sync_dups(db: Session, master: StoryboardShot) -> None:
    """母句的画面、状态、预审结果同步给它的复唱句（审一次，全部生效）。"""
    for r in shots(db, master.project_id):
        if r.dup_of == master.order and r.id != master.id:
            r.image_path, r.clip_path, r.asset_id = master.image_path, master.clip_path, master.asset_id
            r.provider, r.model, r.status = master.provider, master.model, master.status
            r.ai_score, r.ai_reason = master.ai_score, master.ai_reason
            r.suggest_asset_id, r.error = master.suggest_asset_id, master.error
            r.note = f"副歌复唱：画面跟随第 {master.order} 句"
            r.place, r.prompt, r.rev = master.place, master.prompt, master.rev


def _judge(rows: list[StoryboardShot], p: Project) -> dict[str, dict | None]:
    """并发跑视觉预审（只读文件、不碰数据库）。"""
    from ..adapters import vlm

    if not vlm.judge_ready() or not rows:
        return {}
    args = [(r.id, r.image_path, r.line_text, r.place) for r in rows if r.image_path]

    def one(a):
        rid, img, line, place = a
        return rid, vlm.judge_image(img, line, place, p.destination or "", project_id=p.id)

    with ThreadPoolExecutor(max_workers=4) as pool:
        return dict(pool.map(one, args))


def _apply_judge(db: Session, rows: list[StoryboardShot], p: Project) -> None:
    res = _judge(rows, p)
    for r in rows:
        j = res.get(r.id)
        if j:
            r.ai_score, r.ai_reason = j["score"], j["reason"]
    db.commit()


def _suggest_real(db: Session, p: Project, masters: list[StoryboardShot]) -> None:
    """给 AI 画面找可替换的素材库实拍（一支片内不重复推荐同一个镜头）。

    演示模式下演示素材也参与推荐（界面会标「演示素材」），让替换流程能被体验到。
    """
    used = {r.asset_id for r in shots(db, p.id) if r.asset_id and r.provider and r.provider.startswith("library")}
    tiers = ["shot"] + (["seed"] if settings().allow_mock else [])
    for r in masters:
        r.suggest_asset_id = ""
        if r.provider not in AI_SOURCES and r.provider != "placeholder":
            continue      # 本来就是实拍 / 上传的不需要推荐
        for tier in tiers:
            a = _library_pick(db, p, r, used, tier)
            if a is not None:
                r.suggest_asset_id = a.id
                used.add(a.id)
                break
    db.commit()


def generate(db: Session, project_id: str, *, shot_ids: list[str] | None = None,
             provider: str | None = None, force: bool = False,
             on_progress=None) -> dict:
    """为每句配画面并预审，返回统计。

    默认只补「还没有图」和「被驳回」的，已有待审图的不会重复花钱；
    要整批重做传 `force=True`。副歌复唱句不单独出图，跟随母句。
    """
    p = get_project(db, project_id)
    provider = (provider or image_ad.default_provider()).strip().lower()
    ai_ready = image_ad.available(provider)
    allow_mock = settings().allow_mock

    rows = shots(db, project_id)
    if not rows or is_stale(db, project_id):
        plan(db, project_id, force=bool(rows))
        rows = shots(db, project_id)
    mark_dups(db, project_id)
    rows = shots(db, project_id)
    if shot_ids:
        want = set(shot_ids)
        rows = [r for r in rows if r.id in want]
    todo = [r for r in (rows if force else [r for r in rows if not r.image_path or r.status == "rejected"])
            if not r.dup_of]
    if not todo:
        return {"ok": 0, "failed": 0, "total": 0, "errors": [], "by_source": {},
                "note": "没有待生成的句子（都已有图）"}

    for r in todo:
        r.status = "generating"
        r.error = ""
        r.ai_score, r.ai_reason = -1, ""
    db.commit()

    by_source: dict[str, int] = {}
    errors: list[dict] = []
    need_ai: list[StoryboardShot] = list(todo)

    if not ai_ready:
        # 生图不可用：退回素材库检索（同一支片内不重复用同一个镜头，PRD F6-6）
        todo_ids = {r.id for r in todo}
        used = {r.asset_id for r in shots(db, project_id)
                if r.asset_id and r.id not in todo_ids and r.status != "rejected"}
        tiers = ["shot", "ai"] + (["seed"] if allow_mock else [])
        need_ai = []
        for r in todo:
            hit = None
            for tier in tiers:
                hit = _library_pick(db, p, r, used, tier)
                if hit is not None:
                    src = {"shot": "library", "ai": "library_ai", "seed": "library_seed"}[tier]
                    _use_library(db, r, hit, src)
                    used.add(hit.id)
                    by_source[src] = by_source.get(src, 0) + 1
                    break
            if hit is None:
                if allow_mock:
                    _placeholder(db, r, p)
                    by_source["placeholder"] = by_source.get("placeholder", 0) + 1
                else:
                    r.status = "rejected"
                    r.error = (f"素材库没有「{_dest_core(p.destination)}」的可用镜头，"
                               f"且生图模型未就绪（{image_ad.note(provider)}）。请上传素材或配置生图模型")
                    errors.append({"order": r.order, "place": r.place, "error": r.error})
        db.commit()

    # AI 先出分镜
    workers = max(1, min(6, settings().image_concurrency))

    def work(row: StoryboardShot):
        try:
            img, clip, thumb, note = _render_one(row, p, provider)
            return row.id, (img, clip, thumb, note), None
        except Exception as e:  # noqa: BLE001
            return row.id, None, f"{type(e).__name__}: {e}"[:300]

    if need_ai:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for n, (rid, res, err) in enumerate(pool.map(work, need_ai), 1):
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
                    row.image_path, row.clip_path = str(img), str(clip)
                    row.provider, row.model = provider, image_ad.model_name(provider)
                    row.error = ""
                    a = _asset_for_shot(db, row, p, img=img, clip=clip, thumb=thumb,
                                        provider=provider)
                    row.asset_id = a.id
                    row.status = "pending"
                    by_source[provider] = by_source.get(provider, 0) + 1
                db.commit()
                if on_progress:
                    on_progress(n, len(need_ai) + 1)

    # AI 出不了图的句子不留空卡：素材库 → 借用前一句画面（产品决定，E2E 反馈）
    for r in todo:
        if r.status == "rejected" and r.error and _borrow(db, p, r, r.error):
            by_source["borrowed"] = by_source.get("borrowed", 0) + 1
            errors = [e for e in errors if e["order"] != r.order]
    db.commit()

    done = [r for r in todo if r.status == "pending"]
    # 机器先审：视觉模型打图文一致度，人只看低分的
    _apply_judge(db, done, p)
    # AI 画面找可替换的实拍
    _suggest_real(db, p, done)
    for r in todo:
        sync_dups(db, r)
    db.commit()
    if on_progress:
        on_progress(1, 1)

    ok = len(done)
    parts = "，".join(f"{SOURCE_LABEL.get(k, k)} {v}" for k, v in by_source.items())
    return {
        "ok": ok, "failed": len(errors), "total": ok + len(errors), "errors": errors,
        "by_source": by_source,
        "provider": provider, "model": image_ad.model_name(provider),
        "note": f"共 {ok + len(errors)} 句：{parts or '无'}" + (f"；失败 {len(errors)}" if errors else ""),
    }


# ------------------------------------------------------------------ 3) 审核
def _master(db: Session, shot_id: str) -> StoryboardShot:
    row = db.get(StoryboardShot, shot_id)
    if row is None:
        raise ProjectError("分镜不存在")
    if row.dup_of:
        # 对复唱句的操作一律作用到母句上，保证同一句歌词只有一个画面
        m = db.execute(select(StoryboardShot).where(
            StoryboardShot.project_id == row.project_id,
            StoryboardShot.order == row.dup_of)).scalar_one_or_none()
        if m is not None:
            return m
    return row


def approve(db: Session, shot_id: str) -> dict:
    row = _master(db, shot_id)
    if not row.image_path:
        raise ProjectError("这条还没有图，先出图再通过")
    row.status = "approved"
    row.error = ""
    sync_dups(db, row)
    db.commit()
    return {"id": row.id, "status": row.status, "status_label": STATUS_LABEL[row.status]}


def approve_suggested(db: Session, project_id: str) -> dict:
    """一键通过所有「建议通过」（预审分 ≥ SUGGEST_PASS）的句子，低分的留给人看。"""
    n = 0
    for r in shots(db, project_id):
        if not r.dup_of and r.status == "pending" and r.ai_score >= SUGGEST_PASS:
            r.status = "approved"
            sync_dups(db, r)
            n += 1
    db.commit()
    return {"approved": n}


def reject(db: Session, shot_id: str, note: str = "") -> dict:
    row = _master(db, shot_id)
    row.status = "rejected"
    row.note = (note or "")[:300]
    sync_dups(db, row)
    db.commit()
    return {"id": row.id, "status": row.status, "status_label": STATUS_LABEL[row.status]}


def use_library(db: Session, shot_id: str, asset_id: str = "") -> dict:
    """换成素材库实拍（默认用系统推荐的那条）。人主动选的，视为已通过。"""
    row = _master(db, shot_id)
    aid = asset_id or row.suggest_asset_id
    a = db.get(Asset, aid) if aid else None
    if a is None:
        raise ProjectError("没有可替换的素材库镜头")
    src = "library_seed" if a.is_seed else ("library_ai" if a.source == "ai" else "library")
    row.rev += 1
    _use_library(db, row, a, src)
    row.status = "approved"
    sync_dups(db, row)
    db.commit()
    return {"id": row.id, "status": row.status, "image_url": _img_url(row)}


def regenerate(db: Session, shot_id: str, *, prompt: str | None = None,
               provider: str | None = None) -> dict:
    """驳回重生成：换一个 rev，产出新文件名（避免同名覆盖 + 缓存取旧图）。"""
    row = _master(db, shot_id)
    p = get_project(db, row.project_id)
    provider = (provider or row.provider or image_ad.default_provider()).strip().lower()
    if provider not in image_ad.PROVIDERS:
        provider = image_ad.default_provider()     # 上一版来自素材库/占位时，重出走默认生图模型
    if not image_ad.available(provider):
        if settings().allow_mock:
            row.rev += 1
            _placeholder(db, row, p)
            sync_dups(db, row)
            db.commit()
            return {"id": row.id, "status": row.status, "rev": row.rev, "image_url": _img_url(row)}
        raise ProjectError(f"{provider} 未就绪：{image_ad.note(provider)}")
    if prompt is not None and prompt.strip():
        row.prompt = prompt.strip()[:1500]
    _write_rev_meta(row)
    row.rev += 1
    row.status = "generating"
    row.error = ""
    db.commit()
    try:
        img, clip, thumb, note = _render_one(row, p, provider)
    except Exception as e:  # noqa: BLE001
        reason = f"{type(e).__name__}: {e}"[:300]
        row.rev -= 1      # 新文件没生成出来，回到上一版的文件名
        if row.image_path and Path(row.image_path).exists():
            # 重出失败：保留上一版画面，不把卡片变成空的
            row.status = "pending"
            row.note = f"重新出图失败，已保留上一版画面（{reason[:60]}）"[:300]
            row.error = ""
        elif _borrow(db, p, row, reason):
            pass
        else:
            row.status = "rejected"
            row.error = reason
        sync_dups(db, row)
        db.commit()
        if row.status == "rejected":
            raise ProjectError(f"重生成失败：{row.error}") from e
        return {"id": row.id, "status": row.status, "rev": row.rev, "image_url": _img_url(row),
                "fallback": True, "note": row.note}
    row.image_path, row.clip_path = str(img), str(clip)
    if note:
        row.note = note
    row.provider, row.model = provider, image_ad.model_name(provider)
    _write_rev_meta(row)
    a = _asset_for_shot(db, row, p, img=img, clip=clip, thumb=thumb, provider=provider)
    row.asset_id = a.id
    row.status = "pending"
    row.error = ""
    row.ai_score, row.ai_reason = -1, ""
    db.commit()
    # 重出的图同样先过机器预审，并重新找可替换的实拍
    _apply_judge(db, [row], p)
    _suggest_real(db, p, [row])
    sync_dups(db, row)
    db.commit()
    return {"id": row.id, "status": row.status, "rev": row.rev, "image_url": _img_url(row)}


def _rev_meta_path(row: StoryboardShot, rev: int) -> Path:
    return settings().data_dir / "storyboard" / f"{_slug(row.place, rev)}.meta.json"


def _write_rev_meta(row: StoryboardShot) -> None:
    """记下某一版画面是哪个模型出的（文件名只带 rev，不带模型）。"""
    import json

    if not row.image_path:
        return
    try:
        _rev_meta_path(row, row.rev).write_text(
            json.dumps({"provider": row.provider or "", "model": row.model or "",
                        "prompt": row.prompt or "", "image_path": row.image_path or "",
                        "clip_path": row.clip_path or "", "asset_id": row.asset_id or "",
                        "note": row.note or ""}, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _read_rev_meta(row: StoryboardShot, rev: int) -> dict:
    import json

    try:
        return json.loads(_rev_meta_path(row, rev).read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _prev_files(row: StoryboardShot) -> tuple[Path, Path, dict] | None:
    """上一版的画面与片段：优先按记录（可能来自素材库），没有记录再按 rev 命名推算。"""
    if row.rev <= 0:
        return None
    meta = _read_rev_meta(row, row.rev - 1)
    if meta.get("image_path") and meta.get("clip_path"):
        img, clip = Path(meta["image_path"]), Path(meta["clip_path"])
    else:
        row.rev -= 1
        img, clip, _ = _image_paths(row)
        row.rev += 1
    if not img.exists() or not clip.exists():
        return None
    return img, clip, meta


def revert(db: Session, shot_id: str) -> dict:
    """用回上一版（换模型重出后觉得不如原来的）：上一版的文件都还在，切回去即可，不花钱。"""
    row = _master(db, shot_id)
    p = get_project(db, row.project_id)
    if row.rev <= 0:
        raise ProjectError("这一句没有上一版")
    prev = _prev_files(row)
    if prev is None:
        raise ProjectError("上一版的画面文件已不存在，无法用回")
    img, clip, meta = prev
    row.rev -= 1
    row.image_path, row.clip_path = str(img), str(clip)
    if meta.get("provider"):
        row.provider, row.model = meta["provider"], meta.get("model", "")
    lib = db.get(Asset, meta["asset_id"]) if meta.get("asset_id") else None
    if row.provider in image_ad.PROVIDERS or lib is None:
        # 上一版是 AI 出图：把本句的 AI 资产指回旧文件（卡点合成用的是资产）
        _, _, thumb = _image_paths(row)
        row.asset_id = _asset_for_shot(db, row, p, img=img, clip=clip, thumb=thumb,
                                       provider=row.provider or "").id
    else:
        row.asset_id = lib.id      # 上一版来自素材库：直接指回那条资产，不改动它
    row.status = "pending"
    row.error = ""
    row.note = (meta.get("note") or "") if row.provider not in image_ad.PROVIDERS else "已用回上一版画面"
    row.ai_score, row.ai_reason = -1, ""
    db.commit()
    if row.provider in image_ad.PROVIDERS:
        _apply_judge(db, [row], p)
    sync_dups(db, row)
    db.commit()
    return {"id": row.id, "status": row.status, "rev": row.rev, "image_url": _img_url(row)}


def upload(db: Session, shot_id: str, filename: str, content: bytes) -> dict:
    """用用户自己提供的图替换（用户对自己上传的图负责，视为已通过）。"""
    row = _master(db, shot_id)
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
    # 用户上传的是自有/已授权实拍图，来源记为实拍；授权细节在素材库补登
    a.source = "shot"
    a.license_status = "licensed"
    a.licensor = "用户上传"
    a.license_no = "UPLOAD"
    row.asset_id = a.id
    row.status = "approved"
    row.note = f"用户上传：{filename}"[:300]
    row.error = ""
    row.ai_score, row.ai_reason, row.suggest_asset_id = -1, "", ""
    sync_dups(db, row)
    db.commit()
    return {"id": row.id, "status": row.status, "image_url": _img_url(row)}


def approve_all(db: Session, project_id: str) -> dict:
    rows = shots(db, project_id)
    n = 0
    for r in rows:
        if r.image_path and r.status in ("pending", "rejected") and not r.dup_of:
            r.status = "approved"
            sync_dups(db, r)
            n += 1
    db.commit()
    return {"approved": n, "total": len(rows)}


# ------------------------------------------------------------------ 4) 查询
def _img_url(row: StoryboardShot) -> str:
    from ..serialize import media_url
    return media_url("storyboard", row.image_path)


def _clip_url(row: StoryboardShot) -> str:
    from ..serialize import media_url
    # 素材库镜头直接复用原文件（data/assets/）；生成的片段落在 data/assets/storyboard/
    if (row.provider or "").startswith("library"):
        return media_url("assets", row.clip_path)
    return media_url("assets/storyboard", row.clip_path)


def _thumb_url(row: StoryboardShot) -> str:
    from ..serialize import media_url
    if not row.image_path:
        return ""
    # 缩略图由 _image_paths 命名成 sb-{slug}.jpg，不能从 clip_path 推
    slug = _slug(row.place, row.rev)
    return media_url("thumbs", str(settings().data_dir / "thumbs" / f"sb-{slug}.jpg"))


def _prev_image_url(row: StoryboardShot) -> str:
    """上一版的图（用于「换模型重出」后和新图并排对比、以及「用回上一版」）。

    重生成写的是新文件名、不覆盖旧文件；上一版若来自素材库则按记录里的路径找。
    """
    from ..serialize import media_url
    prev = _prev_files(row)
    if prev is None:
        return ""
    return media_url("storyboard", str(prev[0]))   # 画面统一落在 data/storyboard（素材库镜头也会抽帧到这里）


def progress(db: Session, project_id: str) -> dict:
    rows = shots(db, project_id)
    by = {k: 0 for k in STATUS_LABEL}
    for r in rows:
        by[r.status] = by.get(r.status, 0) + 1
    done = by.get("approved", 0)
    masters = [r for r in rows if not r.dup_of]
    pend = [r for r in masters if r.status == "pending"]
    with_img = [r for r in rows if r.image_path]
    ai_n = len([r for r in with_img if r.provider in AI_SOURCES])
    return {
        "total": len(rows), "by_status": by, "approved": done,
        "pending": by.get("pending", 0), "rejected": by.get("rejected", 0),
        "all_approved": bool(rows) and done == len(rows),
        # —— 审核减负视角：按母句计数（复唱句只审一次）——
        "unique": len(masters),
        "unique_approved": len([r for r in masters if r.status == "approved"]),
        "need_review": len([r for r in pend if r.ai_score < SUGGEST_PASS]),
        "suggest_pass": len([r for r in pend if r.ai_score >= SUGGEST_PASS]),
        "suggest_pass_threshold": SUGGEST_PASS,
        # AI 画面占比只作提醒（产品决定：不硬拦截），按句计（含复唱句，代表成片里的时长占比）
        "ai_ratio": round(ai_n / len(with_img), 2) if with_img else 0.0,
        "ai_ratio_limit": 0.3,
        "replaceable": len([r for r in masters if r.suggest_asset_id]),
    }


def payload(db: Session, project_id: str) -> dict:
    rows = shots(db, project_id)
    dup_count: dict[int, int] = {}
    for r in rows:
        if r.dup_of:
            dup_count[r.dup_of] = dup_count.get(r.dup_of, 0) + 1
    sug_ids = {r.suggest_asset_id for r in rows if r.suggest_asset_id}
    sug = {a.id: a for a in db.execute(select(Asset).where(Asset.id.in_(sug_ids))).scalars()} if sug_ids else {}
    from ..serialize import media_url

    def suggestion(r: StoryboardShot) -> dict | None:
        a = sug.get(r.suggest_asset_id)
        if a is None:
            return None
        return {"asset_id": a.id, "name": a.name, "licensor": a.licensor, "is_seed": bool(a.is_seed),
                "thumb_url": media_url("thumbs", a.thumb_path) if a.thumb_path else ""}

    from ..models import Job

    regen_ids = {j.ref_id for j in db.execute(select(Job).where(
        Job.project_id == project_id, Job.type == "storyboard_generate_one",
        Job.status.in_(("queued", "running")))).scalars()}
    items = []
    for r in rows:
        items.append({
            # 这一句正在重出：界面上把重出按钮锁住并显示「重出中」，避免连点重复花钱
            "regen_active": r.id in regen_ids,
            "dup_of": r.dup_of, "repeats": dup_count.get(r.order, 0),
            "ai_score": None if r.ai_score < 0 else r.ai_score, "ai_reason": r.ai_reason,
            "ai_verdict": ("" if r.ai_score < 0 else ("pass" if r.ai_score >= SUGGEST_PASS else "review")),
            "suggest": suggestion(r),
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
            "source": r.provider, "source_label": SOURCE_LABEL.get(r.provider or "", ""),
            "updated_at": r.updated_at,
        })
    from ..models import Job

    # 真有出图任务在排队/运行时才算「出图中」。V1.0 把「待生成」也当成出图中，
    # 生图模型没配好时页面永远显示「出图中，自动刷新…」
    job_active = db.execute(select(Job).where(
        Job.project_id == project_id,
        Job.type.in_(("storyboard_generate", "storyboard_generate_one")),
        Job.status.in_(("queued", "running")))).first() is not None
    return {"items": items, "progress": progress(db, project_id),
            "providers": image_ad.list_providers(),
            "provider": image_ad.default_provider(),
            "job_active": job_active,
            # 歌词改过、分镜还是旧的：界面提示重新出图
            "stale": is_stale(db, project_id),
            "allow_mock": settings().allow_mock}


def approved_map(db: Session, project_id: str) -> dict[int, str]:
    """{第几行(从1开始): asset_id} —— 卡点时按行对号入座，不再靠模型猜。"""
    out: dict[int, str] = {}
    for r in shots(db, project_id):
        if r.status == "approved" and r.asset_id:
            out[r.order] = r.asset_id
    return out


def recover_stuck(db: Session) -> int:
    """服务重启后，「生成中」却没有任务在跑的句子不能一直卡着：有图回到待审，没图标需重做。"""
    from ..models import Job

    active = {j.project_id for j in db.execute(select(Job).where(
        Job.type.in_(("storyboard_generate", "storyboard_generate_one")),
        Job.status.in_(("queued", "running")))).scalars()}
    n = 0
    for r in db.execute(select(StoryboardShot).where(StoryboardShot.status == "generating")).scalars():
        if r.project_id in active:
            continue
        if r.image_path and Path(r.image_path).exists():
            r.status = "pending"
        else:
            r.status, r.error = "rejected", "出图任务被中断（服务重启），请重新出图"
        n += 1
    db.commit()
    return n


def reset(db: Session, project_id: str) -> dict:
    for r in shots(db, project_id):
        db.delete(r)
    db.commit()
    return {"deleted": True}
