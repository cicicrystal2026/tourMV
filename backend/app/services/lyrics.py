"""歌词服务：生成 / 检查 / 编辑新版本 / 采纳改写建议 / 回滚。

按用户决定：不建知识库，史实核对直接交给 LLM（未配置 Key 时用本地规则兜底）。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import llm
from ..models import LyricCheck, LyricVersion, Project
from . import prompts
from .projects import ProjectError, get as get_project

METRIC_RE = re.compile(r"[\s,，。、！？!?；;：:—…·\-]")


def clean_len(line: str) -> int:
    return len(METRIC_RE.sub("", line or ""))


def flat_lines(sections: list[dict]) -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for s in sections or []:
        for i, ln in enumerate(s.get("lines", []) or [], 1):
            out.append((s.get("name", ""), i, ln))
    return out


def latest(db: Session, project_id: str) -> LyricVersion | None:
    return db.execute(
        select(LyricVersion).where(LyricVersion.project_id == project_id)
        .order_by(LyricVersion.version.desc()).limit(1)
    ).scalar_one_or_none()


def versions(db: Session, project_id: str) -> list[LyricVersion]:
    return list(db.execute(
        select(LyricVersion).where(LyricVersion.project_id == project_id)
        .order_by(LyricVersion.version.desc())
    ).scalars())


# ------------------------------------------------------------------ 生成
def generate(db: Session, project_id: str) -> tuple[LyricVersion, bool]:
    from ..models import Brief

    p = get_project(db, project_id)
    brief = db.execute(select(Brief).where(Brief.project_id == p.id)).scalar_one_or_none()
    mood = (brief.mood if brief else []) or []
    style = (brief.style_prompt if brief else "") or ""

    payload, real = llm.json_chat(
        prompts.lyrics_messages(
            p.destination, brief.genre if brief else "", mood,
            brief.duration_s if brief else 180, style,
            brief.forbidden if brief else "",
        ),
        fallback=prompts.lyrics_fallback(p.destination, mood),
        purpose="lyrics_generate", project_id=p.id,
    )
    sections = payload.get("sections") or prompts.lyrics_fallback(p.destination, mood)["sections"]

    prev = latest(db, p.id)
    ver = LyricVersion(project_id=p.id, version=(prev.version + 1) if prev else 1,
                       sections=sections, source="ai")
    db.add(ver)
    db.commit()
    db.refresh(ver)
    run_checks(db, ver, real=real)
    if not real:
        p.status_note = "歌词已生成（模拟数据：未配置 LLM）"
        db.commit()
    return ver, real


# ------------------------------------------------------------------ 检查
def run_checks(db: Session, ver: LyricVersion, real: bool = True) -> list[LyricCheck]:
    proj = db.get(Project, ver.project_id)
    destination = proj.destination if proj else ""

    payload, used_real = llm.json_chat(
        prompts.check_messages(ver.sections, destination),
        fallback=prompts.check_fallback(ver.sections, destination),
        purpose="lyrics_check", project_id=ver.project_id,
    )
    issues = payload.get("issues") or prompts.check_fallback(ver.sections, destination)["issues"]

    db.query(LyricCheck).filter(LyricCheck.lyric_version_id == ver.id).delete()
    db.commit()
    rows: list[LyricCheck] = []
    for it in issues:
        row = LyricCheck(
            lyric_version_id=ver.id, project_id=ver.project_id,
            type=it.get("type", "fact"), line_no=int(it.get("line_no") or 0),
            section=it.get("section", ""), text=it.get("text", ""),
            status=it.get("status", "pending"), evidence=it.get("evidence", ""),
            suggestions=it.get("suggestions") or [],
        )
        db.add(row)
        rows.append(row)
    db.commit()
    return rows


def checks_of(db: Session, lyric_version_id: str) -> list[LyricCheck]:
    return list(db.execute(
        select(LyricCheck).where(LyricCheck.lyric_version_id == lyric_version_id)
        .order_by(LyricCheck.type, LyricCheck.line_no)
    ).scalars())


def check_summary(rows: list[LyricCheck]) -> dict:
    pending = [r for r in rows if r.status == "pending"]
    by_type: dict[str, dict] = {}
    for t in ("rhyme", "meter", "fact"):
        grp = [r for r in rows if r.type == t]
        by_type[t] = {"total": len(grp), "pending": len([r for r in grp if r.status == "pending"])}
    return {"total": len(rows), "pending": len(pending), "by_type": by_type}


# ------------------------------------------------------------------ 编辑
def save_version(db: Session, project_id: str, sections: list[dict], source: str = "manual") -> LyricVersion:
    prev = latest(db, project_id)
    ver = LyricVersion(project_id=project_id, version=(prev.version + 1) if prev else 1,
                       sections=sections, source=source)
    db.add(ver)
    db.commit()
    db.refresh(ver)
    run_checks(db, ver)
    return ver


def rewrite(db: Session, project_id: str, targets: list[dict], instruction: str = "") -> LyricVersion:
    """targets: [{section, line_no}]，重写选中行，返回新版本。"""
    p = get_project(db, project_id)
    ver = latest(db, project_id)
    if ver is None:
        raise ProjectError("还没有歌词版本，先生成歌词")
    from ..models import Brief

    brief = db.execute(select(Brief).where(Brief.project_id == p.id)).scalar_one_or_none()
    want = {(t.get("section"), int(t.get("line_no") or 0)) for t in targets}
    picked: list[str] = []
    for s in ver.sections:
        for i, ln in enumerate(s.get("lines", []) or [], 1):
            if (s.get("name"), i) in want:
                picked.append(ln)
    if not picked:
        raise ProjectError("没有选中任何歌词行")

    payload, _real = llm.json_chat(
        prompts.rewrite_messages(picked, p.destination, brief.genre if brief else "", instruction),
        fallback=prompts.rewrite_fallback(picked),
        purpose="lyrics_rewrite", project_id=p.id,
    )
    new_lines = payload.get("lines") or prompts.rewrite_fallback(picked)["lines"]
    if len(new_lines) != len(picked):
        new_lines = (new_lines + picked)[: len(picked)]

    mapping = {tuple(k): v for k, v in zip([(t.get("section"), int(t.get("line_no") or 0)) for t in targets],
                                           new_lines)}
    sections = []
    ci = 0
    for s in ver.sections:
        lines = []
        for i, ln in enumerate(s.get("lines", []) or [], 1):
            key = (s.get("name"), i)
            if key in mapping:
                lines.append(mapping[key])
                ci += 1
            else:
                lines.append(ln)
        sections.append({"name": s.get("name"), "lines": lines})
    return save_version(db, project_id, sections, source="manual")


def adopt_suggestion(db: Session, check_id: str, index: int = 0) -> LyricVersion:
    """采纳某条改写建议：替换对应行，生成新版本并重新检查。"""
    c = db.get(LyricCheck, check_id)
    if c is None:
        raise ProjectError("检查项不存在")
    if not c.suggestions:
        raise ProjectError("该检查项没有可采纳的建议")
    new_text = c.suggestions[min(index, len(c.suggestions) - 1)]

    ver = db.get(LyricVersion, c.lyric_version_id)
    if ver is None:
        raise ProjectError("歌词版本不存在")
    sections = []
    for s in ver.sections:
        if s.get("name") != c.section:
            sections.append({"name": s.get("name"), "lines": list(s.get("lines", []))})
            continue
        lines = list(s.get("lines", []))
        if 1 <= c.line_no <= len(lines):
            lines[c.line_no - 1] = new_text
        sections.append({"name": s.get("name"), "lines": lines})
    return save_version(db, ver.project_id, sections, source="manual")


def resolve_check(db: Session, check_id: str) -> LyricCheck:
    c = db.get(LyricCheck, check_id)
    if c is None:
        raise ProjectError("检查项不存在")
    c.status = "resolved"
    db.commit()
    return c


def rollback(db: Session, project_id: str, version: int) -> LyricVersion:
    """回滚到历史版本 —— 生成新版本，不覆盖历史（PRD F2-6）。"""
    src = db.execute(
        select(LyricVersion).where(LyricVersion.project_id == project_id,
                                   LyricVersion.version == version)
    ).scalar_one_or_none()
    if src is None:
        raise ProjectError(f"版本 v{version} 不存在")
    return save_version(db, project_id, src.sections, source="manual")


def all_lines(ver: LyricVersion) -> list[str]:
    return [ln for _n, _i, ln in flat_lines(ver.sections)]
