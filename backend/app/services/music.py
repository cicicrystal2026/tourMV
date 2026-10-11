"""音乐模型服务：生曲（生产模式 / 选型轮）、人工评分、选定揭晓、模型战绩统计。

PRD V1.1 D2 —— 两种模式并存：
- production（生产模式）：日常出片。所选模型出 1 首，不满意换模型或同模型再来；
  直接显示模型名；**不计入模型战绩**。
- selection（选型轮）：确定主力模型。参赛模型并行各出 N 版（默认 2），
  **强制盲听**（前端与接口都不暴露模型），选定后揭晓，只有选型轮计入战绩。
"""
from __future__ import annotations

import os
import random
import uuid

from sqlalchemy import func
from sqlalchemy import select as sa_select
from sqlalchemy.orm import Session

from .. import serialize as ser
from ..adapters import music as music_reg
from ..adapters.music.base import GenRequest
from ..config import csv_list, settings
from ..models import Brief, MusicCandidate, MusicModel, MusicRound, Project, Rating
from ..queue import enqueue
from .projects import ProjectError, ensure_editable, get as get_project

# 生产模式可选的模型名单。可用 MUSIC_MODEL_KEYS 覆盖（逗号分隔）。
# 选型轮参赛名单另见 SELECTION_MODEL_KEYS（PRD V1.1 §7.2）。
DEFAULT_MODEL_KEYS = ["mureka", "eleven", "lyria"]


def model_keys() -> list[str]:
    """生产模式名单。只返回已注册的适配器，避免配错 key 直接炸流程。"""
    settings()  # 确保 .env 已加载（settings 是 lru_cache，第一次调用时才读 .env）
    keys = csv_list(os.environ.get("MUSIC_MODEL_KEYS", "")) or DEFAULT_MODEL_KEYS
    valid = [k for k in keys if music_reg.get(k) is not None]
    return valid or DEFAULT_MODEL_KEYS


def selection_keys() -> list[str]:
    return [k for k in settings().selection_model_keys if music_reg.get(k) is not None]


def _roster() -> list[str]:
    seen: list[str] = []
    for k in model_keys() + selection_keys():
        if k not in seen:
            seen.append(k)
    return seen


def usable(key: str) -> bool:
    """能否用于生成：真实就绪，或者允许 mock 占位。"""
    a = music_reg.get(key)
    if a is None:
        return False
    return a.ready or settings().allow_mock


def ensure_models(db: Session) -> None:
    """建表行，并把 name/run_mode/available 同步到当前配置。

    note 刻意不写库：它描述的是「当前配置下这个模型怎么用」，会随 .env 变化。
    list_models() 实时取 adapter.note()；库里的 note 只用于存用户手填的备注。
    """
    changed = False
    roster = _roster()
    for key in roster:
        adapter = music_reg.get(key)
        name = adapter.name if adapter else key
        run_mode = adapter.run_mode if adapter else "api"
        row = db.execute(sa_select(MusicModel).where(MusicModel.key == key)).scalar_one_or_none()
        if row is None:
            db.add(MusicModel(key=key, name=name, run_mode=run_mode, enabled=True,
                              available=bool(adapter and adapter.ready), note=""))
            changed = True
            continue
        available = bool(adapter and adapter.ready)
        if (row.name, row.run_mode, row.available) != (name, run_mode, available):
            row.name, row.run_mode, row.available = name, run_mode, available
            changed = True
    # 跌出名单的模型一并停用：否则它仍会挤进生成（无 Key 时还会降级成 mock）
    for row in db.execute(sa_select(MusicModel)).scalars():
        if row.key not in roster and row.enabled:
            row.enabled = False
            changed = True
    if changed:
        db.commit()


def list_models(db: Session) -> list[dict]:
    ensure_models(db)
    sel = set(selection_keys())
    prod = set(model_keys())
    out = []
    for m in db.execute(sa_select(MusicModel).order_by(MusicModel.created_at)).scalars():
        adapter = music_reg.get(m.key)
        out.append({
            "key": m.key, "name": m.name, "run_mode": m.run_mode,
            "enabled": m.enabled, "available": m.available,
            # 战绩只来自选型轮（PRD V1.1 F1-8）；avg_score 为 1–5 分
            "wins": m.wins, "rounds": m.rounds,
            "avg_score": m.avg_score, "score_n": m.score_n,
            "in_selection": m.key in sel, "in_production": m.key in prod,
            "usable": usable(m.key),
            "note": m.note or (adapter.note() if adapter else ""),
            "capabilities": adapter.capabilities() if adapter else {},
            "effective": (music_reg.resolve(m.key).key if adapter else "mock"),
            "ready": bool(adapter and adapter.ready),
            # 提供商的版本/变体（如 Mureka 的 mureka-7.6 / mureka-9.5），供界面显示
            "model": getattr(adapter, "model_name", "") if adapter else "",
        })
    return out


