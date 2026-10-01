"""后台任务处理器。所有耗时/外部调用都在这里发生，API 层只负责入队。"""
from __future__ import annotations

import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import state_machine as sm
from ..adapters import align, llm, media
from ..adapters.music import base as music_base
from ..config import settings
from ..models import Brief, Job, LyricVersion, MusicCandidate, MusicModel, Project, RenderJob
from ..queue import JobContext, enqueue, handler
from ..services import assets as assets_svc
from ..services import edit as edit_svc
from ..services import lyrics as lyrics_svc
from ..services import music as music_svc
from ..services import prompts
from ..services import render as render_svc
from ..services import storyboard as storyboard_svc
from ..services import trends as trends_svc

log = logging.getLogger("wenlv.tasks")

GEN_TIMEOUT_S = 900


def _advance_if(db: Session, project_id: str, expected: str, target: str) -> bool:
    p = db.get(Project, project_id)
    if p is None or p.status != expected:
        return False
    p.status = target
    p.status_note = sm.spec(target).enter_note
    db.commit()
    return True


# ------------------------------------------------------------------ 歌词
@handler("lyric_generate")
def h_lyric_generate(ctx: JobContext) -> None:
    ctx.progress(10, "检索简报与素材卡")
    ver, real = lyrics_svc.generate(ctx.db, ctx.job().project_id)
    ctx.progress(90, "押韵 / 字数 / 史实检查")
    ctx.set_result({"lyric_version_id": ver.id, "version": ver.version, "llm": real})
    _advance_if(ctx.db, ctx.job().project_id, "lyric_generating", "lyric_review")


# ------------------------------------------------------------------ 生曲
@handler("music_generate")
def h_music_generate(ctx: JobContext) -> None:
    pid = ctx.job().project_id
    db = ctx.db
    job = ctx.job()
    music_svc.ensure_models(db)
    # 默认只用一个模型生成一首，试听后由用户决定是否换模型再加 —— 省钱省 token。
    # 多模型并排对比仍然支持，但必须由用户在页面上显式勾选（payload.models）。
    # 约定见 .workbuddy/memory/MEMORY.md「音乐生成流程」。
    models = job.payload.get("models") or [
        m.key for m in db.execute(
            select(MusicModel).where(MusicModel.enabled.is_(True))
            .order_by(MusicModel.created_at)
        ).scalars()
    ][:1]
    variants = int(job.payload.get("variants") or 1)
    if not models:
        raise RuntimeError("没有启用任何音乐模型")
    ctx.progress(20, f"派发 {len(models)} 个模型 × {variants} 版")
    out = music_svc.generate(db, pid, models, variants=variants)
    ctx.set_result(out)


