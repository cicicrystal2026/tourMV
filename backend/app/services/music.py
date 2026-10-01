"""音乐模型服务：多模型批量生曲、人工评分、选定揭晓、模型战绩统计。

按用户决定：已去除盲听 —— 候选卡片直接显示模型名与版本；
但「评分 + 选定 + 跨项目累计胜出/平均分」这套模型选型统计完整保留。
"""
from __future__ import annotations

import os

from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from .. import serialize as ser
from ..adapters import music as music_reg
from ..config import csv_list, settings
from ..models import Brief, MusicCandidate, MusicModel, Project, Rating
from ..queue import enqueue
from .projects import ProjectError, get as get_project

# 参与「同轮多模型对比」的模型名单。可用 MUSIC_MODEL_KEYS 覆盖（逗号分隔）。
#
# 默认三家的选择依据（2026-09 核价）：
#   mureka  —— $0.045/首，国内可直连，最长 5m30s，官方明示付费调用含商用授权
#   eleven  —— $0.15/分钟，授权条款最清晰，但最贵
#   lyria   —— $0.08/首，官方 API 里最便宜，44.1kHz 立体声 + 支持自定义中文歌词
# 不含 minimax：官方 2026-08-20 起已停止对**新用户**开放音乐/歌词生成 API，
# 免费档也已停服。适配器保留（存量账号仍可用），但不进默认对比名单。
DEFAULT_MODEL_KEYS = ["mureka", "eleven", "lyria"]


def model_keys() -> list[str]:
    """当前对比名单。只返回已注册的适配器，避免配错 key 直接炸流程。"""
    settings()  # 确保 .env 已加载（settings 是 lru_cache，第一次调用时才读 .env）
    keys = csv_list(os.environ.get("MUSIC_MODEL_KEYS", "")) or DEFAULT_MODEL_KEYS
    valid = [k for k in keys if music_reg.get(k) is not None]
    return valid or DEFAULT_MODEL_KEYS


def default_models() -> list[dict]:
    """模型清单。名称与运行方式取自适配器本身，因此会随 .env 配置漂移
    （例如 MiniMax 从本地部署切到官方 API 后，名称与队列随之变化）。"""
    out = []
    for key in model_keys():
        a = music_reg.get(key)
        out.append({
            "key": key,
            "name": (a.name if a else key),
            "run_mode": (a.run_mode if a else "api"),
        })
    return out


def ensure_models(db: Session) -> None:
    """建表行，并把 name/run_mode/available 同步到当前配置。

    note 刻意不写库：它描述的是「当前配置下这个模型怎么用」，会随 .env 变化
    （例如 MiniMax 从本地部署切官方 API），入库就会变成过期说明。
    list_models() 实时取 adapter.note()；库里的 note 只用于存用户手填的备注。
    available 之前只在首次 seed 时写一次，导致「后补了 Key 但界面仍显示不可用」，
    这里改为每次读取时对齐实际凭证状态。
    """
    changed = False
    for spec in default_models():
        adapter = music_reg.get(spec["key"])
        row = db.execute(sa_select(MusicModel).where(MusicModel.key == spec["key"])).scalar_one_or_none()
        if row is None:
            db.add(MusicModel(
                key=spec["key"], name=spec["name"], run_mode=spec["run_mode"],
                enabled=True, available=bool(adapter and adapter.ready), note="",
            ))
            changed = True
            continue
        if row.name != spec["name"]:
            row.name = spec["name"]
            changed = True
        if row.run_mode != spec["run_mode"]:
            row.run_mode = spec["run_mode"]
            changed = True
        available = bool(adapter and adapter.ready)
        if row.available != available:
            row.available = available
            changed = True
    # 跌出对比名单的模型必须一并停用：生曲时取的是「所有 enabled 的行」，
    # 只改名单不停用的话它仍会挤进每一轮对比（无 Key 时降级成 Mock，白占一个候选位）。
    # 名单内的模型不动 enabled —— 那是界面上人工勾选的意图，不能被覆盖。
    roster = {spec["key"] for spec in default_models()}
    for row in db.execute(sa_select(MusicModel)).scalars():
        if row.key not in roster and row.enabled:
            row.enabled = False
            changed = True
    if changed:
        db.commit()


