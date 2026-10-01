"""可观测性：所有 AI 调用落库（PRD §11）。"""
from __future__ import annotations

import json
import logging

from .db import session_scope
from .models import AiCall

log = logging.getLogger("wenlv.telemetry")


def record(
    *,
    kind: str,
    provider: str,
    model: str = "",
    purpose: str = "",
    params: dict | None = None,
    status: str = "ok",
    elapsed_s: float = 0.0,
    cost: float = 0.0,
    error: str = "",
    project_id: str = "",
) -> None:
    try:
        with session_scope() as db:
            db.add(
                AiCall(
                    kind=kind,
                    provider=provider,
                    model=model,
                    purpose=purpose,
                    params_digest=json.dumps(params or {}, ensure_ascii=False)[:2000],
                    status=status,
                    elapsed_s=round(elapsed_s, 2),
                    cost=round(cost, 4),
                    error=error[:1000],
                    project_id=project_id,
                )
            )
    except Exception as e:  # noqa: BLE001 - 埋点失败绝不能影响主流程
        log.warning("写调用日志失败：%s", e)
