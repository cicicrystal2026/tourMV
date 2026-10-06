"""项目与状态机编排。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import state_machine as sm
from ..models import Brief, LyricVersion, MusicCandidate, Project, RenderJob, Timeline
from ..queue import enqueue


class ProjectError(Exception):
    pass


class GateError(ProjectError):
    """闸门不满足：接口返回 409，并附带全部原因（前端逐条展示）。"""

    def __init__(self, reasons: list[str]):
        super().__init__("；".join(reasons))
        self.reasons = reasons


def ensure_editable(p: Project) -> None:
    """已交付项目只读（PRD V1.1 §4.2）。所有会改动项目内容的入口都要先过这里。"""
    if p.locked_at is not None:
        raise ProjectError("项目已交付并锁定，只读；如需修改请「新建修订版」")


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
    from . import metrics

    metrics.on_transition(db, p.id, "", "briefing")
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
    from . import metrics

    metrics.on_transition(db, project.id, project.status, state)   # 工时：进出人工节点
    project.status = state
    project.status_note = note if note is not None else sm.spec(state).enter_note
    db.commit()
    return project


def advance(db: Session, project_id: str, payload: dict | None = None) -> dict:
    """人工节点确认：推进到下一状态，并按状态机约定派发对应的后台任务。

    payload 用于把界面上的选择（如勾选的模型、每模型版数）带进被派发的任务，
    避免"确认歌词"与"批量生成"两条路径重复触发。
    """
    from .. import gates

    p = get(db, project_id)
    ensure_editable(p)
    nxt = sm.next_state(p.status)
    if nxt is None:
        raise ProjectError(f"项目已处于终态「{sm.STATES[p.status].label}」，无法继续推进")
    if not sm.is_human(p.status) and p.status != "briefing":
        raise ProjectError(f"「{sm.STATES[p.status].label}」由系统自动推进，请等待完成")

    # 闸门（PRD V1.1 §4.2）：不再提供 force 绕过 —— V1.0 正是靠前端置灰挡人，
    # 接口本身照样能推进，才出现「已交付项目还挂着未处理疑点」。
    g = gates.check(db, p)
    if not g.ok:
        raise GateError(g.reasons)

    # 先切状态再派发：派发出去的任务可能很快跑完并尝试「从 X 推进到下一步」，
    # 如果此时状态还没切过去，推进条件不成立，项目就会卡死在中间态。
    # 派发失败（如模型未配置、超预算）则回滚到原状态，错误原样抛给界面。
    prev, prev_note = p.status, p.status_note
    if p.status == "lyric_review" and _reusable_music(db, p) is not None:
        # 修订版、歌词未改：沿用原曲，跳过生曲，直接到「待选曲」确认（不花钱、不用再盲听）
        set_state(db, p, "music_review", "修订版沿用原曲（歌词未改），确认即可进入配图")
        db.refresh(p)
        return {"status": p.status, "dispatched": "reuse_music"}
    set_state(db, p, nxt)
    try:
        dispatched = _dispatch(db, p, nxt, payload or {})
    except Exception:
        db.rollback()
        p = get(db, project_id)
        set_state(db, p, prev, prev_note)
        raise
    db.refresh(p)
    return {"status": p.status, "dispatched": dispatched}


def _dispatch(db: Session, p: Project, target_state: str, payload: dict | None = None) -> str:
    """进入某状态时需要启动的后台工作。"""
    payload = payload or {}
    if target_state == "lyric_generating":
        enqueue(db, "lyric_generate", project_id=p.id, ref_id=p.id, name="生成歌词")
        return "lyric_generate"
    if target_state == "music_generating":
        mode = "selection" if payload.get("mode") == "selection" else "production"
        # 先在请求里校验并创建候选（模型未配置、超预算等错误要当场报给用户，
        # 而不是进了队列才在后台失败、项目卡在「生曲中」）
        from . import music as music_svc

        out = music_svc.generate(db, p.id, payload.get("models") or [],
                                 variants=int(payload.get("variants") or (2 if mode == "selection" else 1)),
                                 mode=mode)
        return f"music:{mode}:{out['created']}"
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
    ensure_editable(p)
    sm.assert_can_reject(p.status, target)
    p.reject_count += 1
    p.selected_candidate_id = None if target in ("music_generating", "lyric_review") else p.selected_candidate_id
    set_state(db, p, target, f"被驳回返工（第 {p.reject_count} 次）")
    return {"status": p.status, "reject_count": p.reject_count}


def revise(db: Session, project_id: str) -> Project:
    """已交付项目「新建修订版」：复制简报与最新歌词，原版本保持只读（PRD V1.1 §4.2）。

    新项目停在「待确认歌词」，并重跑一次自动检查 —— 修订的起点是重新过闸门，
    不继承原版本的检查结论。
    """
    from ..models import LyricVersion as LV

    src = get(db, project_id)
    if src.locked_at is None:
        raise ProjectError("只有已交付（锁定）的项目需要新建修订版，进行中的项目直接修改即可")
    # 修订版的修订版不叠后缀（V1.0 会出现「（修订1）（修订1）」）：按原始名统一编号
    import re

    base = re.sub(r"(（修订\d+）)+$", "", src.name)
    n = len(list(db.execute(select(Project).where(Project.name.like(f"{base}（修订%"))).scalars())) + 1
    p = Project(name=f"{base}（修订{n}）"[:200], destination=src.destination, type=src.type,
                language=src.language, subtitle_language=src.subtitle_language,
                status="briefing", status_note=sm.STATES["briefing"].enter_note,
                parent_project_id=src.id)
    db.add(p)
    db.flush()
    b = brief_of(db, src.id)
    if b is not None:
        db.add(Brief(project_id=p.id, mood=list(b.mood or []), genre=b.genre, bpm_min=b.bpm_min,
                     bpm_max=b.bpm_max, duration_s=b.duration_s, vocal=b.vocal,
                     style_prompt=b.style_prompt, trend_card_ids=list(b.trend_card_ids or []),
                     kb_refs=list(b.kb_refs or []), forbidden=b.forbidden))
    lv = db.execute(select(LV).where(LV.project_id == src.id).order_by(LV.version.desc())
                    .limit(1)).scalar_one_or_none()
    if lv is not None:
        new_lv = LV(project_id=p.id, version=1, sections=lv.sections, source="manual",
                    checks_done=False)
        db.add(new_lv)
        db.flush()
        p.status, p.status_note = "lyric_review", "修订版：已复制原歌词，正在重新检查"
        _copy_selected_music(db, src, p, new_lv.id)
        # 镜头也一并带过来（画面文件与素材资产复用，不重复花生图的钱），
        # 但状态退回「待审核」：修订版要重新过一遍镜头闸门。歌词若在修订中被改动，
        # 分镜会被判定为过期并按新歌词重新提炼（见 storyboard.is_stale）。
        from ..models import StoryboardShot as SS

        for r in db.execute(select(SS).where(SS.project_id == src.id).order_by(SS.order)).scalars():
            db.add(SS(project_id=p.id, order=r.order, section=r.section, line_no=r.line_no,
                      line_text=r.line_text, place=r.place, place_note=r.place_note, keywords=r.keywords,
                      prompt=r.prompt, provider=r.provider, model=r.model, image_path=r.image_path,
                      clip_path=r.clip_path, asset_id=r.asset_id, rev=r.rev, dup_of=r.dup_of,
                      ai_score=r.ai_score, ai_reason=r.ai_reason, suggest_asset_id=r.suggest_asset_id,
                      status="pending" if r.image_path else "draft",
                      note=f"修订版沿用原项目画面" if r.image_path else ""))
    from . import metrics

    metrics.on_transition(db, p.id, "", p.status)
    db.commit()
    db.refresh(p)
    if lv is not None:
        enqueue(db, "lyric_check", project_id=p.id, ref_id=p.id, name="押韵 / 字数 / 史实检查")
    return p


def _copy_selected_music(db: Session, src: Project, p: Project, lyric_version_id: str) -> None:
    """修订版沿用原项目选定的曲目（同一音频文件，不重新生成、不花钱）。

    绑定到修订版的第 1 版歌词：只要歌词没改，确认歌词时直接跳过生曲与选曲；
    歌词一改（产生新版本）就不再沿用 —— 旧曲唱的是旧词。
    """
    import uuid

    from ..models import MusicRound

    c = db.get(MusicCandidate, src.selected_candidate_id) if src.selected_candidate_id else None
    if c is None or c.status != "done" or not c.audio_path:
        return
    r = MusicRound(project_id=p.id, kind="production", round_no=1,
                   model_keys=[c.model_key], variants=1)
    db.add(r)
    db.flush()
    cp = MusicCandidate(project_id=p.id, lyric_version_id=lyric_version_id, round=1, round_id=r.id,
                        model_key=c.model_key, model_name=c.model_name, variant=1, label="A",
                        audio_path=c.audio_path, bpm=c.bpm, duration_s=c.duration_s, vocal=c.vocal,
                        fit_score=c.fit_score, fit_reason=c.fit_reason, status="done", cost=0.0,
                        elapsed_s=0.0, is_mock=c.is_mock, audio_token=uuid.uuid4().hex)
    db.add(cp)
    db.flush()
    r.winner_candidate_id = cp.id
    p.selected_candidate_id = cp.id


def _reusable_music(db: Session, p: Project) -> MusicCandidate | None:
    """修订版沿用的原曲是否仍可用：选定曲目绑定的正是当前最新歌词版本。"""
    c = db.get(MusicCandidate, p.selected_candidate_id) if p.selected_candidate_id else None
    if c is None or not c.lyric_version_id or not p.parent_project_id:
        return None
    lv = db.execute(select(LyricVersion).where(LyricVersion.project_id == p.id)
                    .order_by(LyricVersion.version.desc()).limit(1)).scalar_one_or_none()
    return c if lv is not None and lv.id == c.lyric_version_id else None


def detail(db: Session, project_id: str) -> dict:
    from .. import gates

    p = get(db, project_id)
    b = brief_of(db, p.id)
    lyric = db.execute(
        select(LyricVersion).where(LyricVersion.project_id == p.id)
        .order_by(LyricVersion.version.desc()).limit(1)
    ).scalar_one_or_none()
    tl = db.execute(select(Timeline).where(Timeline.project_id == p.id)).scalar_one_or_none()
    renders = list(db.execute(select(RenderJob).where(RenderJob.project_id == p.id,
                                                      RenderJob.variant != "preview")).scalars())
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
            "reject_targets": [] if p.locked_at else sm.REJECT_TARGETS.get(p.status, []),
        },
        # 当前人工节点能否推进、为什么不能 —— 前端据此置灰主按钮并逐条展示原因
        "gate": gates.check(db, p).as_dict(),
    }


def list_projects(db: Session, status: str | None = None, limit: int = 200) -> list[Project]:
    stmt = select(Project).order_by(Project.updated_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Project.status == status)
    return list(db.execute(stmt).scalars())
