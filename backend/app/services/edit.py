"""剪辑服务：音频分析、自动粗剪、镜头候选、人工精剪。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import align, clip as clip_ad, llm, media
from ..adapters.clip import cosine, encoder
from ..models import Asset, Project, Timeline, TimelineClip
from ..serialize import basename
from ..services.projects import ProjectError, get as get_project
from . import prompts
from .music import winner_of

SCENE_BATCH = 12


# ------------------------------------------------------------------ 音频分析
def analyze(db: Session, project_id: str) -> Timeline:
    p = get_project(db, project_id)
    w = winner_of(db, p.id)
    if w is None:
        raise ProjectError("还没有选定曲目，无法做音频分析")
    if not w.audio_path:
        raise ProjectError("选定曲目没有音频文件")

    beats = align.detect_beats(w.audio_path, fallback_bpm=w.bpm or 84.0)
    from .lyrics import latest

    ver = latest(db, p.id)
    flat = [(s.get("name", ""), ln) for s in (ver.sections if ver else [])
            for ln in (s.get("lines") or [])]
    lines = [ln for _n, ln in flat]
    duration = float(w.duration_s or 0) or media.probe(w.audio_path).get("duration_s") or 180.0
    aligned = align.align_lyrics(w.audio_path, lines, duration,
                                 strong_beats=beats.get("strong_beats"), bpm=beats.get("bpm", 84.0))
    # 回填段落名：短版切片需要定位副歌
    for i, item in enumerate(aligned.get("lines", [])):
        if i < len(flat):
            item["section"] = flat[i][0]

    tl = db.execute(select(Timeline).where(Timeline.project_id == p.id)).scalar_one_or_none()
    if tl is None:
        tl = Timeline(project_id=p.id)
        db.add(tl)

    # 防回归：如果这次只拿到了「按字数估算」，而库里已经是「强制对齐」的结果，
    # 就**保留旧时间轴**，不要用更差的覆盖更好的。
    # 触发场景很实际：线上沙箱没带 ASR 模型、首次下载又失败，
    # 此时点一次「重做歌词对齐」就会把好数据冲掉。
    was = (tl.align_meta or {}).get("engine")
    if aligned.get("engine") == "analytic" and was == "whisper" and tl.lyric_lines:
        return tl

    tl.audio_path = w.audio_path
    tl.bpm = beats.get("bpm", 0.0)
    tl.beats = beats.get("beats", [])
    tl.duration_s = duration
    tl.lyric_lines = aligned.get("lines", [])
    # 记下这条时间轴是怎么来的 —— 界面要如实告诉用户
    # 「时间是唱出来的」还是「按字数估的」，否则音画飘了也查不出原因。
    tl.align_meta = {
        "engine": aligned.get("engine", "unknown"),
        "note": aligned.get("note", ""),
        "match_ratio": aligned.get("match_ratio"),
        "anchored": aligned.get("anchored"),
        "vocal_span": aligned.get("vocal_span"),
        "dropped_segments": aligned.get("dropped_segments"),
        "beat_engine": beats.get("engine"),
    }
    db.commit()
    db.refresh(tl)
    return tl


# ------------------------------------------------------------------ 自动粗剪
def _usable_assets(db: Session) -> list[Asset]:
    rows = db.execute(select(Asset).where(Asset.usable.is_(True))).scalars().all()
    out = []
    for a in rows:
        if a.license_status == "missing":
            continue
        if a.has_portrait and not (a.portrait or "").strip():  # PRD §6.4 交互规则
            continue
        out.append(a)
    return out


def _scene_descriptions(lines: list[str], destination: str, genre: str, project_id: str) -> list[str]:
    out: list[str] = []
    for i in range(0, len(lines), SCENE_BATCH):
        chunk = lines[i:i + SCENE_BATCH]
        payload, real = llm.json_chat(
            prompts.scene_messages(chunk, destination, genre),
            fallback={"scenes": [{"line": n, "desc": prompts.scene_fallback(ln, destination)}
                                 for n, ln in enumerate(chunk, 1)]},
            purpose="scene_describe", project_id=project_id,
        )
        scenes = payload.get("scenes") or []
        by_line = {int(s.get("line", 0)): s.get("desc", "") for s in scenes}
        for n, ln in enumerate(chunk, 1):
            out.append(by_line.get(n) or prompts.scene_fallback(ln, destination))
    return out


def auto_cut(db: Session, project_id: str, keep_manual: bool = True) -> Timeline:
    p = get_project(db, project_id)
    tl = db.execute(select(Timeline).where(Timeline.project_id == p.id)).scalar_one_or_none()
    if tl is None:
        tl = analyze(db, p.id)

    from ..models import Brief

    brief = db.execute(select(Brief).where(Brief.project_id == p.id)).scalar_one_or_none()
    lines = tl.lyric_lines or []
    if not lines:
        raise ProjectError("时间线没有歌词行，先做音频分析")

    descs = _scene_descriptions([l["text"] for l in lines], p.destination,
                                brief.genre if brief else "", p.id)

    assets = _usable_assets(db)
    if not assets:
        raise ProjectError("素材库没有可用镜头，先去素材库上传素材")

    # 用 LLM 一次性把「画面描述」分配到最贴切的素材上。
    # 原实现走 clip 编码器的向量检索，但未装 CLIP 时那是 md5 哈希伪造的向量，
    # 余弦相似度毫无语义含义 —— 等于随机配图，会出现「唱故宫配草原」这种现象。
    pool = _real_pool(assets, p.destination or "")
    plan = _ai_assign(db, descs, pool)

    # 但**优先**用「逐句分镜」里人工审核通过的映射：第几行歌词 → 哪张图，
    # 是人看过并认可的，比模型猜可靠得多。有分镜的行不再参与轮换，
    # 保证「这一句就该是这张图」。
    from . import storyboard as storyboard_svc

    by_index: dict[int, str] = {}
    by_text: dict[str, str] = {}
    ok_ids = {a.id for a in assets}
    for shot in storyboard_svc.shots(db, p.id):
        if shot.status == "approved" and shot.asset_id in ok_ids:
            by_index[shot.order] = shot.asset_id
            if (shot.line_text or "").strip():
                by_text[(shot.line_text or "").strip()] = shot.asset_id

    # 保留人工锁定/替换过的片段
    old = {c.order: c for c in db.execute(
        select(TimelineClip).where(TimelineClip.timeline_id == tl.id)).scalars()}
    for c in list(old.values()):
        if c.locked and keep_manual:
            continue
        db.delete(c)
    db.commit()

    used: set[str] = {c.asset_id for c in old.values() if c.locked and c.asset_id and keep_manual}
    counts: dict[str, int] = {}
    last_id: str | None = None
    strong = tl.beats[::4] if tl.beats else []
    order = 0
    # 先收集，再统一「收口」——因为歌词逐句对齐后句子之间会留空隙
    # （间奏、拖腔），如果每段只按句子时长排，镜头之间就有黑洞：
    # 画面总长会明显短于音频，成片在音乐还没结束时就没了。
    staged: list[dict] = []
    for i, (line, desc) in enumerate(zip(lines, descs)):
        start, end = float(line["start_s"]), float(line["end_s"])
        if end - start < 0.4:  # 过短的行与前一句合并
            continue
        if keep_manual and i in old and old[i].locked:
            old[i].order = order
            order += 1
            continue
        # ⚠️ 这里**刻意不做节拍吸附**。歌词时间轴现在来自强制对齐（歌声本身），
        # 是比节拍网格更可信的信息源；而 librosa 缺席时 tl.beats 是「84BPM 假网格」，
        # 拿它吸附等于把已经对齐好的时间再随机挪 ±0.4s —— 纯有害。见 align.py 顶部说明。
        _ = strong  # 保留变量以便将来接真实节拍（目前不参与计算）
        # 分镜里审过的行直接对号入座；其余行才走「LLM 分配 + 相邻轮换」
        pinned = by_index.get(i + 1) or by_text.get((line["text"] or "").strip())
        pinned_asset = db.get(Asset, pinned) if pinned else None
        asset = pinned_asset or _asset_for(i, desc, plan, pool, used, last_id, counts)
        if asset is None:
            break
        used.add(asset.id)
        counts[asset.id] = counts.get(asset.id, 0) + 1
        last_id = asset.id
        staged.append({"order": order, "asset": asset, "start": start, "end": end,
                       "line": line["text"], "desc": desc})
        order += 1

    # 收口：每个镜头延伸到下一个镜头的起点，最后一个铺到音频末尾，保证无缝
    total = float(tl.duration_s or 0)
    for k, it in enumerate(staged):
        nxt = staged[k + 1]["start"] if k + 1 < len(staged) else None
        tail = nxt if nxt is not None else (total if total > it["end"] else it["end"])
        it["end"] = max(it["end"], tail)
    # 第一句歌词之前通常有几秒前奏。这段也得有画面 —— 否则整条画面轨
    # 会比音频提前「前奏时长」秒，末句画面还没出来音乐就结束了。
    # 让第一个镜头从 0 开始铺（前奏就看着第一句的画面，MV 里很正常）。
    if staged:
        staged[0]["start"] = 0.0

    for it in staged:
        seg = max(0.6, it["end"] - it["start"])
        src_len = float(it["asset"].duration_s or 0) or seg
        # 需要的时间超过素材本身时，in_s 归零（整段都用上，渲染阶段再放慢补齐）
        in_s = 0.0 if src_len <= seg else round(min(src_len - seg, max(0.0, (src_len - seg) / 2)), 2)
        db.add(TimelineClip(
            timeline_id=tl.id, project_id=p.id, order=it["order"], asset_id=it["asset"].id,
            in_s=in_s, out_s=round(min(src_len, in_s + seg), 2),
            start_s=round(it["start"], 2), end_s=round(it["end"], 2),
            lyric_line=it["line"], scene_prompt=it["desc"], locked=False, manual=False,
        ))
    tl.auto_version += 1
    db.commit()
    return tl


def _real_pool(assets: list[Asset], destination: str = "") -> list[Asset]:
    """挑一批「该给这个项目用」的候选素材，按贴合度递减尝试。

    背景：实验室种子库里那 13 条是按文件名生成的占位色块，属于完全不相干的目的地；
    让 LLM 在这种候选里挑，一定会出现「唱永州配杭州雷峰塔色块」。

    但**光看「是不是 AI 生成」还不够** —— AI 素材是跨项目累积的，
    给桂林的项目放一批北京/永州的景点，同样会张冠李戴。所以按四级回退：

    1. 同目的地 且 AI 生成（最理想）
    2. 同目的地的素材（含占位图库 —— 桂林的占位里就有「漓江 航拍 雾」，地域是对的）
    3. 任意 AI 素材（目的地字段没填全时的兜底）
    4. 全部（没有任何真实素材时的最后兜底）
    """
    dest = (destination or "").strip()
    ai = [a for a in assets if (a.licensor or "").startswith("AI")]

    def same_dest(pool: list[Asset]) -> list[Asset]:
        if not dest:
            return []
        return [a for a in pool if a.destination and (dest in a.destination or a.destination in dest)]

    same_ai, same_any = same_dest(ai), same_dest(assets)
    # ① 同目的地的 AI 素材够多 → 最理想
    if len(same_ai) >= 4:
        return same_ai
    # ② 同目的地有一批素材（含占位图库，地域是对的）→ 用它们，别跨目的地
    if len(same_any) >= 4:
        return same_any
    # ③ 同目的地只有零星几条真实素材 → 也比跨目的地强
    if same_ai:
        return same_ai
    # ④ 兜底：全给。宁可混杂，也别让单张图反复循环十几遍。
    #    （真正的解法是去素材库给这个目的地点一次「AI 生成景点素材」）
    return assets


def _ai_assign(db: Session, descs: list[str], assets: list[Asset]) -> dict[int, str]:
    """让 LLM 把每个画面描述分配到一条素材，返回 {行下标: asset_id}。

    失败或未配置模型时返回空字典，调用方会回退到向量检索，保证流程不断。
    """
    from . import assets as assets_svc

    usable_ids = {a.id for a in assets}
    cand = [c for c in assets_svc.asset_descriptions(db) if c["id"] in usable_ids][:40]
    if not cand or not descs:
        return {}
    try:
        payload, _real = llm.json_chat(
            prompts.assign_assets_messages([{"desc": d} for d in descs], cand),
            fallback={"picks": []},
            purpose="assign_assets",
        )
    except Exception:  # noqa: BLE001
        return {}

    out: dict[int, str] = {}
    for it in (payload.get("picks") or []):
        try:
            li = int(it.get("line", 0)) - 1
            ai = int(it.get("asset", 0)) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= li < len(descs) and 0 <= ai < len(cand):
            out[li] = cand[ai]["id"]
    return out


def _asset_for(i: int, desc: str, plan: dict[int, str],
               assets: list[Asset], used: set[str],
               last_id: str | None = None, counts: dict[str, int] | None = None) -> Asset | None:
    """优先用 LLM 的分配结果，但**避免与上一个镜头撞同一张素材**。

    ⚠️ 这里**故意不做 used 去重**：AI 生成的素材数量通常少于镜头数，
    如果强制不重复，多出来的镜头就会退回占位色块 ——
    在「重复用贴切的真实景点」和「用不相关的色块」之间，前者明显更好。

    但完全不管复用会出现另一个问题：相邻两个镜头用同一张图，观众看到的就是
    「一个没切过的长镜头」，整片显得很单调（4 张素材撑 18 个镜头时尤其明显）。
    所以这里加一层轮换：只有当计划素材与上一个镜头撞图时，才换一张
    「用得最少、语义尽量接近」的替代素材。
    """
    by_id = {a.id: a for a in assets}
    aid = plan.get(i)
    cand = by_id.get(aid) if aid else None
    if cand is not None and cand.id != last_id:
        return cand
    rotated = _rotate_asset(desc, assets, counts or {}, exclude=last_id)
    return rotated or cand


def _rotate_asset(desc: str, assets: list[Asset], counts: dict[str, int],
                  exclude: str | None = None) -> Asset | None:
    """挑一张替代素材：使用次数最少优先，同次数里语义最接近的优先。"""
    pool = [a for a in assets if a.id != exclude]
    if not pool:
        return assets[0] if assets else None
    qv = encoder.encode_text(desc)
    return min(pool, key=lambda a: (counts.get(a.id, 0), -cosine(qv, a.embedding or [])))


def _best_asset(desc: str, assets: list[Asset], used: set[str]) -> Asset | None:
    """按画面描述做向量检索，未使用过的素材优先（PRD F3-2 同片不重复使用）。"""
    qv = encoder.encode_text(desc)
    scored: list[tuple[Asset, float]] = []
    for a in assets:
        emb = a.embedding or []
        s = cosine(qv, emb) if emb else 0.0
        scored.append((a, s))
    scored.sort(key=lambda x: x[1], reverse=True)
    for a, _s in scored:
        if a.id not in used:
            return a
    return scored[0][0] if scored else None


# ------------------------------------------------------------------ 镜头候选
def clip_candidates(db: Session, clip_id: str, top_k: int = 4) -> list[dict]:
    c = db.get(TimelineClip, clip_id)
    if c is None:
        raise ProjectError("片段不存在")
    assets = _usable_assets(db)
    qv = encoder.encode_text(c.scene_prompt or c.lyric_line or "")
    scored = sorted(((a, cosine(qv, a.embedding or [])) for a in assets),
                    key=lambda x: x[1], reverse=True)
    out = []
    for a, s in scored[:top_k]:
        out.append({
            "asset_id": a.id, "name": a.name, "thumb_url": _thumb_url(a),
            "duration_s": a.duration_s, "license_status": a.license_status,
            "destination": a.destination, "score": round(s, 4),
            "in_use": a.id == c.asset_id,
        })
    return out


def _thumb_url(a: Asset) -> str:
    if not a.thumb_path:
        return ""
    return f"/media/thumbs/{basename(a.thumb_path)}"


def timeline_payload(db: Session, project_id: str) -> dict:
    p = get_project(db, project_id)
    tl = db.execute(select(Timeline).where(Timeline.project_id == p.id)).scalar_one_or_none()
    if tl is None:
        return {"timeline": None, "clips": []}
    clips = list(db.execute(
        select(TimelineClip).where(TimelineClip.timeline_id == tl.id).order_by(TimelineClip.order)
    ).scalars())
    asset_map = {a.id: a for a in db.execute(select(Asset)).scalars()}
    out = []
    for c in clips:
        a = asset_map.get(c.asset_id or "")
        out.append({
            "id": c.id, "order": c.order, "asset_id": c.asset_id,
            "asset_name": a.name if a else "（未匹配）",
            "thumb_url": _thumb_url(a) if a else "",
            "video_url": _video_url(a) if a else "",
            "in_s": c.in_s, "out_s": c.out_s, "start_s": c.start_s, "end_s": c.end_s,
            "lyric_line": c.lyric_line, "scene_prompt": c.scene_prompt,
            "locked": c.locked, "manual": c.manual,
        })
    meta = tl.align_meta or {}
    return {
        "timeline": {
            # 不返回 audio_path：库里存的是服务器绝对路径（PRD V1.1 §11）
            "id": tl.id,
            "audio_url": f"/media/music/{basename(tl.audio_path)}" if tl.audio_path else "",
            "bpm": tl.bpm, "duration_s": tl.duration_s,
            "beats": tl.beats, "strong_beats": tl.beats[::4] if tl.beats else [],
            "lyric_lines": tl.lyric_lines, "auto_version": tl.auto_version,
            # 歌词时间轴的来源与质量：界面据此提示「来自歌声」还是「按字数估算」
            "align_engine": meta.get("engine", ""),
            "align_note": meta.get("note", ""),
            "align_match_ratio": meta.get("match_ratio"),
            "align_anchored": meta.get("anchored"),
            "vocal_span": meta.get("vocal_span"),
        },
        "clips": out,
    }


def _video_url(a: Asset) -> str:
    if not a.file_path:
        return ""
    return f"/media/assets/{basename(a.file_path)}"


def update_clip(db: Session, clip_id: str, *, asset_id: str | None = None,
                start_s: float | None = None, end_s: float | None = None,
                locked: bool | None = None) -> dict:
    c = db.get(TimelineClip, clip_id)
    if c is None:
        raise ProjectError("片段不存在")
    if asset_id:
        a = db.get(Asset, asset_id)
        if a is None:
            raise ProjectError("素材不存在")
        c.asset_id = a.id
        c.manual = True
        seg = max(0.6, float(end_s if end_s is not None else c.end_s) - float(
            start_s if start_s is not None else c.start_s))
        src_len = float(a.duration_s or 0) or seg
        c.in_s = 0.0 if src_len <= seg else round(min(src_len - seg, c.in_s), 2)
        c.out_s = round(min(src_len, c.in_s + seg), 2)
    if start_s is not None:
        c.start_s = round(float(start_s), 3)
    if end_s is not None:
        c.end_s = round(float(end_s), 3)
    if locked is not None:
        c.locked = bool(locked)
    db.commit()
    return {"id": c.id, "asset_id": c.asset_id, "start_s": c.start_s, "end_s": c.end_s,
            "locked": c.locked, "manual": c.manual}


def resnap(db: Session, project_id: str) -> Timeline:
    """重新自动卡点：保持人工锁定的片段不动。"""
    return auto_cut(db, project_id, keep_manual=True)
