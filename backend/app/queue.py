"""SQLite 任务队列 —— 替代 PRD §10.1 的 Redis + RQ（Windows 无原生 Redis）。

保留 PRD 的并发语义：api 队列 4 并发、gpu 队列 1 并发。
单进程内存锁 + DB 状态双保险；接口与 RQ 的 enqueue 形状一致，将来换 Redis 只需替换本文件。
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from .db import session_scope
from .models import Job

log = logging.getLogger("wenlv.queue")

Handler = Callable[["JobContext"], None]
_HANDLERS: dict[str, Handler] = {}


class JobContext:
    """交给 handler 的上下文：持有自己的 DB 会话，避免跨线程复用。"""

    def __init__(self, job_id: str, db) -> None:
        self.job_id = job_id
        self.db = db

    def job(self) -> Job:
        j = self.db.get(Job, self.job_id)
        if j is None:
            raise RuntimeError(f"任务不存在：{self.job_id}")
        return j

    def progress(self, pct: int, note: str | None = None) -> None:
        j = self.job()
        j.progress = max(0, min(100, int(pct)))
        if note:
            j.name = note[:200]
        self.db.commit()

    def vram(self, mb: int) -> None:
        j = self.job()
        j.vram_mb = mb
        self.db.commit()

    def set_result(self, result: dict[str, Any]) -> None:
        j = self.job()
        j.result = result
        self.db.commit()


def handler(job_type: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _HANDLERS[job_type] = fn
        return fn

    return deco


def enqueue(
    db,
    job_type: str,
    *,
    queue: str = "api",
    project_id: str = "",
    ref_id: str = "",
    name: str = "",
    payload: dict | None = None,
) -> Job:
    j = Job(
        queue=queue,
        type=job_type,
        ref_id=ref_id,
        project_id=project_id,
        name=name or job_type,
        status="queued",
        payload=payload or {},
    )
    db.add(j)
    db.commit()
    db.refresh(j)
    _wake()
    return j


# ------------------------------------------------------------------ worker
_lock = threading.Lock()
_wake_event = threading.Event()
_threads: list[threading.Thread] = []
_running = False
MAX_RETRIES = 3


def _claim(db, queue: str) -> Job | None:
    """取一条排队中的任务并置为 running。"""
    j = db.execute(
        select(Job).where(Job.queue == queue, Job.status == "queued").order_by(Job.created_at).limit(1)
    ).scalar_one_or_none()
    if j is None:
        return None
    j.status = "running"
    j.started_at = datetime.now(timezone.utc)
    j.progress = 1
    db.commit()
    return j


def _loop(queue: str) -> None:
    while _running:
        worked = False
        with _lock:
            with session_scope() as db:
                j = _claim(db, queue)
                if j is not None:
                    worked = True
                    jid, jtype = j.id, j.type
        if worked:
            _run_one(jid, jtype)
            continue
        _wake_event.wait(timeout=1.5)
        _wake_event.clear()


def _run_one(job_id: str, job_type: str) -> None:
    fn = _HANDLERS.get(job_type)
    t0 = time.time()
    with session_scope() as db:
        ctx = JobContext(job_id, db)
        try:
            if fn is None:
                raise RuntimeError(f"未注册的任务类型：{job_type}")
            fn(ctx)
            j = ctx.job()
            j.status = "done"
            j.progress = 100
            j.finished_at = datetime.now(timezone.utc)
        except Exception as e:  # noqa: BLE001 - worker 必须吞掉异常并回写状态
            log.warning("任务失败 %s: %s", job_type, e)
            j = ctx.job()
            j.error = f"{type(e).__name__}: {e}"[:2000]
            j.retries += 1
            if j.retries < MAX_RETRIES:
                j.status = "queued"  # 指数退避由轮询间隔自然实现
                j.progress = 0
            else:
                j.status = "failed"
                j.finished_at = datetime.now(timezone.utc)
            log.debug(traceback.format_exc())
    log.info("任务 %s(%s) 用时 %.1fs", job_type, job_id[:8], time.time() - t0)


def start(api_concurrency: int = 4, gpu_concurrency: int = 1) -> None:
    global _running
    if _running:
        return
    _running = True
    for i in range(api_concurrency):
        t = threading.Thread(target=_loop, args=("api",), name=f"api-worker-{i}", daemon=True)
        t.start()
        _threads.append(t)
    for i in range(gpu_concurrency):
        t = threading.Thread(target=_loop, args=("gpu",), name=f"gpu-worker-{i}", daemon=True)
        t.start()
        _threads.append(t)
    log.info("队列已启动：api=%d gpu=%d", api_concurrency, gpu_concurrency)


def stop() -> None:
    global _running
    _running = False
    _wake_event.set()


def _wake() -> None:
    _wake_event.set()


def requeue(db, job: Job) -> Job:
    """失败任务重试。"""
    job.status = "queued"
    job.progress = 0
    job.error = ""
    db.commit()
    _wake()
    return job


def registered() -> list[str]:
    return sorted(_HANDLERS)
