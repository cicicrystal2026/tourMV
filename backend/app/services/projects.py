"""项目与状态机编排。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import state_machine as sm
from ..models import Brief, LyricVersion, MusicCandidate, Project, RenderJob, Timeline
from ..queue import enqueue


class ProjectError(Exception):
    pass


def create(db: Session, *, name: str, destination: str, type_: str = "sample",
           language: str = "zh", trend_card_ids: list[str] | None = None,
           mood: list[str] | None = None, genre: str = "", bpm_min: int = 70,
           bpm_max: int = 100, duration_s: int = 180, vocal: str = "female",
           style_prompt: str = "", forbidden: str = "") -> Project:
    p = Project(name=name, destination=destination, type=type_, language=language,
                status="briefing", status_note=sm.STATES["briefing"].enter_note)
    db.add(p)
    db.flush()
    db.add(Brief(project_id=p.id, mood=mood or [], genre=genre, bpm_min=bpm_min, bpm_max=bpm_max,
                 duration_s=duration_s, vocal=vocal, style_prompt=style_prompt,
                 trend_card_ids=trend_card_ids or [], forbidden=forbidden))
    db.commit()
    db.refresh(p)
    return p


def get(db: Session, project_id: str) -> Project:
    p = db.get(Project, project_id)
    if p is None:
        raise ProjectError(f"项目不存在：{project_id}")
    return p


def brief_of(db: Session, project_id: str) -> Brief | None:
    return db.execute(select(Brief).where(Brief.project_id == project_id)).scalar_one_or_none()


def set_state(db: Session, project: Project, state: str, note: str | None = None) -> Project:
    project.status = state
    project.status_note = note if note is not None else sm.spec(state).enter_note
    db.commit()
    return project


def advance(db: Session, project_id: str, payload: dict | None = None) -> dict:
    """人工节点确认：推进到下一状态，并按状态机约定派发对应的后台任务。

    payload 用于把界面上的选择（如勾选的模型、每模型版数）带进被派发的任务，
    避免"确认歌词"与"批量生成"两条路径重复触发。
    """
    p = get(db, project_id)
    nxt = sm.next_state(p.status)
    if nxt is None:
        raise ProjectError(f"项目已处于终态「{sm.STATES[p.status].label}」，无法继续推进")

    # 配图闸门：没审完不许往下走 —— 否则会拿没确认的图去卡点合成，
    # 等于白跑一次渲染（渲染是这条链路上最贵的一步）。
    if p.status == "storyboard_review" and not (payload or {}).get("force"):
        from . import storyboard as storyboard_svc

        prog = storyboard_svc.progress(db, p.id)
        if prog["total"] and not prog["all_approved"]:
            raise ProjectError(
                f"还有 {prog['total'] - prog['approved']} 句配图未通过审核，"
                "全部确认后才能进入卡点合成"
            )

    dispatched = _dispatch(db, p, nxt, payload or {})
    set_state(db, p, nxt)
    return {"status": p.status, "dispatched": dispatched}


def _dispatch(db: Session, p: Project, target_state: str, payload: dict | None = None) -> str:
    """进入某状态时需要启动的后台工作。"""
    payload = payload or {}
    if target_state == "lyric_generating":
        enqueue(db, "lyric_generate", project_id=p.id, ref_id=p.id, name="生成歌词")
        return "lyric_generate"
    if target_state == "music_generating":
        enqueue(db, "music_generate", project_id=p.id, ref_id=p.id, name="多模型批量生曲",
                payload={"models": payload.get("models") or [],
                         "variants": int(payload.get("variants") or 2)})
        return "music_generate"
    if target_state == "storyboard_generating":
        # 逐句配图：提炼景区 → 出图 → 等人工审核。「配图审核」闸门在界面里做，
        # 任务只负责把图准备好。
        enqueue(db, "storyboard_generate", queue="gpu", project_id=p.id, ref_id=p.id,
                name="逐句生成景区配图",
                payload={"provider": (payload or {}).get("provider") or ""})
        return "storyboard_generate"
    if target_state == "audio_analyzing":
        enqueue(db, "audio_analyze", queue="gpu", project_id=p.id, ref_id=p.id,
                name="节拍检测与歌词对齐")
        return "audio_analyze"
    if target_state == "rendering":
        enqueue(db, "render_all", queue="gpu", project_id=p.id, ref_id=p.id, name="多版本合成",
                payload={"variants": payload.get("variants") or []})
        return "render_all"
    return ""


def reject(db: Session, project_id: str, target: str) -> dict:
    p = get(db, project_id)
    sm.assert_can_reject(p.status, target)
    p.reject_count += 1
    p.selected_candidate_id = None if target in ("music_generating", "lyric_review") else p.selected_candidate_id
    set_state(db, p, target, f"被驳回返工（第 {p.reject_count} 次）")
    return {"status": p.status, "reject_count": p.reject_count}


def detail(db: Session, project_id: str) -> dict:
    p = get(db, project_id)
    b = brief_of(db, p.id)
    lyric = db.execute(
        select(LyricVersion).where(LyricVersion.project_id == p.id)
        .order_by(LyricVersion.version.desc()).limit(1)
    ).scalar_one_or_none()
    tl = db.execute(select(Timeline).where(Timeline.project_id == p.id)).scalar_one_or_none()
    renders = list(db.execute(select(RenderJob).where(RenderJob.project_id == p.id)).scalars())
    cands = list(db.execute(select(MusicCandidate).where(MusicCandidate.project_id == p.id)).scalars())
    return {
        "project": p,
        "brief": b,
        "lyric": lyric,
        "timeline": tl,
        "renders": renders,
        "candidate_count": len(cands),
        "state": {
            "key": p.status,
            "label": sm.STATES[p.status].label,
            "column": sm.column_of(p.status),
            "human": sm.is_human(p.status),
            "next": sm.next_state(p.status),
            "reject_targets": sm.REJECT_TARGETS.get(p.status, []),
        },
    }


def list_projects(db: Session, status: str | None = None, limit: int = 200) -> list[Project]:
    stmt = select(Project).order_by(Project.updated_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Project.status == status)
    return list(db.execute(stmt).scalars())