# ------------------------------------------------------------------ 轮次
def rounds_of(db: Session, project_id: str) -> list[MusicRound]:
    return list(db.execute(
        sa_select(MusicRound).where(MusicRound.project_id == project_id)
        .order_by(MusicRound.round_no)).scalars())


def latest_round(db: Session, project_id: str) -> MusicRound | None:
    rs = rounds_of(db, project_id)
    return rs[-1] if rs else None


def current_round(db: Session, project_id: str) -> int:
    r = latest_round(db, project_id)
    return r.round_no if r else 1


def is_blind(r: MusicRound | None) -> bool:
    """选型轮在揭晓（选定）之前一律盲听。"""
    return bool(r and r.kind == "selection" and not r.winner_candidate_id)


def _new_round(db: Session, p: Project, kind: str, keys: list[str], variants: int) -> MusicRound:
    rs = rounds_of(db, p.id)
    r = MusicRound(project_id=p.id, kind=kind, round_no=(rs[-1].round_no + 1) if rs else 1,
                   model_keys=list(keys), variants=variants)
    if kind == "selection":
        top = db.execute(sa_select(func.max(MusicRound.selection_no))).scalar() or 0
        r.selection_no = int(top) + 1
    db.add(r)
    db.flush()
    return r


def _production_round(db: Session, p: Project) -> MusicRound:
    """生产模式的候选都追加到同一个生产轮里（与 V1.0 行为一致）。

    但如果最近一轮是选型轮，就另开一个生产轮，不往选型轮里掺明牌候选。
    """
    r = latest_round(db, p.id)
    if r is not None and r.kind == "production":
        return r
    return _new_round(db, p, "production", [], 1)


# ------------------------------------------------------------------ 生成
def _estimate(keys: list[str], variants: int, duration_s: int) -> float:
    req = GenRequest(lyrics="", style_prompt="", duration_s=duration_s)
    total = 0.0
    for k in keys:
        a = music_reg.resolve(k)
        total += a.estimate_cost(req) * variants
    return round(total, 2)