def list_models(db: Session) -> list[dict]:
    ensure_models(db)
    out = []
    for m in db.execute(sa_select(MusicModel).order_by(MusicModel.created_at)).scalars():
        adapter = music_reg.get(m.key)
        out.append({
            "key": m.key, "name": m.name, "run_mode": m.run_mode,
            "enabled": m.enabled, "available": m.available,
            "wins": m.wins, "rounds": m.rounds,
            "avg_score": m.avg_score, "score_n": m.score_n,
            "note": m.note or (adapter.note() if adapter else ""),
            "capabilities": adapter.capabilities() if adapter else {},
            "effective": (music_reg.resolve(m.key).key if adapter else "mock"),
            "ready": bool(adapter and adapter.ready),
            # 提供商的版本/变体（如 Mureka 的 mureka-7.6 / mureka-9.5），供界面显示
            "model": getattr(adapter, "model_name", "") if adapter else "",
        })
    return out


def current_round(db: Session, project_id: str) -> int:
    rows = db.execute(sa_select(MusicCandidate.round).where(MusicCandidate.project_id == project_id)).scalars()
    return max(rows, default=0) or 1


def generate(db: Session, project_id: str, model_keys: list[str], variants: int = 1,
             style_prompt: str | None = None) -> dict:
    p = get_project(db, project_id)
    if not model_keys:
        # 未显式指定则取全部启用的模型（供状态机派发的任务使用）
        model_keys = [m.key for m in db.execute(
            sa_select(MusicModel).where(MusicModel.enabled.is_(True))).scalars()]
    model_keys = [k for k in model_keys if music_reg.get(k) is not None]
    if not model_keys:
        raise ProjectError("至少勾选 1 个模型才能生成")
    # 默认 1 版：一轮只出一首，试听后再决定是否换模型/加量（省钱，见项目 MEMORY.md）
    variants = max(1, min(4, int(variants or 1)))

    brief = db.execute(sa_select(Brief).where(Brief.project_id == p.id)).scalar_one_or_none()
    if brief and style_prompt is not None:
        brief.style_prompt = style_prompt
        db.commit()

    from .lyrics import latest
    ver = latest(db, p.id)
    if ver is None:
        raise ProjectError("还没有歌词版本，先生成并确认歌词")

    rnd = current_round(db, p.id)
    # 同一轮内再次调用视为追加候选（例如「用胜出模型再生成」）
    existing = db.execute(
        sa_select(MusicCandidate).where(MusicCandidate.project_id == p.id, MusicCandidate.round == rnd)
    ).scalars().all()

    base = len(existing)
    created: list[MusicCandidate] = []
    for mi, key in enumerate(model_keys):
        adapter = music_reg.get(key)
        for v in range(1, variants + 1):
            idx = base + len(created)
            model_row = db.execute(sa_select(MusicModel).where(MusicModel.key == key)).scalar_one_or_none()
            c = MusicCandidate(
                project_id=p.id, lyric_version_id=ver.id, round=rnd, model_key=key,
                model_name=model_row.name if model_row else key, variant=v,
                label=chr(65 + idx % 26), status="queued",
            )
            db.add(c)
            created.append(c)
    db.commit()
    for c in created:
        db.refresh(c)
        queue = "gpu" if (adapter := music_reg.get(c.model_key)) and adapter.run_mode == "local" else "api"
        enqueue(db, "music_one", queue=queue, project_id=p.id, ref_id=c.id,
                name=f"{c.model_name} 第 {c.variant} 版",
                payload={"candidate_id": c.id, "style_prompt": (brief.style_prompt if brief else "")})

    p.status_note = f"本轮共 {len(existing) + len(created)} 个候选"
    db.commit()
    return {"round": rnd, "created": len(created), "total": len(existing) + len(created),
            "candidates": [c.id for c in created]}