@handler("music_one")
def h_music_one(ctx: JobContext) -> None:
    db = ctx.db
    job = ctx.job()
    cand = db.get(MusicCandidate, job.payload.get("candidate_id", ""))
    if cand is None:
        raise RuntimeError("候选不存在")
    proj = db.get(Project, cand.project_id)
    brief = db.execute(select(Brief).where(Brief.project_id == cand.project_id)).scalar_one_or_none()
    ver = db.execute(
        select(LyricVersion).where(LyricVersion.project_id == cand.project_id)
        .order_by(LyricVersion.version.desc()).limit(1)
    ).scalar_one_or_none()

    cand.status = "running"
    db.commit()
    ctx.progress(10, f"{cand.model_name} 第 {cand.variant} 版生成中")

    adapter = music_base.resolve(cand.model_key)
    lyrics_text = "\n".join(
        f"[{s.get('name')}]\n" + "\n".join(s.get("lines") or []) for s in (ver.sections if ver else [])
    )
    req = music_base.GenRequest(
        lyrics=lyrics_text,
        style_prompt=(job.payload.get("style_prompt") or (brief.style_prompt if brief else "")),
        duration_s=int((brief.duration_s if brief else 180) or 180),
        vocal=(brief.vocal if brief else "female"),
        seed=abs(hash(f"{cand.id}{cand.variant}")) % 10**6,
        title=proj.name if proj else "",
    )

    t0 = time.time()
    try:
        jid = adapter.generate(req)
        result = adapter.get_result(jid)
        waited = 0.0
        while result.status == "running" and waited < GEN_TIMEOUT_S:
            time.sleep(3)
            waited += 3
            ctx.progress(min(85, 20 + int(waited / max(1, 60))), f"{adapter.name} 生成中 {int(waited)}s")
            result = adapter.get_result(jid)
        if result.status != "done" or not result.audio_path:
            raise RuntimeError(result.error or f"{adapter.name} 生成未完成")

        info = media.probe(result.audio_path)
        cand.audio_path = result.audio_path
        cand.bpm = float(result.bpm or 0) or _infer_bpm(result.audio_path)
        cand.duration_s = float(result.duration_s or info.get("duration_s") or 0)
        cand.vocal = req.vocal
        cand.cost = float(result.cost or adapter.estimate_cost(req))
        cand.elapsed_s = round(time.time() - t0, 2)
        cand.status = "done"
        db.commit()

        pay, _real = llm.json_chat(
            prompts.fit_messages(
                {"genre": brief.genre if brief else "", "mood": brief.mood if brief else [],
                 "bpm_min": brief.bpm_min if brief else 70, "bpm_max": brief.bpm_max if brief else 100,
                 "duration_s": brief.duration_s if brief else 180, "vocal": brief.vocal if brief else ""},
                {"model_name": cand.model_name, "bpm": cand.bpm, "duration_s": cand.duration_s,
                 "vocal": cand.vocal},
            ),
            fallback=prompts.fit_fallback(
                {"bpm_min": brief.bpm_min if brief else 70, "bpm_max": brief.bpm_max if brief else 100,
                 "duration_s": brief.duration_s if brief else 180, "vocal": brief.vocal if brief else ""},
                {"model_name": cand.model_name, "bpm": cand.bpm, "duration_s": cand.duration_s,
                 "vocal": cand.vocal},
            ),
            purpose="fit_score", project_id=cand.project_id,
        )
        cand.fit_score = int(pay.get("score") or 0)
        cand.fit_reason = pay.get("reason", "")
        if proj:
            proj.total_cost = round((proj.total_cost or 0) + cand.cost, 4)
        db.commit()
        ctx.progress(95, f"{cand.model_name} 第 {cand.variant} 版完成")
    except Exception as e:
        cand.status = "failed"
        cand.error = f"{type(e).__name__}: {e}"[:1000]
        db.commit()
        # 失败也要判一次本轮是否收尾，否则最后一个候选失败时项目会卡在 music_generating
        _maybe_finish_round(ctx)
        raise

    _maybe_finish_round(ctx)


def _infer_bpm(path: str) -> float:
    return float(align.detect_beats(path).get("bpm") or 0)


def _maybe_finish_round(ctx: JobContext) -> None:
    """本轮是否收尾：候选全部落定，且没有 music_one 任务还在排队/重试中。"""
    db = ctx.db
    cand = db.get(MusicCandidate, ctx.job().payload.get("candidate_id", ""))
    if cand is None:
        return
    pending_cand = db.execute(
        select(MusicCandidate).where(
            MusicCandidate.project_id == cand.project_id,
            MusicCandidate.round == cand.round,
            MusicCandidate.status.in_(("queued", "running")),
        )
    ).scalars().all()
    if pending_cand:
        return
    # 注意：本函数在 handler 内部被调用，此时「当前这个任务」的状态仍是 running
    # （队列要等 handler 返回后才回写 done），必须把它自己排除掉，否则永远收不了尾。
    me = ctx.job().id
    pending_job = db.execute(
        select(Job).where(
            Job.project_id == cand.project_id,
            Job.type == "music_one",
            Job.status.in_(("queued", "running")),
            Job.id != me,
        )
    ).scalars().all()
    if pending_job:
        return
    _advance_if(db, cand.project_id, "music_generating", "music_review")


# ------------------------------------------------------------------ 剪辑
@handler("audio_analyze")
def h_audio_analyze(ctx: JobContext) -> None:
    pid = ctx.job().project_id
    ctx.progress(15, "节拍与强拍检测")
    tl = edit_svc.analyze(ctx.db, pid)
    ctx.progress(70, "歌词逐句对齐")
    ctx.set_result({"bpm": tl.bpm, "lines": len(tl.lyric_lines or []), "duration_s": tl.duration_s})
    if _advance_if(ctx.db, pid, "audio_analyzing", "rough_cutting"):
        enqueue(ctx.db, "rough_cut", queue="gpu", project_id=pid, ref_id=pid, name="镜头匹配与卡点")


