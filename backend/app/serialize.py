"""模型 → 前端 JSON 的统一序列化（避免各路由重复拼装，也保证字段命名一致）。"""
from __future__ import annotations

from datetime import datetime, timezone

from . import state_machine as sm
from .models import (
    Asset,
    AiCall,
    Brief,
    Job,
    LyricCheck,
    LyricVersion,
    MusicCandidate,
    MusicModel,
    Project,
    PublishMetric,
    RenderJob,
    Timeline,
    TimelineClip,
    TrendCard,
)


def basename(path: str) -> str:
    """从库里存的媒体路径取出文件名。

    ⚠️ **不能用 `Path(path).name`** —— 库里存的是 **Windows 绝对路径**
    （`C:\\Users\\...\\data\\renders\\x.mp4`），而线上沙箱跑在 **Linux**：
    POSIX 的 `Path` 不把 `\\` 当分隔符，会把**整条路径**当成文件名，
    拼出来的 URL 变成 `/media/renders/C:\\Users\\...\\x.mp4`，
    浏览器一律 404 —— 表现为「线上视频/音频/缩略图全打不开」，
    而**本地 Windows 开发时完全正常**，所以只在线上暴露（这个 bug 真犯过）。

    统一手工切分，与操作系统无关。
    """
    return (path or "").replace("\\", "/").rsplit("/", 1)[-1]


def media_url(kind: str, path: str) -> str:
    if not path:
        return ""
    return f"/media/{kind}/{basename(path)}"


def dt(v):
    """时间戳统一按 UTC 输出成带时区的 ISO 8601。

    库里存的是 datetime.now(timezone.utc)，但 SQLite 不保留 tzinfo，读回来是 naive，
    直接 isoformat() 会得到没有时区标记的字符串，浏览器 new Date() 会当成本地时间解析，
    导致相对时间整体偏移一个时区（本项目实测差了 7 小时，出现「-22406 秒前」这种负数）。
    """
    if not v:
        return None
    if isinstance(v, datetime) and v.tzinfo is None:
        v = v.replace(tzinfo=timezone.utc)
    return v.isoformat()


def project(p: Project) -> dict:
    return {
        "id": p.id, "name": p.name, "destination": p.destination, "type": p.type,
        "language": p.language, "subtitle_language": p.subtitle_language,
        "status": p.status,
        "state_label": sm.STATES[p.status].label if p.status in sm.STATES else p.status,
        "column": sm.column_of(p.status) if p.status in sm.STATES else "",
        "waiting_human": sm.is_human(p.status),
        "status_note": p.status_note,
        "selected_candidate_id": p.selected_candidate_id,
        "total_cost": p.total_cost, "reject_count": p.reject_count,
        "reject_targets": sm.REJECT_TARGETS.get(p.status, []),
        "next_state": sm.next_state(p.status),
        "created_at": dt(p.created_at), "updated_at": dt(p.updated_at),
    }


def brief(b: Brief | None) -> dict | None:
    if b is None:
        return None
    return {
        "id": b.id, "mood": b.mood or [], "genre": b.genre,
        "bpm_min": b.bpm_min, "bpm_max": b.bpm_max, "duration_s": b.duration_s,
        "vocal": b.vocal, "style_prompt": b.style_prompt,
        "trend_card_ids": b.trend_card_ids or [], "kb_refs": b.kb_refs or [],
        "forbidden": b.forbidden,
    }


def lyric(v: LyricVersion | None) -> dict | None:
    if v is None:
        return None
    return {"id": v.id, "version": v.version, "sections": v.sections or [],
            "source": v.source, "created_at": dt(v.created_at)}


def check(c: LyricCheck) -> dict:
    return {"id": c.id, "type": c.type, "section": c.section, "line_no": c.line_no,
            "text": c.text, "status": c.status, "evidence": c.evidence,
            "suggestions": c.suggestions or []}


def model(m: MusicModel, *, capabilities: dict | None = None, effective: str = "",
          ready: bool = False) -> dict:
    return {"key": m.key, "name": m.name, "run_mode": m.run_mode,
            "enabled": m.enabled, "available": m.available,
            "wins": m.wins, "rounds": m.rounds, "avg_score": m.avg_score,
            "score_n": m.score_n, "note": m.note,
            "capabilities": capabilities or {}, "effective": effective, "ready": ready}


