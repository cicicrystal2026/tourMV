"""人工节点闸门（PRD V1.1 §4.2「闸门规则」）。

人工节点的「确认」由后端校验：不满足条件时 advance 接口拒绝推进，
前端按钮置灰并显示这里给出的原因。V1.0 只有前端按钮置灰，
接口本身照样能推进 —— 这正是「已交付项目还挂着 12 条未处理疑点」的成因。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import Brief, LyricCheck, LyricVersion, MusicCandidate, MusicRound, Project, Rating


@dataclass
class Gate:
    ok: bool = True
    reasons: list[str] = field(default_factory=list)

    def fail(self, msg: str) -> None:
        self.ok = False
        self.reasons.append(msg)

    def as_dict(self) -> dict:
        return {"ok": self.ok, "reasons": self.reasons}


def _latest_lyric(db: Session, project_id: str) -> LyricVersion | None:
    return db.execute(select(LyricVersion).where(LyricVersion.project_id == project_id)
                      .order_by(LyricVersion.version.desc()).limit(1)).scalar_one_or_none()


def check(db: Session, p: Project) -> Gate:
    g = Gate()
    if p.locked_at is not None:
        g.fail("项目已交付并锁定，只读；如需修改请「新建修订版」")
        return g
    st = p.status

    if st == "briefing":
        b = db.execute(select(Brief).where(Brief.project_id == p.id)).scalar_one_or_none()
        if not (p.destination or "").strip():
            g.fail("简报缺少目的地")
        if b is None or not (b.genre or "").strip():
            g.fail("简报缺少曲风")

    elif st == "lyric_review":
        ver = _latest_lyric(db, p.id)
        if ver is None:
            g.fail("还没有歌词")
        elif ver.source == "mock" and settings().llm_ready:
            # LLM 配了却失败（欠费 / Key 失效 / 限流）：模板歌词不能拿去花钱生曲
            from .models import AiCall

            last = db.execute(select(AiCall).where(
                AiCall.project_id == p.id, AiCall.kind == "llm", AiCall.status == "error")
                .order_by(AiCall.created_at.desc()).limit(1)).scalar_one_or_none()
            why = (last.error if last else "") or "原因未知"
            g.fail(f"当前歌词是模板占位，不是 AI 写的：写词模型调用失败（{why[:160]}）。"
                   "处理好后点「重新生成」")
        elif not ver.checks_done:
            g.fail("自动检查还没跑完")
        else:
            rows = list(db.execute(select(LyricCheck).where(
                LyricCheck.lyric_version_id == ver.id)).scalars())
            pending = [r for r in rows if r.status == "pending"]
            if pending:
                g.fail(f"还有 {len(pending)} 条疑点未处理（逐条「采纳」或「保留原文」）")
            kept_no_reason = [r for r in rows if r.status == "kept" and not (r.keep_reason or "").strip()]
            if kept_no_reason:
                g.fail(f"{len(kept_no_reason)} 条「保留原文」缺少理由")

    elif st == "music_review":
        sel = db.get(MusicCandidate, p.selected_candidate_id) if p.selected_candidate_id else None
        if sel is None:
            g.fail("还没有选定曲目")
        else:
            if sel.status != "done":
                g.fail("选定的候选还没生成完成")
            if sel.is_mock and not settings().allow_mock:
                g.fail("选定的是占位结果（模型未配置），不能作为交付曲目")
            r = db.get(MusicRound, sel.round_id) if sel.round_id else None
            if r is not None and r.kind == "selection":
                unrated = []
                for c in db.execute(select(MusicCandidate).where(
                        MusicCandidate.round_id == r.id, MusicCandidate.status == "done")).scalars():
                    if db.execute(select(Rating).where(Rating.candidate_id == c.id)).first() is None:
                        unrated.append(c.label)
                if unrated:
                    g.fail(f"选型轮还有候选未评分：{'、'.join(sorted(unrated))}")

    elif st == "storyboard_review":
        from .services import storyboard as storyboard_svc

        prog = storyboard_svc.progress(db, p.id)
        if storyboard_svc.is_stale(db, p.id):
            g.fail("歌词已更新，现有配图对应的是旧歌词，请重新出图")
        if not prog["total"]:
            g.fail("还没有逐句配图")
        elif not prog["all_approved"]:
            g.fail(f"还有 {prog['total'] - prog['approved']} 句配图未通过审核")
        if not settings().allow_mock:
            fake = [r.order for r in storyboard_svc.shots(db, p.id)
                    if r.provider in ("placeholder", "library_seed")]
            if fake:
                g.fail(f"第 {'、'.join(map(str, fake[:8]))} 句用的是占位 / 演示画面，生产环境不能进入卡点")

    elif st == "edit_review":
        from .models import Timeline, TimelineClip

        tl = db.execute(select(Timeline).where(Timeline.project_id == p.id)).scalar_one_or_none()
        if tl is None:
            g.fail("还没有时间线")
        else:
            clips = list(db.execute(select(TimelineClip).where(
                TimelineClip.timeline_id == tl.id)).scalars())
            if not clips:
                g.fail("时间线没有片段")
            empty = [c for c in clips if not c.asset_id]
            if empty:
                g.fail(f"{len(empty)} 个片段没有匹配镜头")
    return g
