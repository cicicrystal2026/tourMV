"""后台任务处理器。所有耗时/外部调用都在这里发生，API 层只负责入队。"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import state_machine as sm
from ..adapters import align, llm, media
from ..adapters.music import base as music_base
from ..config import settings
from ..models import Timeline, Brief, Job, LyricVersion, MusicCandidate, MusicModel, Project, RenderJob
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
    from ..services import metrics

    metrics.on_transition(db, project_id, expected, target)
    p.status = target
    p.status_note = sm.spec(target).enter_note
    if target == "delivered":
        # 交付即锁定（PRD V1.1 §4.2）：之后只能「新建修订版」
        p.locked_at = datetime.now(timezone.utc)
    db.commit()
    return True


# ------------------------------------------------------------------ 歌词
@handler("lyric_generate")
def h_lyric_generate(ctx: JobContext) -> None:
    ctx.progress(10, "检索简报与素材卡")
    pid = ctx.job().project_id
    try:
        ver, real = lyrics_svc.generate(ctx.db, pid)
    except Exception as e:
        # 写词失败不能让项目卡在「写词中」：退回简报阶段并写明原因，用户可直接重新确认
        ctx.db.rollback()
        p = ctx.db.get(Project, pid)
        if p is not None and p.status == "lyric_generating":
            p.status, p.status_note = "briefing", f"写词失败，已退回简报：{e}"[:300]
            ctx.db.commit()
        raise
    ctx.progress(90, "押韵 / 字数 / 史实检查")
    ctx.set_result({"lyric_version_id": ver.id, "version": ver.version, "llm": real})
    _advance_if(ctx.db, ctx.job().project_id, "lyric_generating", "lyric_review")


@handler("lyric_check")
def h_lyric_check(ctx: JobContext) -> None:
    """只跑自动检查（修订版复制歌词后用）。"""
    ver = lyrics_svc.latest(ctx.db, ctx.job().project_id)
    if ver is None:
        raise RuntimeError("没有歌词版本")
    ctx.progress(30, "押韵 / 字数 / 史实检查")
    rows = lyrics_svc.run_checks(ctx.db, ver)
    p = ctx.db.get(Project, ver.project_id)
    carried = 0
    if p is not None and p.parent_project_id and ver.version == 1:
        # 修订版：原项目对同一句同一类疑点已经写过保留理由的，直接沿用（理由前标明出处），
        # 不让运营对没改过的歌词再逐条点一遍（E2E：修订 2 要重新处理 27 条）
        src = lyrics_svc.latest(ctx.db, p.parent_project_id)
        if src is not None:
            carried = lyrics_svc._carry_decisions(ctx.db, src, ver, prefix="沿用原项目的决定：")
    if p is not None and p.status == "lyric_review":
        p.status_note = sm.spec("lyric_review").enter_note
        ctx.db.commit()
    ctx.set_result({"lyric_version_id": ver.id, "issues": len(rows), "carried": carried})


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
    mode = job.payload.get("mode") or "production"
    if not models:
        raise RuntimeError("没有启用任何音乐模型")
    ctx.progress(20, f"派发 {len(models)} 个模型 × {variants} 版")
    out = music_svc.generate(db, pid, models, variants=variants, mode=mode)
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
    # 用候选创建时绑定的歌词版本，而不是「最新版本」——两者在生成期间可能已经不同
    ver = (db.get(LyricVersion, cand.lyric_version_id) if cand.lyric_version_id else None) or db.execute(
        select(LyricVersion).where(LyricVersion.project_id == cand.project_id)
        .order_by(LyricVersion.version.desc()).limit(1)
    ).scalar_one_or_none()
    # 选型轮盲听：任务名 / 进度 / 报错都会出现在公开的任务队列里，一律不带模型名
    blind = bool(job.payload.get("blind"))
    who = f"候选 {cand.label}" if blind else f"{cand.model_name} 第 {cand.variant} 版"

    cand.status = "running"
    db.commit()
    ctx.progress(10, f"{who} 生成中")

    adapter = music_base.resolve(cand.model_key)
    if adapter.key == "mock":
        if not settings().allow_mock:
            # 兜底：生成入口已拦过一次，这里防的是排队期间 Key 被撤掉
            cand.status, cand.error = "failed", "模型未配置，生产环境禁用占位结果"
            db.commit()
            _maybe_finish_round(ctx)
            raise RuntimeError(f"{who}：模型未配置，已拒绝生成占位结果")
        cand.is_mock = True
        db.commit()
    lyrics_text = "\n".join(
        f"[{s.get('name')}]\n" + "\n".join(s.get("lines") or []) for s in (ver.sections if ver else [])
    )
    req = music_base.GenRequest(
        lyrics=lyrics_text,
        style_prompt=(job.payload.get("style_prompt") or (brief.style_prompt if brief else "")),
        duration_s=int((brief.duration_s if brief else 0) or settings().song_default_seconds),
        vocal=(brief.vocal if brief else "female"),
        seed=abs(hash(f"{cand.id}{cand.variant}")) % 10**6,
        title=proj.name if proj else "",
    )

    t0 = time.time()

    def _poll(jid_: str) -> "music_base.GenResult":
        r = adapter.get_result(jid_)
        waited = 0.0
        while r.status == "running" and waited < GEN_TIMEOUT_S:
            time.sleep(3)
            waited += 3
            ctx.progress(min(85, 20 + int(waited / max(1, 60))), f"{who} 生成中 {int(waited)}s")
            r = adapter.get_result(jid_)
        return r

    try:
        # 续跑优先：上一次执行已经把任务提交给上游了（很可能已经计费），
        # 这里只把请求重新绑上再继续轮询，**绝不重新提交**。
        # 判据是落库的 provider_job_id —— 它在提交成功后立即写盘，
        # 所以即使进程重启、换 worker 线程，也能接上原来的任务。
        resumed = bool(cand.provider_job_id) and not cand.is_mock
        if resumed:
            jid = cand.provider_job_id
            adapter.attach_request(jid, req)
            ctx.progress(15, f"{who} 续跑（复用上游任务，不重复计费）")
        else:
            jid = adapter.generate(req)
            cand.provider_job_id = jid
            db.commit()          # 立刻落库：迟一步就可能在重试时重提交、重复计费
        try:
            result = _poll(jid)
        except Exception:
            # 续跑时上游任务号已失效（过期/被上游清理）→ 清掉它，重提交一次兜底。
            # 首次提交失败则原样抛出，避免误吞真实错误。
            if not resumed:
                raise
            log.warning("上游任务 %s 查询失败，改为重新提交", jid)
            cand.provider_job_id = ""
            db.commit()
            jid = adapter.generate(req)
            cand.provider_job_id = jid
            db.commit()
            result = _poll(jid)
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
        # 清掉上一次失败留下的报错：否则候选明明已经出片、卡片上却还挂着
        # 「ReadTimeout」之类的旧错误，用户会以为改坏了（线上真发生过）。
        cand.error = ""
        db.commit()

        # 匹配分只依据客观参数打分。V1.0 把模型名也喂给了 LLM —— 既可能带入品牌偏好，
        # 理由里还可能直接写出模型名，在盲听时泄露来源。占位结果不打分（F2-17）。
        if not cand.is_mock:
            brief_d = {"genre": brief.genre if brief else "", "mood": brief.mood if brief else [],
                       "bpm_min": brief.bpm_min if brief else 70,
                       "bpm_max": brief.bpm_max if brief else 100,
                       "duration_s": brief.duration_s if brief else settings().song_default_seconds,
                       "vocal": brief.vocal if brief else ""}
            cand_d = {"model_name": "", "bpm": cand.bpm, "duration_s": cand.duration_s,
                      "vocal": cand.vocal}
            pay, _real = llm.json_chat(
                prompts.fit_messages(brief_d, cand_d),
                fallback=prompts.fit_fallback(brief_d, cand_d),
                purpose="fit_score", project_id=cand.project_id,
            )
            cand.fit_score = int(pay.get("score") or 0)
            cand.fit_reason = pay.get("reason", "")
        if proj:
            proj.total_cost = round((proj.total_cost or 0) + cand.cost, 4)
        db.commit()
        ctx.progress(95, f"{who} 完成")
    except Exception as e:
        cand.status = "failed"
        cand.error = f"{type(e).__name__}: {e}"[:1000]
        db.commit()
        # 失败也要判一次本轮是否收尾，否则最后一个候选失败时项目会卡在 music_generating
        _maybe_finish_round(ctx)
        if blind:
            # 真实报错（含厂商名）留在候选上，仅在揭晓后可见；队列里只记中性信息
            raise RuntimeError(f"{who} 生成失败") from None
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


@handler("render_preview")
def h_render_preview(ctx: JobContext) -> None:
    """F3-7 720p 内部评审预览：不改项目状态，不算交付。"""
    import time as _t

    t0 = _t.time()
    job = render_svc.render_variant(ctx.db, ctx.job().project_id, render_svc.PREVIEW_VARIANT,
                                    progress=lambda v: ctx.progress(v, "导出 720p 预览"))
    if job.status != "done":
        raise RuntimeError(job.error or "预览导出失败")
    ctx.set_result({"elapsed_s": round(_t.time() - t0, 1), "size_mb": job.size_mb})


@handler("retag_assets")
def h_retag_assets(ctx: JobContext) -> None:
    from ..services import assets as assets_svc

    def prog(n: int, total: int) -> None:
        ctx.progress(int(100 * n / max(1, total)), f"自动打标 {n}/{total}")

    ctx.set_result(assets_svc.retag_missing(ctx.db, limit=int((ctx.job().payload or {}).get("limit") or 0) or None,
                                            on_progress=prog))


# ------------------------------------------------------------------ 素材向量重建
@handler("reembed_assets")
def h_reembed_assets(ctx: JobContext) -> None:
    from ..services import assets as assets_svc

    def prog(n: int, total: int) -> None:
        ctx.progress(int(100 * n / max(1, total)), f"重建素材向量 {n}/{total}")

    ctx.set_result(assets_svc.reembed_stale(ctx.db, on_progress=prog))


# ------------------------------------------------------------------ 剪辑
@handler("audio_analyze")
def h_audio_analyze(ctx: JobContext) -> None:
    pid = ctx.job().project_id
    ctx.progress(15, "节拍与强拍检测")
    from ..adapters.align import AlignError

    try:
        tl = edit_svc.analyze(ctx.db, pid, allow_estimate=bool((ctx.job().payload or {}).get("allow_estimate")))
    except AlignError as e:
        p = ctx.db.get(Project, pid)
        if p is not None and p.status == "audio_analyzing":
            p.status_note = f"对齐失败，等待处理：{e}"[:300]
            ctx.db.commit()
        raise
    ctx.progress(70, "歌词逐句对齐")
    ctx.set_result({"bpm": tl.bpm, "lines": len(tl.lyric_lines or []), "duration_s": tl.duration_s})
    if _advance_if(ctx.db, pid, "audio_analyzing", "rough_cutting"):
        enqueue(ctx.db, "rough_cut", queue="gpu", project_id=pid, ref_id=pid, name="镜头匹配与卡点")
    elif (ctx.job().payload or {}).get("then_cut"):
        enqueue(ctx.db, "rough_cut", queue="gpu", project_id=pid, ref_id=pid, name="按新时间轴重新卡点")


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
    # 「短版 2:30–2:50」要求整首歌至少 150 秒：歌不够长时跳过，不出一个和竖版几乎一样的重复版本
    tl0 = ctx.db.execute(select(Timeline).where(Timeline.project_id == ctx.job().project_id)).scalar_one_or_none()
    if tl0 is not None and float(tl0.duration_s or 0) < render_svc.SHORT_MIN_S and len(variants) > 1:
        variants = [v for v in variants if v != "short"]
    done, failed, qc_failed = [], [], []
    for i, v in enumerate(variants):
        base = int(100 * i / len(variants))
        ctx.progress(base, f"合成 {render_svc.VARIANTS[v]['label']}")
        job = render_svc.render_variant(
            ctx.db, pid, v,
            progress=lambda p, b=base: ctx.progress(min(99, b + int(p * 0.2)), None),
        )
        (done if job.status == "done" else failed).append(v)
        if job.status == "done" and not (job.qc or {}).get("ok", True):
            bad = "、".join(c["name"] for c in job.qc.get("checks", []) if not c["ok"])
            qc_failed.append(f"{render_svc.VARIANTS[v]['label']}：{bad}")
    result = {"done": done, "failed": failed, "qc_failed": qc_failed}
    if failed or qc_failed:
        # 合成失败或出片自检不通过：不交付，退回「待精剪」并写明原因（PRD V1.1 F3-10），
        # 而不是停在「合成中」让人以为还在跑
        p = ctx.db.get(Project, pid)
        if p is not None and p.status == "rendering":
            reason = "；".join(([f"合成失败：{'、'.join(failed)}"] if failed else []) + qc_failed)
            p.status, p.status_note = "edit_review", f"出片未通过：{reason}"[:300]
            ctx.db.commit()
        raise RuntimeError(f"出片未通过：failed={failed} qc={qc_failed}")
    # 全部版本合成成功且自检通过才推进到「已交付」。
    # 顺手出封面（横版 + 竖版）：底图取第一句歌词的景区配图，标题用项目名。
    # 封面是交付物料、不是闸门 —— 生成失败只记日志，不阻塞交付。
    try:
        covers = render_svc.render_covers(ctx.db, pid,
                                          progress=lambda p: ctx.progress(p, None))
        ok_covers = [c.variant for c in covers if c.status == "done"]
        if ok_covers:
            result["covers"] = ok_covers
    except Exception as e:      # noqa: BLE001
        log.warning("封面生成失败（不影响交付）：%s", e)
    ctx.set_result(result)
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
