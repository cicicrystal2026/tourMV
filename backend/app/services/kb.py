"""文化知识库服务（PRD V1.1 §6.7 / D4）。

轻量实现：人工录入 / AI 起草 + 人工核实，按目的地检索。
- 只有已核实条目才能作为「出处」被写词与史实核对引用；
- 检索先用中文字二元组重合度（无额外依赖、结果可解释），
  语义向量检索与素材库的 CLIP 一起在「真实能力」阶段接入。
"""
from __future__ import annotations

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import llm
from ..models import KbEntry, LyricCheck
from .projects import ProjectError

SOURCE_TYPES = {
    "official": "官方", "academic": "学术", "encyclopedia": "百科",
    "field": "人工查证", "other": "其他", "ai_draft": "AI 起草",
}
COVERAGE_MIN = 10   # 每个目的地建议至少录入的已核实条目数（PRD F7-5）

_NON_WORD = re.compile(r"[^\w]", re.U)


def dest_core(destination: str) -> str:
    """「吉林·长白山」→「长白山」，条目按核心地名归属。"""
    return (destination or "").replace("•", "·").split("·")[-1].strip()


def _bigrams(text: str) -> set[str]:
    t = _NON_WORD.sub("", text or "")
    return {t[i:i + 2] for i in range(len(t) - 1)} or ({t} if t else set())


def serialize(e: KbEntry) -> dict:
    from ..serialize import dt
    return {"id": e.id, "destination": e.destination, "title": e.title, "content": e.content,
            "source_name": e.source_name, "source_url": e.source_url,
            "source_type": e.source_type, "source_type_label": SOURCE_TYPES.get(e.source_type, e.source_type),
            "verified": e.verified, "verified_by": e.verified_by,
            "from_check_id": e.from_check_id, "updated_at": dt(e.updated_at)}


# ------------------------------------------------------------------ 增删改
def list_entries(db: Session, destination: str = "", q: str = "", verified: bool | None = None) -> list[KbEntry]:
    stmt = select(KbEntry).order_by(KbEntry.destination, KbEntry.updated_at.desc())
    rows = list(db.execute(stmt).scalars())
    core = dest_core(destination)
    if core:
        rows = [r for r in rows if core in r.destination or r.destination in destination]
    if verified is not None:
        rows = [r for r in rows if r.verified == verified]
    if q.strip():
        return [r for _s, r in search_rows(rows, q, k=len(rows))]
    return rows


def _validate(payload: dict, partial: bool = False) -> dict:
    out = {}
    for k in ("destination", "title", "content", "source_name", "source_url", "source_type"):
        if k in payload:
            out[k] = str(payload.get(k) or "").strip()
    if not partial:
        for k in ("destination", "title", "content"):
            if not out.get(k):
                raise ProjectError(f"知识库条目缺少「{ {'destination': '目的地', 'title': '标题', 'content': '内容'}[k] }」")
    if out.get("source_type") and out["source_type"] not in SOURCE_TYPES:
        raise ProjectError(f"未知的来源类型：{out['source_type']}")
    return out


def create(db: Session, payload: dict) -> KbEntry:
    data = _validate(payload)
    verified = bool(payload.get("verified")) and data.get("source_type") != "ai_draft"
    if verified and not data.get("source_name"):
        raise ProjectError("已核实条目必须写明出处（来源名称）")
    e = KbEntry(**data, verified=verified, verified_by="人工" if verified else "",
                from_check_id=str(payload.get("from_check_id") or ""))
    db.add(e)
    db.commit()
    db.refresh(e)
    return e


def update(db: Session, entry_id: str, payload: dict) -> KbEntry:
    e = db.get(KbEntry, entry_id)
    if e is None:
        raise ProjectError("条目不存在")
    for k, v in _validate(payload, partial=True).items():
        setattr(e, k, v)
    # 内容被改过的条目要重新核实
    if any(k in payload for k in ("content", "title")) and not payload.get("verified"):
        e.verified, e.verified_by = False, ""
    db.commit()
    return e


def verify(db: Session, entry_id: str, source_name: str = "", source_url: str = "",
           source_type: str = "") -> KbEntry:
    """人工核实：必须给出真实出处。AI 起草的条目核实时要把来源改成真实来源类型。"""
    e = db.get(KbEntry, entry_id)
    if e is None:
        raise ProjectError("条目不存在")
    if source_name.strip():
        e.source_name = source_name.strip()
    if source_url.strip():
        e.source_url = source_url.strip()
    if source_type:
        if source_type not in SOURCE_TYPES or source_type == "ai_draft":
            raise ProjectError("核实时请选择真实的来源类型（官方 / 学术 / 百科 / 人工查证 / 其他）")
        e.source_type = source_type
    if e.source_type == "ai_draft":
        raise ProjectError("AI 起草的条目核实时，请把来源类型改成你实际查证的来源")
    if not e.source_name.strip():
        raise ProjectError("请填写出处（来源名称），例如：长白山管委会官网 / 《长白山志》")
    e.verified, e.verified_by = True, "人工"
    db.commit()
    return e


