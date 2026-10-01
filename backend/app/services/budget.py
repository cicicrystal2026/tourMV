"""每日费用上限（PRD V1.1 §11 安全：生成类接口受 DAILY_COST_LIMIT 约束）。

费用来源有两处：音乐候选的 cost（适配器按首计价写在候选上），
以及 ai_call 表（LLM / 生图等按次记录）。两者不重叠，相加即当日支出。
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import AiCall, MusicCandidate
from .projects import ProjectError


def _today_start_utc() -> dt.datetime:
    # 按服务器本地日期切日，再换算成库里存的 UTC（naive）
    local_midnight = dt.datetime.combine(dt.date.today(), dt.time.min).astimezone()
    return local_midnight.astimezone(dt.timezone.utc).replace(tzinfo=None)


def spent_today(db: Session) -> float:
    since = _today_start_utc()
    music = db.execute(select(func.coalesce(func.sum(MusicCandidate.cost), 0.0))
                       .where(MusicCandidate.created_at >= since)).scalar_one()
    calls = db.execute(select(func.coalesce(func.sum(AiCall.cost), 0.0))
                       .where(AiCall.created_at >= since)).scalar_one()
    return round(float(music or 0) + float(calls or 0), 2)


def status(db: Session) -> dict:
    limit = settings().daily_cost_limit
    spent = spent_today(db)
    return {"limit": limit, "spent": spent,
            "remaining": None if not limit else round(max(0.0, limit - spent), 2)}


def ensure_budget(db: Session, estimate: float = 0.0, what: str = "本次生成") -> None:
    """超出当日上限时拒绝派发付费任务。estimate 为本次预估费用（元）。"""
    limit = settings().daily_cost_limit
    if not limit:
        return
    spent = spent_today(db)
    if spent + max(0.0, estimate) > limit:
        raise ProjectError(
            f"已达今日费用上限：今日已花 ¥{spent:.2f}，{what}预计 ¥{estimate:.2f}，"
            f"上限 ¥{limit:.2f}。如确需继续，请调高 DAILY_COST_LIMIT。"
        )