@handler("rough_cut")
def h_rough_cut(ctx: JobContext) -> None:
    pid = ctx.job().project_id
    ctx.progress(20, "LLM 生成画面描述")
    tl = edit_svc.auto_cut(ctx.db, pid)
    ctx.db.refresh(tl)
    clips = ctx.db.execute(select(edit_svc.TimelineClip).where(
        edit_svc.TimelineClip.timeline_id == tl.id)).scalars().all()
    ctx.set_result({"clips": len(clips), "version": tl.auto_version})
    _advance_if(ctx.db, pid, "rough_cutting", "edit_review")


@handler("resnap")
def h_resnap(ctx: JobContext) -> None:
    pid = ctx.job().project_id
    edit_svc.resnap(ctx.db, pid)
    ctx.set_result({"ok": True})


# ------------------------------------------------------------------ 逐句配图
@handler("storyboard_generate")
def h_storyboard_generate(ctx: JobContext) -> None:
    """提炼景区 → 并发出图 → 等人工审核。

    注意这里**不推进到下一状态**：出完图要停在 storyboard_review 等人工审核，
    全部通过由界面上的「确认配图」触发推进（见 api/storyboard.py）。
    """
    pid = ctx.job().project_id
    provider = (ctx.job().payload or {}).get("provider") or None
    ctx.progress(3, "按歌词提炼景区")
    plan = storyboard_svc.plan(ctx.db, pid)
    ctx.progress(8, f"逐句出图（{plan.get('created', 0)} 句）")

    def on_progress(n: int, total: int) -> None:
        ctx.progress(8 + int(88 * n / max(1, total)), f"出图 {n}/{total}")

    res = storyboard_svc.generate(ctx.db, pid, provider=provider, on_progress=on_progress)
    prog = storyboard_svc.progress(ctx.db, pid)
    ctx.set_result({"plan": plan.get("created"), "progress": prog, **res})
    if res.get("total") and res.get("ok") == 0:
        raise RuntimeError(f"配图全部失败：{(res.get('errors') or [{}])[0].get('error', '')}")
    # 出图完成 → 停在待审核
    _advance_if(ctx.db, pid, "storyboard_generating", "storyboard_review")


@handler("storyboard_generate_one")
def h_storyboard_generate_one(ctx: JobContext) -> None:
    """单条重生成（驳回后重做一张）。"""
    shot_id = ctx.job().ref_id
    res = storyboard_svc.regenerate(ctx.db, shot_id)
    ctx.set_result(res)


# ------------------------------------------------------------------ 合成
@handler("render_all")
def h_render_all(ctx: JobContext) -> None:
    pid = ctx.job().project_id
    variants = ctx.job().payload.get("variants") or list(render_svc.VARIANTS)
    variants = [v for v in variants if v in render_svc.VARIANTS] or list(render_svc.VARIANTS)
    done, failed = [], []
    for i, v in enumerate(variants):
        base = int(100 * i / len(variants))
        ctx.progress(base, f"合成 {render_svc.VARIANTS[v]['label']}")
        job = render_svc.render_variant(
            ctx.db, pid, v,
            progress=lambda p, b=base: ctx.progress(min(99, b + int(p * 0.2)), None),
        )
        (done if job.status == "done" else failed).append(v)
    ctx.set_result({"done": done, "failed": failed})
    if failed:
        raise RuntimeError(f"部分版本合成失败：{failed}")
    # 只有全部版本都合成成功才推进到「已交付」，避免失败也标记为交付完成
    _advance_if(ctx.db, pid, "rendering", "delivered")


@handler("asset_tag")
def h_asset_tag(ctx: JobContext) -> None:
    aid = ctx.job().ref_id
    ctx.progress(30, "抽帧与视觉打标")
    r = assets_svc.tag_asset(ctx.db, aid, hint=ctx.job().payload.get("hint", ""))
    ctx.set_result(r)


@handler("trend_sync")
def h_trend_sync(ctx: JobContext) -> None:
    days = int(ctx.job().payload.get("days", 30))
    top = int(ctx.job().payload.get("top", 10))
    try:
        r = trends_svc.sync_from_monitor(ctx.db, days=days, top=top)
    except FileNotFoundError as e:
        ctx.set_result({"error": str(e), "fallback": "已改用示例数据"})
        trends_svc.seed_demo(ctx.db)
        return
    ctx.set_result(r)