def candidates(db: Session, project_id: str, round_: int | None = None) -> list[dict]:
    stmt = sa_select(MusicCandidate).where(MusicCandidate.project_id == project_id)
    if round_:
        stmt = stmt.where(MusicCandidate.round == round_)
    stmt = stmt.order_by(MusicCandidate.round, MusicCandidate.label)
    rows = list(db.execute(stmt).scalars())
    out = []
    for c in rows:
        r = db.execute(sa_select(Rating).where(Rating.candidate_id == c.id,
                                               Rating.rater == "default")).scalar_one_or_none()
        rating = ({"diction": r.diction, "melody": r.melody,
                   "arrangement": r.arrangement, "total": r.total} if r else None)
        # 统一走序列化层：媒体 URL 只在 serialize.media_url 里拼一次。
        # 注意不要在这里手写 split('/')，Windows 的 audio_path 用反斜杠，会切不开而把
        # 绝对路径整串塞进 URL（既 404 又泄露服务器路径）。
        out.append(ser.candidate(c, rating))
    return out


def rate(db: Session, candidate_id: str, *, diction: int, melody: int, arrangement: int,
         rater: str = "default") -> dict:
    c = db.get(MusicCandidate, candidate_id)
    if c is None:
        raise ProjectError("候选不存在")
    for v in (diction, melody, arrangement):
        if not 1 <= int(v) <= 5:
            raise ProjectError("评分必须在 1-5 之间")
    r = db.execute(sa_select(Rating).where(Rating.candidate_id == candidate_id,
                                        Rating.rater == rater)).scalar_one_or_none()
    if r is None:
        r = Rating(candidate_id=candidate_id, project_id=c.project_id, rater=rater)
        db.add(r)
    r.diction, r.melody, r.arrangement = int(diction), int(melody), int(arrangement)
    db.commit()
    recompute_model_stats(db, c.project_id)
    return {"candidate_id": candidate_id, "total": r.total}


def select(db: Session, project_id: str, candidate_id: str) -> dict:
    p = get_project(db, project_id)
    c = db.get(MusicCandidate, candidate_id)
    if c is None or c.project_id != p.id:
        raise ProjectError("候选不属于该项目")
    if c.status != "done":
        raise ProjectError("该候选尚未生成完成，无法选定")
    p.selected_candidate_id = candidate_id
    db.commit()
    recompute_model_stats(db, p.id)
    return {"selected": candidate_id, "model_key": c.model_key, "model_name": c.model_name,
            "variant": c.variant, "round": c.round}


def recompute_model_stats(db: Session, project_id: str) -> None:
    """跨项目累计：wins（每项目每轮最多计 1 次胜出）与平均分。"""
    models = {m.key: m for m in db.execute(sa_select(MusicModel)).scalars()}
    for m in models.values():
        m.wins, m.rounds, m.score_sum, m.score_n = 0, 0, 0.0, 0

    # 胜出：遍历所有项目，按 (项目, 轮次) 归一到选定候选所属模型
    projects = list(db.execute(sa_select(Project)).scalars())
    win_map: dict[tuple[str, int], str] = {}
    for p in projects:
        if not p.selected_candidate_id:
            continue
        sel = db.get(MusicCandidate, p.selected_candidate_id)
        if sel is None:
            continue
        win_map[(p.id, sel.round)] = sel.model_key
        if sel.model_key in models:
            models[sel.model_key].rounds += 1
            models[sel.model_key].wins += 1

    # 平均分：按 (模型, 项目, 轮次) 聚合，先取该轮各版本均分，再对轮次取均值
    grouped: dict[tuple[str, str, int], list[int]] = {}
    for c in db.execute(sa_select(MusicCandidate)).scalars():
        r = db.execute(sa_select(Rating).where(Rating.candidate_id == c.id,
                                            Rating.rater == "default")).scalar_one_or_none()
        if r is None:
            continue
        grouped.setdefault((c.model_key, c.project_id, c.round), []).append(r.total)
    for (key, _pid, _rd), totals in grouped.items():
        if key not in models:
            continue
        models[key].score_sum += sum(totals) / len(totals)
        models[key].score_n += 1

    db.commit()


def winner_of(db: Session, project_id: str) -> MusicCandidate | None:
    p = get_project(db, project_id)
    if not p.selected_candidate_id:
        return None
    return db.get(MusicCandidate, p.selected_candidate_id)


def regenerate_with_winner(db: Session, project_id: str, variants: int = 1) -> dict:
    w = winner_of(db, project_id)
    if w is None:
        raise ProjectError("还没有选定曲目")
    return generate(db, project_id, [w.model_key], variants)
