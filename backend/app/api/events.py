"""看板实时推送（PRD V1.1 F1-2：项目状态变化 5 秒内反映到看板，SSE）。

做法：每个连接每秒查一次「项目 / 任务」的变更指纹（最近更新时间 + 条数），
变了就推一条 `change` 事件，前端收到后再拉一次汇总。只推「变了」这个信号，
不推数据本身 —— 数据仍走原有接口，权限与口径只有一处。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from ..db import SessionLocal
from ..models import Job, Project

router = APIRouter(prefix="/api", tags=["events"])

TICK_S = 1.0
KEEPALIVE_S = 15.0


def fingerprint() -> str:
    with SessionLocal() as db:
        p = db.execute(select(func.max(Project.updated_at), func.count(Project.id))).one()
        j = db.execute(select(func.max(Job.updated_at), func.count(Job.id))).one()
    return f"{p[0]}|{p[1]}|{j[0]}|{j[1]}"


@router.get("/events")
async def events(request: Request) -> StreamingResponse:
    async def stream():
        last = await run_in_threadpool(fingerprint)
        # 告诉浏览器断线后 3 秒重连；先发一条 ready，前端据此确认推送可用
        yield "retry: 3000\nevent: ready\ndata: {}\n\n"
        idle = 0.0
        while not await request.is_disconnected():
            await asyncio.sleep(TICK_S)
            cur = await run_in_threadpool(fingerprint)
            if cur != last:
                last = cur
                idle = 0.0
                yield "event: change\ndata: {}\n\n"
            else:
                idle += TICK_S
                if idle >= KEEPALIVE_S:     # 注释行保活，防代理把空闲连接掐掉
                    idle = 0.0
                    yield ": keepalive\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