def delete(db: Session, entry_id: str) -> None:
    e = db.get(KbEntry, entry_id)
    if e is None:
        raise ProjectError("条目不存在")
    db.delete(e)
    db.commit()


# ------------------------------------------------------------------ 检索
def search_rows(rows: list[KbEntry], q: str, k: int = 5) -> list[tuple[float, KbEntry]]:
    qb = _bigrams(q)
    scored = []
    for r in rows:
        rb = _bigrams(r.title + r.content)
        if not qb or not rb:
            continue
        s = len(qb & rb) / (len(qb) ** 0.5 * len(rb) ** 0.5)
        if s > 0:
            scored.append((s, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return scored[:k]


def verified_for(db: Session, destination: str) -> list[KbEntry]:
    return list_entries(db, destination, verified=True)


def coverage(db: Session, destination: str) -> dict:
    rows = list_entries(db, destination)
    v = len([r for r in rows if r.verified])
    return {"destination": dest_core(destination), "verified": v, "unverified": len(rows) - v,
            "min": COVERAGE_MIN, "enough": v >= COVERAGE_MIN}


def context_block(db: Session, destination: str, query: str = "", limit: int = 12) -> tuple[str, list[str]]:
    """给 LLM 的「已核实资料」块，返回 (文本, 条目 id 列表)。"""
    rows = verified_for(db, destination)
    if not rows:
        return "", []
    if query.strip():
        picked = [r for _s, r in search_rows(rows, query, k=limit)] or rows[:limit]
    else:
        picked = rows[:limit]
    text = "\n".join(f"[{i + 1}] {r.title}：{r.content}（出处：{r.source_name}）" for i, r in enumerate(picked))
    return text, [r.id for r in picked]


# ------------------------------------------------------------------ AI 起草
def draft(db: Session, destination: str, count: int = 8, project_id: str = "") -> list[KbEntry]:
    """让 LLM 起草条目，全部标「AI 起草·待核实」，不会被当作出处引用，直到有人核实。"""
    core = dest_core(destination)
    if not core:
        raise ProjectError("请指定目的地")
    count = max(3, min(15, int(count or 8)))
    exist = [r.title for r in list_entries(db, destination)]
    payload, real = llm.json_chat(
        [{"role": "system", "content": (
            "你是文旅资料编辑。为指定目的地整理可用于写歌词与核对史实的资料条目："
            "地理地貌、自然景观、历史沿革、地名由来、民族文化、物产风俗等。"
            "每条只写你有把握的、可公开查证的事实，60-150 字；不确定的不要写。"
            "并给出读者可以去核对的公开来源名称（如官方网站、地方志、百科词条名），不要编造链接。"
            '只输出 JSON：{"entries":[{"title":"…","content":"…","source_hint":"…"}]}')},
         {"role": "user", "content": f"目的地：{destination}\n需要 {count} 条。已有条目（不要重复）：{'、'.join(exist[:30]) or '无'}"}],
        fallback={"entries": []}, purpose="kb_draft", project_id=project_id,
    )
    if not real:
        raise ProjectError("LLM 不可用，无法起草（可手动录入条目）")
    out = []
    for it in (payload.get("entries") or [])[:count]:
        title, content = str(it.get("title") or "").strip(), str(it.get("content") or "").strip()
        if not title or not content or title in exist:
            continue
        e = KbEntry(destination=destination, title=title[:200], content=content[:2000],
                    source_name=str(it.get("source_hint") or "")[:200], source_type="ai_draft",
                    verified=False)
        db.add(e)
        out.append(e)
    db.commit()
    return out


# ------------------------------------------------------------------ 疑点回流（F7-4）
def from_check(db: Session, check_id: str, destination: str, content: str, source_name: str) -> KbEntry:
    """处理「需人工核实」疑点时，把查证结论直接存成已核实条目。"""
    c = db.get(LyricCheck, check_id)
    if c is None:
        raise ProjectError("检查项不存在")
    if not source_name.strip():
        raise ProjectError("存入知识库需要写明出处")
    return create(db, {"destination": destination, "title": f"核实：{c.text}"[:200], "content": content,
                       "source_name": source_name, "source_type": "field", "verified": True,
                       "from_check_id": check_id})
