"""线上单端口启动入口（PaaS 用）。

为什么需要这个文件：发布沙箱只认「一条能监听 $PORT 的启动命令」，
而 `backend/app/main.py` 是包内模块、没有 `__main__` 入口 ——
在项目根直接跑 uvicorn 找不到 `app` 包，自动检测也拿不到可执行入口
（实测表现为「service did not become reachable on port 3000 within 60s」）。

这里把 `backend/` 挂进 sys.path 再起 uvicorn，并显式读 PORT 环境变量，
所以**不依赖调用方的工作目录**。
"""
from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    port = int(os.environ.get("PORT") or 3000)
    uvicorn.run("app.main:app", host="0.0.0.0", port=port, log_level="info")
