"""测试夹具：拷一份真实数据库到临时目录，在 Mock 模式下起应用。

- DATA_DIR 指向临时目录 → 不污染 data/；
- FORCE_MOCK=1 → 不发生任何付费调用（LLM / 音乐 / 生图全部走本地兜底）；
- 用真实库的拷贝，顺带验证 V1.1 迁移在存量数据上的表现。
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SRC_DB = ROOT / "data" / "_backup" / "wenlv.db.pre-p0"

_tmp = Path(tempfile.mkdtemp(prefix="wenlv-test-"))
(_tmp / "data").mkdir()
if SRC_DB.exists():
    shutil.copy(SRC_DB, _tmp / "data" / "wenlv.db")
os.environ["DATA_DIR"] = str(_tmp / "data")
os.environ["FORCE_MOCK"] = "1"
os.environ["ACCESS_PASSWORD"] = ""
os.environ["DAILY_COST_LIMIT"] = "30"
os.environ.pop("DATABASE_URL", None)
sys.path.insert(0, str(ROOT / "backend"))


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c
    shutil.rmtree(_tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def has_real_db() -> bool:
    return SRC_DB.exists()


def wait_status(client, pid: str, want: str, timeout: float = 90) -> dict:
    t0 = time.time()
    while time.time() - t0 < timeout:
        p = client.get(f"/api/projects/{pid}").json()
        if p["project"]["status"] == want:
            return p
        time.sleep(0.5)
    raise AssertionError(f"等待状态 {want} 超时，当前 {p['project']['status']}：{p['project']['status_note']}")
