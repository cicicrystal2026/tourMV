"""度量：人工工时、项目复盘、选型结论（PRD V1.1 F1-10 / F1-11 / §5.1）。"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import state_machine as sm
from ..models import (AiCall, LyricVersion, MusicCandidate, MusicModel, MusicRound, Project,
                      Rating, StoryboardShot, WorkLog)
from .projects import ProjectError

HEARTBEAT_S = 30          # 前端心跳间隔
MAX_PER_BEAT_S = 45       # 单次心跳最多记 45 秒，防止异常请求灌水


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ------------------------------------------------------------------ 工时
def on_transition(db: Session, project_id: str, old: str, new: str) -> None:
    """状态切换时开 / 关人工节点的工时记录。由 set_state 与后台推进统一调用。"""
    if old == new:
        return
    if sm.is_human(old):
        for w in db.execute(select(WorkLog).where(
                WorkLog.project_id == project_id, WorkLog.node == old,
                WorkLog.confirmed_at.is_(None))).scalars():
            w.confirmed_at = _now()
    if sm.is_human(new):
        db.add(WorkLog(project_id=project_id, node=new, entered_at=_now()))


def heartbeat(db: Session, project_id: str, seconds: int = HEARTBEAT_S) -> dict:
    """页面在前台且最近有操作时，前端每 30 秒报一次：计入当前人工节点的有效时长。"""
    p = db.get(Project, project_id)
    if p is None:
        raise ProjectError("项目不存在")
    if not sm.is_human(p.status) or p.locked_at is not None:
        return {"counted": 0}
    w = db.execute(select(WorkLog).where(
        WorkLog.project_id == project_id, WorkLog.node == p.status,
        WorkLog.confirmed_at.is_(None)).order_by(WorkLog.entered_at.desc())).scalars().first()
    if w is None:      # 存量项目没有进入记录：从现在开始记
        w = WorkLog(project_id=project_id, node=p.status, entered_at=_now())
        db.add(w)
    add = max(0, min(MAX_PER_BEAT_S, int(seconds)))
    w.active_seconds = (w.active_seconds or 0) + add
    db.commit()
    return {"counted": add, "node": p.status}


def set_manual(db: Session, project_id: str, node: str, minutes: float, note: str = "") -> dict:
    rows = list(db.execute(select(WorkLog).where(
        WorkLog.project_id == project_id, WorkLog.node == node)).scalars())
    if not rows:
        raise ProjectError("该节点还没有工时记录")
    w = rows[-1]
    w.manual_minutes = max(0.0, float(minutes))
    w.note = (note or "")[:300]
    db.commit()
    return {"node": node, "manual_minutes": w.manual_minutes}


# ------------------------------------------------------------------ 复盘
def _lyric_edit_ratio(db: Session, project_id: str) -> float | None:
    """写词指标：歌词确认前人工改动行数占比（以首个 AI 版本对比最新版本，§5.1）。"""
    vers = list(db.execute(select(LyricVersion).where(LyricVersion.project_id == project_id)
                           .order_by(LyricVersion.version)).scalars())
    first = next((v for v in vers if v.source == "ai"), None)
    if first is None or not vers:
        return None
    a = [ln for s in first.sections for ln in s.get("lines") or []]
    b = [ln for s in vers[-1].sections for ln in s.get("lines") or []]
    if not a:
        return None
    changed = sum(1 for i, ln in enumerate(a) if i >= len(b) or b[i] != ln)
    return round(changed / len(a), 3)


def review(db: Session, project_id: str) -> dict:
    p = db.get(Project, project_id)
    if p is None:
        raise ProjectError("项目不存在")
    nodes = []
    total_min = 0.0
    for w in db.execute(select(WorkLog).where(WorkLog.project_id == project_id)
                        .order_by(WorkLog.entered_at)).scalars():
        active_min = round((w.active_seconds or 0) / 60, 1)
        wall_min = round(((w.confirmed_at or _now()) - w.entered_at).total_seconds() / 60, 1) \
            if w.entered_at else None
        used = w.manual_minutes if w.manual_minutes is not None else active_min
        total_min += used or 0
        nodes.append({"node": w.node, "label": sm.STATES[w.node].label if w.node in sm.STATES else w.node,
                      "entered_at": w.entered_at.isoformat() + "Z" if w.entered_at else None,
                      "confirmed_at": w.confirmed_at.isoformat() + "Z" if w.confirmed_at else None,
                      "active_minutes": active_min, "wall_minutes": wall_min,
                      "manual_minutes": w.manual_minutes, "note": w.note, "open": w.confirmed_at is None})
    calls = db.execute(select(AiCall.kind, func.count(), func.coalesce(func.sum(AiCall.cost), 0.0),
                              func.coalesce(func.sum(AiCall.elapsed_s), 0.0))
                       .where(AiCall.project_id == project_id).group_by(AiCall.kind)).all()
    music_cost = db.execute(select(func.coalesce(func.sum(MusicCandidate.cost), 0.0))
                            .where(MusicCandidate.project_id == project_id)).scalar_one()
    by_kind = {k: {"calls": n, "cost": round(float(c), 3), "elapsed_s": round(float(e), 1)} for k, n, c, e in calls}
    by_kind["music"] = {**by_kind.get("music", {"calls": 0, "elapsed_s": 0.0}),
                        "cost": round(float(music_cost or 0), 3)}
    total_cost = round(sum(v["cost"] for v in by_kind.values()), 3)
    shots = list(db.execute(select(StoryboardShot).where(StoryboardShot.project_id == project_id)).scalars())
    real = [s for s in shots if s.provider in ("library", "upload")]
    return {
        "project": {"id": p.id, "name": p.name, "status": p.status},
        "work": {"nodes": nodes, "total_minutes": round(total_min, 1),
                 "target_minutes": 8 * 60, "within_target": total_min <= 8 * 60},
        "cost": {"total": total_cost, "by_kind": by_kind},
        "experience": {
            "lyric_edit_ratio": _lyric_edit_ratio(db, project_id), "lyric_edit_target": 0.3,
            "real_shot_ratio": round(len(real) / len(shots), 3) if shots else None,
            "ai_shot_ratio": round(1 - len(real) / len(shots), 3) if shots else None,
        },
    }


# ------------------------------------------------------------------ 选型结论（F1-10）
def selection_report(db: Session, fmt: str = "md") -> tuple[str, str]:
    """导出选型轮明细与汇总结论。只统计已揭晓的选型轮（与看板口径一致）。"""
    from .music import recompute_model_stats

    recompute_model_stats(db)
    models = list(db.execute(select(MusicModel).order_by(MusicModel.wins.desc())).scalars())
    rounds = list(db.execute(select(MusicRound).where(MusicRound.kind == "selection",
                                                      MusicRound.winner_candidate_id.is_not(None))
                             .order_by(MusicRound.selection_no)).scalars())
    detail = []
    for r in rounds:
        p = db.get(Project, r.project_id)
        win = db.get(MusicCandidate, r.winner_candidate_id)
        for c in db.execute(select(MusicCandidate).where(MusicCandidate.round_id == r.id)
                            .order_by(MusicCandidate.label)).scalars():
            rt = db.execute(select(Rating).where(Rating.candidate_id == c.id)).scalars().first()
            detail.append({"selection_no": r.selection_no, "project": p.name if p else "",
                           "label": c.label, "model": c.model_name, "variant": c.variant,
                           "diction": rt.diction if rt else "", "melody": rt.melody if rt else "",
                           "arrangement": rt.arrangement if rt else "",
                           "avg": round(rt.total / 3, 2) if rt else "",
                           "winner": "是" if win and win.id == c.id else "", "mock": "是" if c.is_mock else ""})
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["选型轮", "项目", "候选", "模型", "版本", "咬字", "旋律", "编曲", "均分", "胜出", "占位"])
        for d in detail:
            w.writerow([d["selection_no"], d["project"], d["label"], d["model"], d["variant"], d["diction"],
                        d["melody"], d["arrangement"], d["avg"], d["winner"], d["mock"]])
        return buf.getvalue(), "text/csv; charset=utf-8"

    n = len(rounds)
    ranked = [m for m in models if m.rounds]
    lines = ["# 音乐模型选型结论", "",
             f"- 统计口径：只计已揭晓的选型轮（盲听），共 **{n}** 轮；生产模式与占位结果不计入。",
             f"- PRD 要求 ≥ 6 轮后再下结论：{'已满足' if n >= 6 else f'**样本不足（还差 {6 - n} 轮）**，以下结论仅供参考'}。", "",
             "| 模型 | 参与轮数 | 胜出 | 胜率 | 均分（1–5） |", "|---|---|---|---|---|"]
    for m in ranked:
        rate = f"{m.wins / m.rounds:.0%}" if m.rounds else "—"
        lines.append(f"| {m.name} | {m.rounds} | {m.wins} | {rate} | {m.avg_score or '—'} |")
    if ranked:
        best = max(ranked, key=lambda m: (m.wins / m.rounds, m.avg_score))
        lines += ["", f"**当前领先：{best.name}**（按胜率，其次均分）。"]
    else:
        lines += ["", "还没有已揭晓的选型轮。"]
    lines += ["", "## 明细", "", "| 轮 | 项目 | 候选 | 模型 | 咬字 | 旋律 | 编曲 | 均分 | 胜出 |", "|---|---|---|---|---|---|---|---|---|"]
    for d in detail:
        lines.append(f"| {d['selection_no']} | {d['project']} | {d['label']} | {d['model']}{'（占位）' if d['mock'] else ''} | "
                     f"{d['diction']} | {d['melody']} | {d['arrangement']} | {d['avg']} | {d['winner']} |")
    return "\n".join(lines) + "\n", "text/markdown; charset=utf-8"
