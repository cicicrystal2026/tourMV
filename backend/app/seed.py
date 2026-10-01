"""一键初始化：建表 + 音乐模型 + 示例素材卡 + 占位素材 + 示例项目。

    cd backend
    python -m app.seed            # 只补齐缺失的数据
    python -m app.seed --reset    # 清空业务数据后重建（不删 data/ 下的媒体文件）
    python -m app.seed --run      # 额外同步跑通一支样片（生成歌词 + 三模型生曲 + 粗剪 + 合成）
"""
from __future__ import annotations

import argparse
import logging
import time

from sqlalchemy import delete, select

from .config import settings
from .db import init_db, session_scope
from .models import (
    Job,
    LyricCheck,
    LyricVersion,
    MusicCandidate,
    Project,
    Rating,
    RenderJob,
    Timeline,
    TimelineClip,
)
from .services import assets as assets_svc
from .services import edit as edit_svc
from .services import lyrics as lyrics_svc
from .services import music as music_svc
from .services import projects as projects_svc
from .services import render as render_svc
from .services import trends as trends_svc

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("seed")

DEMO_PROJECTS = [
    {"name": "桂林漓江·晨雾", "destination": "桂林·漓江", "genre": "ambient pop",
     "mood": ["治愈", "宁静"], "bpm_min": 70, "bpm_max": 84, "duration_s": 180,
     "type": "sample", "vocal": "female"},
    {"name": "杭州西湖·晚照", "destination": "杭州·西湖", "genre": "city pop revival",
     "mood": ["怀旧", "热闹"], "bpm_min": 92, "bpm_max": 104, "duration_s": 170,
     "type": "sample", "vocal": "female"},
    {"name": "长白山·雪原", "destination": "吉林·长白山", "genre": "cinematic folk",
     "mood": ["壮阔", "苍凉"], "bpm_min": 64, "bpm_max": 78, "duration_s": 195,
     "type": "demo", "vocal": "male"},
]