def generate(db: Session, project_id: str, model_keys: list[str], variants: int = 1,
             style_prompt: str | None = None, mode: str = "production",
             round_id: str | None = None) -> dict:
    from . import budget

    p = get_project(db, project_id)
    ensure_editable(p)
    mode = "selection" if mode == "selection" else "production"
    ensure_models(db)

    target: MusicRound | None = db.get(MusicRound, round_id) if round_id else None
    if target is not None:
        if target.project_id != p.id:
            raise ProjectError("轮次不属于该项目")
        mode = target.kind

    if mode == "selection":
        allowed = set(selection_keys())
        model_keys = [k for k in (model_keys or selection_keys()) if k in allowed]
        variants = max(1, min(4, int(variants or 2)))
    else:
        if not model_keys:
            # 未显式指定：取第一个启用且可用的模型（默认只出一首，省钱）
            model_keys = [m.key for m in db.execute(
                sa_select(MusicModel).where(MusicModel.enabled.is_(True))
                .order_by(MusicModel.created_at)).scalars() if usable(m.key)][:1]
        model_keys = [k for k in model_keys if music_reg.get(k) is not None]
        variants = max(1, min(4, int(variants or 1)))

    # 生产环境禁用占位结果（PRD V1.1 §11 数据隔离）
    blocked = [k for k in model_keys if not usable(k)]
    if blocked:
        names = "、".join((music_reg.get(k).name if music_reg.get(k) else k) for k in blocked)
        raise ProjectError(f"{names} 未配置凭证，不能生成（生产环境禁用占位结果，见 ALLOW_MOCK）")
    model_keys = list(dict.fromkeys(model_keys))
    if not model_keys:
        raise ProjectError("至少勾选 1 个可用模型才能生成")
    if mode == "selection" and target is None and len(model_keys) < 2:
        raise ProjectError("选型轮至少需要 2 个可用的参赛模型才有对比意义")

    brief = db.execute(sa_select(Brief).where(Brief.project_id == p.id)).scalar_one_or_none()
    if brief and style_prompt is not None:
        brief.style_prompt = style_prompt
        db.commit()

    from .lyrics import latest
    ver = latest(db, p.id)
    if ver is None:
        raise ProjectError("还没有歌词版本，先生成并确认歌词")

    budget.ensure_budget(db, _estimate(model_keys, variants, int((brief.duration_s if brief else 0) or settings().song_default_seconds)),
                         what=f"{len(model_keys) * variants} 首生曲")

    if target is None:
        target = _new_round(db, p, "selection", model_keys, variants) if mode == "selection" \
            else _production_round(db, p)
    for k in model_keys:
        if k not in (target.model_keys or []):
            target.model_keys = [*(target.model_keys or []), k]

    existing = list(db.execute(
        sa_select(MusicCandidate).where(MusicCandidate.round_id == target.id)).scalars())
    used_labels = {c.label for c in existing}
    plan = [(k, v) for k in model_keys for v in range(1, variants + 1)]
    labels = [chr(65 + i) for i in range(26) if chr(65 + i) not in used_labels][:len(plan)]
    if mode == "selection":
        # 盲听编号随机分配：否则 A/B 永远是名单里第一个模型，编号本身就泄露了来源
        random.shuffle(labels)

    created: list[MusicCandidate] = []
    for (key, v), label in zip(plan, labels):
        model_row = db.execute(sa_select(MusicModel).where(MusicModel.key == key)).scalar_one_or_none()
        prior = len([c for c in existing + created if c.model_key == key])
        c = MusicCandidate(
            project_id=p.id, lyric_version_id=ver.id, round=target.round_no, round_id=target.id,
            model_key=key, model_name=model_row.name if model_row else key,
            variant=prior + 1, label=label, status="queued",
            audio_token=uuid.uuid4().hex,
        )
        db.add(c)
        created.append(c)
    db.commit()

    for c in created:
        db.refresh(c)
        adapter = music_reg.get(c.model_key)
        queue = "gpu" if adapter and adapter.run_mode == "local" else "api"
        # 选型轮的任务名不能带模型名：任务队列在看板上是公开可见的
        name = f"选型轮候选 {c.label}" if mode == "selection" else f"{c.model_name} 第 {c.variant} 版"
        enqueue(db, "music_one", queue=queue, project_id=p.id, ref_id=c.id, name=name,
                payload={"candidate_id": c.id, "style_prompt": (brief.style_prompt if brief else ""),
                         "blind": mode == "selection"})

    total = len(existing) + len(created)
    p.status_note = (f"选型轮 #{target.selection_no}：{total} 个盲听候选" if mode == "selection"
                     else f"本轮共 {total} 个候选")
    db.commit()
    return {"round": target.round_no, "round_id": target.id, "mode": mode,
            "created": len(created), "total": total,
            "candidates": [c.id for c in created]}


# ------------------------------------------------------------------ 查询
def _rating_of(db: Session, candidate_id: str) -> dict | None:
    r = db.execute(sa_select(Rating).where(Rating.candidate_id == candidate_id,
                                           Rating.rater == "default")).scalar_one_or_none()
    if r is None:
        return None
    return {"diction": r.diction, "melody": r.melody, "arrangement": r.arrangement,
            "total": r.total, "avg": round(r.total / 3, 2)}


def round_meta(r: MusicRound) -> dict:
    return {"id": r.id, "round_no": r.round_no, "kind": r.kind, "selection_no": r.selection_no,
            "blind": is_blind(r), "winner_candidate_id": r.winner_candidate_id,
            "variants": r.variants,
            # 盲听时不列参赛模型（只给数量），揭晓后再给
            "model_keys": None if is_blind(r) else (r.model_keys or []),
            "model_count": len(r.model_keys or [])}


def candidates_payload(db: Session, project_id: str, round_id: str | None = None,
                       round_no: int | None = None) -> dict:
    rs = rounds_of(db, project_id)
    target = None
    if round_id:
        target = next((r for r in rs if r.id == round_id), None)
    elif round_no:
        target = next((r for r in rs if r.round_no == round_no), None)
    target = target or (rs[-1] if rs else None)
    if target is None:
        return {"round": 1, "round_meta": None, "rounds": [], "items": []}
    blind = is_blind(target)
    rows = list(db.execute(sa_select(MusicCandidate).where(MusicCandidate.round_id == target.id)
                           .order_by(MusicCandidate.label)).scalars())
    items = [ser.candidate(c, _rating_of(db, c.id), blind=blind, kind=target.kind) for c in rows]
    return {"round": target.round_no, "round_meta": round_meta(target),
            "rounds": [round_meta(r) for r in rs], "items": items}


def candidates(db: Session, project_id: str, round_: int | None = None) -> list[dict]:
    return candidates_payload(db, project_id, round_no=round_)["items"]


