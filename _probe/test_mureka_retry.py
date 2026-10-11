"""Mureka 健壮性回归测试：轮询可重试 / 提交绝不重试 / 续跑绑定可用。

为什么必须有这个测试：Mureka **按首计费**，一旦提交环节发生「读超时后重提交」，
同一首歌就会被收两次钱（线上真实发生过）。而轮询是幂等 GET，超时后放弃反而
会把已经生成的歌判死。两者的正确行为完全相反，只能靠测试钉住。

用 fake 的 requests.request 驱动，不产生任何真实网络与计费。
"""
from __future__ import annotations

import pathlib
import sys
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import requests                                    # noqa: E402
from app.adapters.music.base import GenRequest     # noqa: E402
from app.adapters.music.mureka import MurekaAdapter   # noqa: E402

REQ = GenRequest(lyrics="[Verse]\n长白天池雪未消", style_prompt="cinematic folk",
                 duration_s=180)


class _Resp:
    def __init__(self, code: int, payload: dict):
        self.status_code = code
        self._payload = payload
        self.text = str(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def test_attach_request() -> None:
    """续跑绑定：任务号与请求重新绑上（基类方法，所有适配器共享）。"""
    a = MurekaAdapter(key="mureka", model="mureka-9", label="Mureka")
    a.attach_request("job-abc", REQ)
    assert a._jobs["job-abc"] is REQ, "attach_request 未生效"
    print("[1] attach_request 可用（基类继承） ✓")


def test_poll_retries_on_timeout() -> None:
    """轮询遇到读超时要重试，而不是把歌判死。"""
    a = MurekaAdapter(key="mureka", model="mureka-9", label="Mureka")
    a.attach_request("job-1", REQ)
    n = {"i": 0}

    def fake(method, url, **kw):
        n["i"] += 1
        if n["i"] <= 2:
            raise requests.Timeout("HTTPSConnectionPool: Read timed out. (read timeout=30)")
        return _Resp(200, {"status": "running"})

    with mock.patch("requests.request", fake), mock.patch("time.sleep"):
        r = a.get_result("job-1")
    assert n["i"] == 3, f"轮询应重试到成功，实际调用 {n['i']} 次"
    assert r.status == "running"
    print(f"[2] 轮询超时自动重试：{n['i']} 次后成功 ✓")


def test_submit_never_retries_on_timeout() -> None:
    """提交读到超时**不能**重试 —— 上游可能已受理并计费，重提交=付两次钱。"""
    a = MurekaAdapter(key="mureka", model="mureka-9", label="Mureka")
    n = {"i": 0}

    def fake(method, url, **kw):
        n["i"] += 1
        raise requests.Timeout("Read timed out. (read timeout=120)")

    with mock.patch("requests.request", fake), mock.patch("time.sleep"):
        try:
            a.generate(REQ)
        except requests.Timeout:
            pass
        else:
            raise AssertionError("提交超时竟然返回了结果")
    assert n["i"] == 1, f"提交只应尝试 1 次，实际 {n['i']} 次（会重复计费）"
    print(f"[3] 提交超时不重试（仅 {n['i']} 次，避免二次计费） ✓")


def test_submit_retries_on_429() -> None:
    """429 一定没被受理，可以安全重试。"""
    a = MurekaAdapter(key="mureka", model="mureka-9", label="Mureka")
    n = {"i": 0}

    def fake(method, url, **kw):
        n["i"] += 1
        if n["i"] <= 2:
            return _Resp(429, {"error": "concurrency limit"})
        return _Resp(200, {"id": "job-xyz"})

    with mock.patch("requests.request", fake), mock.patch("time.sleep"):
        jid = a.generate(REQ)
    assert jid == "job-xyz" and n["i"] == 3, f"429 应退避重试，实际 {n['i']} 次"
    print(f"[4] 429 退避重试：{n['i']} 次后提交成功 ✓")


if __name__ == "__main__":
    test_attach_request()
    test_poll_retries_on_timeout()
    test_submit_never_retries_on_timeout()
    test_submit_retries_on_429()
    print("\n全部通过：轮询可重试、提交不重试、续跑绑定可用。")
