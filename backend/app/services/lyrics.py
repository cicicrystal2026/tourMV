"""歌词服务：生成 / 检查 / 编辑新版本 / 采纳改写建议 / 保留原文 / 回滚。

PRD V1.1 的几处关键变化：
- 检查结果以后端为准：LyricVersion.checks_done 区分「还没检查」与「检查后零疑点」；
- 字数用规则计算（LLM 数不准中文字数），押韵与史实交给 LLM；
- 改写建议必须是整句、6-12 字、不是原句的截断，不合格的建议直接丢弃；
- 知识库上线前，所有史实疑点都标「需人工核实」（needs_manual），不做断言；
- 「保留原文」必须填理由；
- 生产环境（ALLOW_MOCK=0）下 LLM 失败就报错，不再静默产出模板歌词。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import llm
from ..config import settings
from ..models import LyricCheck, LyricVersion, Project
from . import prompts
from .projects import ProjectError, ensure_editable, get as get_project

METRIC_RE = re.compile(r"[\s,，。、！？!?；;：:—…·\-\"'“”‘’（）()《》]")
MIN_LEN, MAX_LEN = 6, 12


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


def _editable(db: Session, project_id: str) -> Project:
    p = get_project(db, project_id)
    ensure_editable(p)
    return p


# ------------------------------------------------------------------ 生成
def generate(db: Session, project_id: str) -> tuple[LyricVersion, bool]:
    from ..models import Brief

    p = _editable(db, project_id)
    brief = db.execute(select(Brief).where(Brief.project_id == p.id)).scalar_one_or_none()
    mood = (brief.mood if brief else []) or []
    style = (brief.style_prompt if brief else "") or ""

    from . import kb as kb_svc

    kb_text, kb_ids = kb_svc.context_block(db, p.destination, " ".join(mood) + (brief.genre if brief else ""))
    payload, real = llm.json_chat(
        prompts.lyrics_messages(
            p.destination, brief.genre if brief else "", mood,
            brief.duration_s if brief else 180, style,
            brief.forbidden if brief else "", kb_text=kb_text,
        ),
        fallback=prompts.lyrics_fallback(p.destination, mood),
        purpose="lyrics_generate", project_id=p.id,
    )
    if not real and not settings().allow_mock:
        # 模板歌词（「我想带你去××」）一旦混进样片就是事故，生产环境宁可失败
        raise ProjectError("LLM 调用失败，未生成歌词（生产环境不使用模板占位歌词），请稍后重试")
    sections = payload.get("sections") or prompts.lyrics_fallback(p.destination, mood)["sections"]

    if brief is not None:
        brief.kb_refs = kb_ids      # 记录这一版写词引用了哪些已核实条目
    prev = latest(db, p.id)
    ver = LyricVersion(project_id=p.id, version=(prev.version + 1) if prev else 1,
                       sections=sections, source="ai", checks_done=False)
    db.add(ver)
    db.commit()
    db.refresh(ver)
    run_checks(db, ver)
    if not real:
        p.status_note = "歌词已生成（模拟数据：未配置 LLM）"
        db.commit()
    return ver, real


# ------------------------------------------------------------------ 检查
def _good_suggestion(s: str, original: str) -> bool:
    s = (s or "").strip()
    if not s or s == original.strip():
        return False
    n = clean_len(s)
    if n < MIN_LEN or n > MAX_LEN:
        return False
    # 原句的截断不是改写（V1.0 的「我想带你去桂林·」就是这么来的）
    core_s, core_o = METRIC_RE.sub("", s), METRIC_RE.sub("", original)
    if core_o.startswith(core_s) or core_s in core_o:
        return False
    # 只改了最后一个字（「雾还没散」→「雾还没江」）也不算改写
    if len(core_s) == len(core_o) and core_s[:-1] == core_o[:-1]:
        return False
    return True


def _filter(sugs: list, original: str) -> list[str]:
    out: list[str] = []
    for s in sugs or []:
        s = str(s).strip()
        if _good_suggestion(s, original) and s not in out:
            out.append(s)
    return out[:3]


def run_checks(db: Session, ver: LyricVersion, real: bool = True) -> list[LyricCheck]:
    proj = db.get(Project, ver.project_id)
    destination = proj.destination if proj else ""
    lines = {(n, i): ln for n, i, ln in flat_lines(ver.sections)}
    issues: list[dict] = []

    # 1) 字数：规则计算，不交给 LLM
    meter = [{"id": k, "section": n, "line_no": i, "text": ln}
             for k, (n, i, ln) in enumerate(flat_lines(ver.sections))
             if not MIN_LEN <= clean_len(ln) <= MAX_LEN]
    if meter:
        pay, _ = llm.json_chat(prompts.meter_fix_messages(meter, destination), fallback={"items": []},
                               purpose="lyrics_meter_fix", project_id=ver.project_id)
        fixes = {int(it.get("id", -1)): it.get("suggestions") or [] for it in (pay.get("items") or [])
                 if isinstance(it, dict)}
        for it in meter:
            n = clean_len(it["text"])
            issues.append({"type": "meter", "section": it["section"], "line_no": it["line_no"],
                           "text": it["text"], "evidence": f"当前 {n} 字，要求 {MIN_LEN}-{MAX_LEN} 字（不计标点）",
                           "suggestions": _filter(fixes.get(it["id"], []), it["text"])})

    # 2) 押韵 + 史实：LLM（带已核实知识库），失败时退回规则兜底（只标疑点、不给建议）
    from . import kb as kb_svc
    from ..models import KbEntry

    body = " ".join(ln for _n, _i, ln in flat_lines(ver.sections))
    kb_text, kb_ids = kb_svc.context_block(db, destination, body, limit=15)
    payload, _used = llm.json_chat(
        prompts.check_messages(ver.sections, destination, kb_text=kb_text),
        fallback=prompts.check_fallback(ver.sections, destination),
        purpose="lyrics_check", project_id=ver.project_id,
    )
    for it in payload.get("issues") or []:
        if not isinstance(it, dict) or it.get("type") not in ("rhyme", "fact"):
            continue
        key = (it.get("section", ""), int(it.get("line_no") or 0))
        actual = lines.get(key)
        if actual is None:
            continue      # 行号对不上的直接丢弃（V1.0 出现过同一行号挂两条不同原句）
        cited = []
        for n in it.get("kb") or []:
            try:
                idx = int(str(n).strip("[] ")) - 1
            except ValueError:
                continue
            if 0 <= idx < len(kb_ids) and kb_ids[idx] not in cited:
                cited.append(kb_ids[idx])
        issues.append({"type": it["type"], "section": key[0], "line_no": key[1], "text": actual,
                       "evidence": str(it.get("evidence") or ""), "kb": cited,
                       "suggestions": _filter(it.get("suggestions") or [], actual)})

    # 同一行同一类型只留一条
    seen: set[tuple] = set()
    uniq: list[dict] = []
    for it in issues:
        k = (it["type"], it["section"], it["line_no"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)

    db.query(LyricCheck).filter(LyricCheck.lyric_version_id == ver.id).delete()
    rows: list[LyricCheck] = []
    for it in uniq:
        is_fact = it["type"] == "fact"
        evidence = it["evidence"]
        cited = it.get("kb") or []
        if is_fact and cited:
            # 有已核实条目作依据：写明出处，不再标「需人工核实」
            refs = [db.get(KbEntry, k) for k in cited]
            src = "；".join(f"《{r.title}》（{r.source_name}）" for r in refs if r)
            evidence = f"据知识库 {src}：{evidence}"
        elif is_fact:
            # 知识库里没有覆盖的内容：只提示「需人工核实」，不当结论用
            evidence = f"【需人工核实·知识库无出处】{evidence}"
        row = LyricCheck(
            lyric_version_id=ver.id, project_id=ver.project_id,
            type=it["type"], line_no=it["line_no"], section=it["section"], text=it["text"],
            status="pending", evidence=evidence, suggestions=it["suggestions"],
            needs_manual=is_fact and not cited, kb_entry_ids=cited,
        )
        db.add(row)
        rows.append(row)
    ver.checks_done = True
    db.commit()
    return rows


def checks_of(db: Session, lyric_version_id: str) -> list[LyricCheck]:
    return list(db.execute(
        select(LyricCheck).where(LyricCheck.lyric_version_id == lyric_version_id)
        .order_by(LyricCheck.type, LyricCheck.line_no)
    ).scalars())


def check_summary(rows: list[LyricCheck], ver: LyricVersion | None = None) -> dict:
    """state: none（没有歌词）/ checking（检查未完成）/ done。

    前端只能在 state == done 时显示「通过」—— 这是 V1.0「没歌词也显示三项通过」的修复点。
    """
    if ver is None:
        return {"state": "none", "total": 0, "pending": 0, "by_type": {}}
    if not ver.checks_done:
        return {"state": "checking", "total": 0, "pending": 0, "by_type": {}}
    pending = [r for r in rows if r.status == "pending"]
    by_type: dict[str, dict] = {}
    for t in ("rhyme", "meter", "fact"):
        grp = [r for r in rows if r.type == t]
        by_type[t] = {"total": len(grp), "pending": len([r for r in grp if r.status == "pending"])}
    return {"state": "done", "total": len(rows), "pending": len(pending), "by_type": by_type}


# ------------------------------------------------------------------ 编辑
def save_version(db: Session, project_id: str, sections: list[dict], source: str = "manual",
                 carry_from: LyricVersion | None = None) -> LyricVersion:
    _editable(db, project_id)
    prev = latest(db, project_id)
    ver = LyricVersion(project_id=project_id, version=(prev.version + 1) if prev else 1,
                       sections=sections, source=source, checks_done=False)
    db.add(ver)
    db.commit()
    db.refresh(ver)
    run_checks(db, ver)
    if carry_from is not None:
        _carry_decisions(db, carry_from, ver)
    return ver


def _carry_decisions(db: Session, old: LyricVersion, new: LyricVersion) -> None:
    """新版本重跑检查后，原文没变的行沿用上一版「保留原文」的决定（含理由）。

    否则每采纳一条建议，其它已经确认保留的疑点就会被重新标成待处理，人要反复点。
    """
    kept = {(c.type, c.section, c.line_no, c.text): c.keep_reason
            for c in checks_of(db, old.id) if c.status in ("kept", "resolved")}
    if not kept:
        return
    for c in checks_of(db, new.id):
        reason = kept.get((c.type, c.section, c.line_no, c.text))
        if reason is not None and c.status == "pending":
            c.status, c.keep_reason = "kept", reason or "沿用上一版的保留决定"
    db.commit()


def rewrite(db: Session, project_id: str, targets: list[dict], instruction: str = "") -> LyricVersion:
    """targets: [{section, line_no}]，重写选中行，返回新版本。"""
    p = _editable(db, project_id)
    ver = latest(db, project_id)
    if ver is None:
        raise ProjectError("还没有歌词版本，先生成歌词")
    from ..models import Brief

    brief = db.execute(select(Brief).where(Brief.project_id == p.id)).scalar_one_or_none()
    want = [(t.get("section"), int(t.get("line_no") or 0)) for t in targets]
    lines = {(n, i): ln for n, i, ln in flat_lines(ver.sections)}
    picked = [lines[k] for k in want if k in lines]
    if not picked:
        raise ProjectError("没有选中任何歌词行")

    payload, real = llm.json_chat(
        prompts.rewrite_messages(picked, p.destination, brief.genre if brief else "", instruction),
        fallback=prompts.rewrite_fallback(picked),
        purpose="lyrics_rewrite", project_id=p.id,
    )
    if not real and not settings().allow_mock:
        raise ProjectError("LLM 调用失败，未能重写，请稍后重试")
    new_lines = payload.get("lines") or []
    if len(new_lines) != len(picked):
        raise ProjectError("改写结果行数与所选行数不一致，请重试")

    mapping = {k: v for k, v in zip([k for k in want if k in lines], new_lines)}
    sections = [{"name": s.get("name"),
                 "lines": [mapping.get((s.get("name"), i), ln) for i, ln in enumerate(s.get("lines") or [], 1)]}
                for s in ver.sections]
    return save_version(db, project_id, sections, source="manual", carry_from=ver)


def adopt_suggestion(db: Session, check_id: str, index: int = 0) -> LyricVersion:
    """采纳某条改写建议：替换对应行，生成新版本并重新检查。"""
    c = db.get(LyricCheck, check_id)
    if c is None:
        raise ProjectError("检查项不存在")
    _editable(db, c.project_id)
    if not c.suggestions:
        raise ProjectError("该检查项没有可采纳的建议，请直接编辑该行或保留原文")
    new_text = c.suggestions[min(max(0, index), len(c.suggestions) - 1)]

    ver = db.get(LyricVersion, c.lyric_version_id)
    if ver is None:
        raise ProjectError("歌词版本不存在")
    sections = []
    for s in ver.sections:
        lines = list(s.get("lines", []))
        if s.get("name") == c.section and 1 <= c.line_no <= len(lines):
            lines[c.line_no - 1] = new_text
        sections.append({"name": s.get("name"), "lines": lines})
    c.status = "adopted"
    db.commit()
    return save_version(db, ver.project_id, sections, source="manual", carry_from=ver)


def keep_original(db: Session, check_id: str, reason: str) -> LyricCheck:
    """保留原文：必须写理由（PRD V1.1 F2-5），理由会作为闸门放行的依据留档。"""
    c = db.get(LyricCheck, check_id)
    if c is None:
        raise ProjectError("检查项不存在")
    _editable(db, c.project_id)
    reason = (reason or "").strip()
    if len(reason) < 2:
        raise ProjectError("保留原文需要填写理由（例如：已查证属实 / 诗意表达不做史实解读）")
    c.status = "kept"
    c.keep_reason = reason[:500]
    db.commit()
    return c


def resolve_check(db: Session, check_id: str, reason: str = "") -> LyricCheck:
    """兼容 V1.0 的 /resolve 接口：等同于保留原文，同样要求理由。"""
    return keep_original(db, check_id, reason)


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