def reset(db) -> None:
    for model in (LyricVersion, MusicCandidate, Timeline, RenderJob, Project):
        for row in db.execute(select(model)).scalars().all():
            db.delete(row)
    db.commit()
    log.info("已清空项目相关数据")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="清空项目/歌词/候选/时间线/成片后重建")
    ap.add_argument("--run", action="store_true", help="同步跑通一支样片（不依赖后台 worker）")
    ap.add_argument("--no-assets", action="store_true", help="跳过占位素材生成")
    a = ap.parse_args()

    s = settings()
    s.ensure_dirs()
    init_db()
    log.info("数据目录：%s", s.data_dir)

    with session_scope() as db:
        if a.reset:
            reset(db)
        music_svc.ensure_models(db)
        log.info("音乐模型就绪：%s", [m["key"] for m in music_svc.list_models(db)])

        if not a.no_assets:
            t0 = time.time()
            r = assets_svc.seed_demo_assets(db)
            log.info("占位素材：%s（%.1fs）", r, time.time() - t0)

        t0 = time.time()
        r = trends_svc.seed_demo(db)
        log.info("示例素材卡：%s（%.1fs）", r, time.time() - t0)
        try:
            r = trends_svc.sync_from_monitor(db)
            log.info("已从 monitor.py 的 trends.db 同步：%s", r)
        except FileNotFoundError as e:
            log.info("未接入真实热歌数据（%s）", e)

        existing = {p.name for p in db.execute(select(Project)).scalars()}
        for spec in DEMO_PROJECTS:
            if spec["name"] in existing:
                continue
            cards = trends_svc.list_cards(db)[:2]
            p = projects_svc.create(
                db, name=spec["name"], destination=spec["destination"], type_=spec["type"],
                trend_card_ids=[c.id for c in cards], mood=spec["mood"], genre=spec["genre"],
                bpm_min=spec["bpm_min"], bpm_max=spec["bpm_max"], duration_s=spec["duration_s"],
                vocal=spec["vocal"],
                style_prompt=(cards[0].style_prompt if cards else ""),
            )
            log.info("示例项目：%s（%s）", p.name, p.status)

        if not a.run:
            log.info("初始化完成。加 --run 可同步跑通一支样片（含真实音频与 mp4 成片）。")
            return

        from . import queue as q
        from .workers import tasks  # noqa: F401  注册处理器

        first = db.execute(select(Project).order_by(Project.created_at)).scalars().first()
        if first is None:
            return
        # 重跑时清掉旧产物，保证可重复。必须按外键依赖顺序删：
        #   rating -> music_candidate，timeline_clip -> timeline
        # 用 bulk DELETE 而非 ORM delete()：ORM 会把删除攒到 commit 时由 unit-of-work
        # 自行排序，顺序不可控，会撞上外键约束；bulk DELETE 立即执行、顺序确定。
        cand_ids = [c.id for c in db.execute(
            select(MusicCandidate).where(MusicCandidate.project_id == first.id)).scalars().all()]
        if cand_ids:
            db.execute(delete(Rating).where(Rating.candidate_id.in_(cand_ids)))
        tl_ids = [t.id for t in db.execute(
            select(Timeline).where(Timeline.project_id == first.id)).scalars().all()]
        if tl_ids:
            db.execute(delete(TimelineClip).where(TimelineClip.timeline_id.in_(tl_ids)))
        db.execute(delete(RenderJob).where(RenderJob.project_id == first.id))
        db.execute(delete(MusicCandidate).where(MusicCandidate.project_id == first.id))
        db.execute(delete(Timeline).where(Timeline.project_id == first.id))
        lv_ids = [v.id for v in db.execute(
            select(LyricVersion).where(LyricVersion.project_id == first.id)).scalars().all()]
        if lv_ids:
            db.execute(delete(LyricCheck).where(LyricCheck.lyric_version_id.in_(lv_ids)))
        db.execute(delete(LyricVersion).where(LyricVersion.project_id == first.id))
        # 清掉该项目的历史任务，避免 _drain 捡到上一轮遗留的 queued 任务
        db.execute(delete(Job).where(Job.project_id == first.id))
        # 关键：必须同时把项目状态拨回起点，否则重跑时 advance 会从上一轮残留的
        # 状态出发（例如停在 edit_review 时会直接跳到 rendering），派发错误的任务。
        first.status = "briefing"
        first.status_note = ""
        first.selected_candidate_id = None
        first.reject_count = 0
        first.total_cost = 0.0
        db.commit()

        log.info("开始跑通样片：%s（%s）", first.name, first.destination)
        projects_svc.advance(db, first.id)      # briefing -> lyric_generating
        _drain(db)
        projects_svc.advance(db, first.id)      # lyric_review -> music_generating
        _drain(db)
        # 自动给全部候选打一个分，否则无法选定
        for c in db.execute(select(MusicCandidate).where(
                MusicCandidate.project_id == first.id, MusicCandidate.status == "done")).scalars().all():
            music_svc.rate(db, c.id, diction=4, melody=4, arrangement=4)
        best = db.execute(
            select(MusicCandidate).where(MusicCandidate.project_id == first.id,
                                         MusicCandidate.status == "done")
        ).scalars().first()
        if best is None:
            log.error("没有生成成功的候选，停止")
            return
        music_svc.select(db, first.id, best.id)
        projects_svc.advance(db, first.id)      # music_review -> audio_analyzing
        _drain(db)
        projects_svc.advance(db, first.id)      # edit_review -> rendering
        _drain(db)

        p = projects_svc.get(db, first.id)
        renders = db.execute(select(RenderJob).where(RenderJob.project_id == first.id)).scalars().all()
        log.info("样片状态：%s（%s）", p.status, p.name)
        for r in renders:
            log.info("  成片 %-11s %-6s %s", r.variant, r.status, r.output_path or r.error[:80])
        log.info("累计 API 费用：%.2f 元", p.total_cost)


def _drain(db) -> None:
    """同步把队列跑空（仅用于 seed --run 的自检，不参与线上运行）。"""
    from datetime import datetime, timezone

    from . import queue as q
    from .models import Job

    while True:
        job = db.execute(
            select(Job).where(Job.status == "queued").order_by(Job.created_at).limit(1)
        ).scalar_one_or_none()
        if job is None:
            return
        job.status = "running"
        job.started_at = datetime.now(timezone.utc)
        db.commit()
        fn = q._HANDLERS.get(job.type)
        if fn is None:
            job.status = "failed"
            job.error = f"未注册的任务类型：{job.type}"
            db.commit()
            continue
        ctx = q.JobContext(job.id, db)
        try:
            fn(ctx)
            j = ctx.job()
            j.status = "done"
            j.progress = 100
            j.finished_at = datetime.now(timezone.utc)
        except Exception as e:  # noqa: BLE001
            j = ctx.job()
            j.status = "failed"
            j.error = f"{type(e).__name__}: {e}"[:1500]
            log.warning("任务失败 %s: %s", job.type, e)
        db.commit()


if __name__ == "__main__":
    main()
