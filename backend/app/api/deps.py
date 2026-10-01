"""路由共用依赖。"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Project


def guard_locked(request: Request, db: Session = Depends(get_db)) -> None:
    """已交付项目只读（PRD V1.1 §4.2）：拦截路径里带 project_id 的所有写请求。

    挂在配图 / 剪辑路由上。投放数据回填（projects 路由）不受影响 —— 交付后回填正是预期操作。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    pid = request.path_params.get("project_id")
    if not pid:
        return
    p = db.get(Project, pid)
    if p is not None and p.locked_at is not None:
        raise HTTPException(status_code=400, detail="项目已交付并锁定，只读；如需修改请「新建修订版」")