def candidate(c: MusicCandidate, rating: dict | None = None) -> dict:
    return {
        "id": c.id, "round": c.round, "label": c.label, "variant": c.variant,
        "model_key": c.model_key, "model_name": c.model_name,
        "status": c.status, "error": c.error,
        "audio_url": media_url("music", c.audio_path),
        "bpm": c.bpm, "duration_s": c.duration_s, "vocal": c.vocal,
        "fit_score": c.fit_score, "fit_reason": c.fit_reason,
        "cost": c.cost, "elapsed_s": c.elapsed_s, "rating": rating,
    }


def asset(a: Asset, tags: dict | None = None, score: float | None = None) -> dict:
    return {
        "id": a.id, "name": a.name, "destination": a.destination,
        "thumb_url": media_url("thumbs", a.thumb_path),
        "video_url": media_url("assets", a.file_path),
        "duration_s": a.duration_s, "width": a.width, "height": a.height, "fps": a.fps,
        "license_status": a.license_status, "licensor": a.licensor,
        "license_no": a.license_no, "scope": a.scope, "valid_until": a.valid_until,
        "portrait": a.portrait, "has_portrait": a.has_portrait, "usable": a.usable,
        "tag_confidence": a.tag_confidence, "tags": tags or {},
        "score": score,
    }


def timeline(t: Timeline) -> dict:
    meta = t.align_meta or {}
    return {
        "id": t.id, "audio_url": media_url("music", t.audio_path), "bpm": t.bpm,
        "duration_s": t.duration_s, "beats": t.beats or [],
        "strong_beats": (t.beats or [])[::4] if t.beats else [],
        "lyric_lines": t.lyric_lines or [], "auto_version": t.auto_version,
        # 歌词时间轴的来源：whisper=来自歌声，analytic=按字数估算
        "align_engine": meta.get("engine", ""),
        "align_note": meta.get("note", ""),
        "align_match_ratio": meta.get("match_ratio"),
        "align_anchored": meta.get("anchored"),
        "vocal_span": meta.get("vocal_span"),
    }


def clip(c: TimelineClip, a: Asset | None) -> dict:
    return {
        "id": c.id, "order": c.order, "asset_id": c.asset_id,
        "asset_name": a.name if a else "（未匹配）",
        "thumb_url": media_url("thumbs", a.thumb_path) if a else "",
        "video_url": media_url("assets", a.file_path) if a else "",
        "in_s": c.in_s, "out_s": c.out_s, "start_s": c.start_s, "end_s": c.end_s,
        "lyric_line": c.lyric_line, "scene_prompt": c.scene_prompt,
        "locked": c.locked, "manual": c.manual,
    }


def render_job(r: RenderJob) -> dict:
    return {"id": r.id, "variant": r.variant, "status": r.status, "progress": r.progress,
            "url": media_url("renders", r.output_path), "size_mb": r.size_mb,
            "error": r.error, "updated_at": dt(r.updated_at)}


def job(j: Job) -> dict:
    return {"id": j.id, "queue": j.queue, "type": j.type, "name": j.name,
            "status": j.status, "progress": j.progress, "vram_mb": j.vram_mb,
            "error": j.error, "retries": j.retries, "project_id": j.project_id,
            "result": j.result or {}, "created_at": dt(j.created_at),
            "started_at": dt(j.started_at), "finished_at": dt(j.finished_at)}


def trend_card(c: TrendCard) -> dict:
    return {"id": c.id, "video_id": c.video_id, "week": c.week, "rank": c.rank,
            "song": c.song, "artist": c.artist, "why_hot": c.why_hot,
            "elements": c.elements or {}, "remix": c.remix or {},
            "metrics": c.metrics or {}, "analyzed_at": c.analyzed_at}


def publish_metric(m: PublishMetric) -> dict:
    return {"id": m.id, "platform": m.platform, "url": m.url, "published_at": m.published_at,
            "views_7d": m.views_7d, "completion": m.completion,
            "engagement": m.engagement, "note": m.note}


def ai_call(c: AiCall) -> dict:
    return {"id": c.id, "kind": c.kind, "provider": c.provider, "model": c.model,
            "purpose": c.purpose, "status": c.status, "elapsed_s": c.elapsed_s,
            "cost": c.cost, "error": c.error, "project_id": c.project_id,
            "created_at": dt(c.created_at)}