# ------------------------------------------------------------------ 评分与选定
def rate(db: Session, candidate_id: str, *, diction: int, melody: int, arrangement: int,
         rater: str = "default") -> dict:
    c = db.get(MusicCandidate, candidate_id)
    if c is None:
        raise ProjectError("候选不存在")
    ensure_editable(get_project(db, c.project_id))
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
    recompute_model_stats(db)
    return {"candidate_id": candidate_id, "total": r.total, "avg": round(r.total / 3, 2)}


def select(db: Session, project_id: str, candidate_id: str) -> dict:
    p = get_project(db, project_id)
    ensure_editable(p)
    c = db.get(MusicCandidate, candidate_id)
    if c is None or c.project_id != p.id:
        raise ProjectError("候选不属于该项目")
    if c.status != "done":
        raise ProjectError("该候选尚未生成完成，无法选定")
    if c.is_mock and not settings().allow_mock:
        raise ProjectError("这是占位结果（模型未配置），不能选定为交付曲目")
    r = db.get(MusicRound, c.round_id) if c.round_id else None
    if r is not None and r.kind == "selection":
        # 选型轮：所有已完成的真实候选都打过分才能揭晓，否则「盲听评分」名存实亡
        # 注意不能排除占位候选：演示模式下整轮都是占位，排除后「全部已评分」恒成立，
        # 等于没评分就能揭晓（E2E 实测抓到的缺陷）。统计里排除占位，评分要求里不排除。
        unrated = [x for x in db.execute(sa_select(MusicCandidate).where(
            MusicCandidate.round_id == r.id, MusicCandidate.status == "done")).scalars()
            if _rating_of(db, x.id) is None]
        if unrated:
            raise ProjectError(f"选型轮还有 {len(unrated)} 个候选未评分（{'、'.join(sorted(u.label for u in unrated))}），"
                               "全部评分后再选定")
        r.winner_candidate_id = c.id      # 改选即覆盖：上一次胜出随之撤销
    p.selected_candidate_id = candidate_id
    db.commit()
    recompute_model_stats(db)
    return {"selected": candidate_id, "model_key": c.model_key, "model_name": c.model_name,
            "variant": c.variant, "round": c.round,
            "revealed": bool(r and r.kind == "selection")}


def recompute_model_stats(db: Session, project_id: str | None = None) -> None:
    """跨项目累计，只统计**已揭晓的选型轮**（PRD V1.1 F1-8 / F2-12）：

    - rounds：模型在该轮有真实（非占位）完成候选即计 1 轮；
    - wins  ：该轮选定候选所属模型 +1（每轮至多 1 次）；
    - avg   ：先取模型在该轮各候选的三项均分（1–5），再对轮次取平均。
    未揭晓的轮次不参与 —— 否则盲听过程中盯着均分变化就能反推出模型。
    """
    models = {m.key: m for m in db.execute(sa_select(MusicModel)).scalars()}
    for m in models.values():
        m.wins, m.rounds, m.score_sum, m.score_n = 0, 0, 0.0, 0

    done_rounds = db.execute(sa_select(MusicRound).where(
        MusicRound.kind == "selection", MusicRound.winner_candidate_id.is_not(None))).scalars()
    for r in done_rounds:
        cands = [c for c in db.execute(sa_select(MusicCandidate).where(
            MusicCandidate.round_id == r.id)).scalars() if not c.is_mock and c.status == "done"]
        per_model: dict[str, list[float]] = {}
        for c in cands:
            per_model.setdefault(c.model_key, [])
            rt = _rating_of(db, c.id)
            if rt:
                per_model[c.model_key].append(rt["total"] / 3)
        for key, scores in per_model.items():
            m = models.get(key)
            if m is None:
                continue
            m.rounds += 1
            if scores:
                m.score_sum += sum(scores) / len(scores)
                m.score_n += 1
        winner = db.get(MusicCandidate, r.winner_candidate_id)
        if winner and not winner.is_mock and winner.model_key in models:
            models[winner.model_key].wins += 1
    db.commit()


def winner_of(db: Session, project_id: str) -> MusicCandidate | None:
    p = get_project(db, project_id)
    if not p.selected_candidate_id:
        return None
    return db.get(MusicCandidate, p.selected_candidate_id)


def regenerate_with_winner(db: Session, project_id: str, variants: int = 1) -> dict:
    """用选定版本的模型再生成：选型轮里仍是盲听候选（F2-13），生产模式就是「同模型再来」。"""
    w = winner_of(db, project_id)
    if w is None:
        raise ProjectError("还没有选定曲目")
    r = db.get(MusicRound, w.round_id) if w.round_id else None
    if r is not None and r.kind == "selection":
        return generate(db, project_id, [w.model_key], variants, mode="selection", round_id=r.id)
    return generate(db, project_id, [w.model_key], variants, mode="production")
