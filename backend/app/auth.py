"""访问控制（PRD V1.1 §11 安全）。

V1.x 是内部工具，不做账号体系，只做一道「实验室口令」：
配置 ACCESS_PASSWORD 后，/api 与 /media 都要求登录 cookie；未配置时全部放行（仅限本机开发）。

为什么是口令 + 签名 cookie，而不是 Basic Auth：
- Basic Auth 的浏览器弹窗在 EdgeOne 边缘函数反代后行为不稳定，且无法「退出」；
- cookie 由 /api/auth/login 下发，前端遇到 401 就显示登录页，体验可控。

脚本 / 验收工具可改用请求头 `X-Access-Token: <口令>`。
"""
from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import APIRouter, Body, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

COOKIE = "wenlv_auth"
COOKIE_MAX_AGE = 14 * 24 * 3600
# 不需要登录就能访问的接口：健康检查（平台探活）与登录本身
PUBLIC_PATHS = {"/api/health", "/api/auth/login", "/api/auth/status"}

router = APIRouter(prefix="/api/auth", tags=["auth"])


# 登录失败计数（进程内）：{来源: [失败时间戳]}
_FAILS: dict[str, list[float]] = {}
FAIL_LIMIT, FAIL_WINDOW_S = 5, 60.0


def _too_many(ip: str) -> bool:
    import time

    now = time.time()
    recent = [t for t in _FAILS.get(ip, []) if now - t < FAIL_WINDOW_S]
    _FAILS[ip] = recent
    return len(recent) >= FAIL_LIMIT


def _note_fail(ip: str) -> None:
    import time

    _FAILS.setdefault(ip, []).append(time.time())


def enabled() -> bool:
    return bool(settings().access_password)


def _token() -> str:
    s = settings()
    return hmac.new(s.session_secret.encode("utf-8"), b"wenlv-v1", hashlib.sha256).hexdigest()


def is_authed(request: Request) -> bool:
    if not enabled():
        return True
    header = request.headers.get("x-access-token", "")
    if header and hmac.compare_digest(header, settings().access_password):
        return True
    cookie = request.cookies.get(COOKIE, "")
    return bool(cookie) and hmac.compare_digest(cookie, _token())


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        guarded = path.startswith("/api/") or path.startswith("/media/")
        if guarded and path not in PUBLIC_PATHS and request.method != "OPTIONS" \
                and not is_authed(request):
            return JSONResponse({"detail": "需要登录", "auth_required": True}, status_code=401)
        return await call_next(request)


@router.get("/status")
def status(request: Request) -> dict:
    return {"enabled": enabled(), "authed": is_authed(request)}


@router.post("/login")
def login(request: Request, response: Response, payload: dict = Body(...)) -> dict:
    if not enabled():
        return {"ok": True, "enabled": False}
    ip = (request.headers.get("x-forwarded-for") or (request.client.host if request.client else "")
          ).split(",")[0].strip() or "?"
    if _too_many(ip):
        raise HTTPException(status_code=429, detail="尝试次数过多，请 1 分钟后再试")
    pwd = str(payload.get("password") or "")
    if not hmac.compare_digest(pwd, settings().access_password):
        _note_fail(ip)
    if not hmac.compare_digest(pwd, settings().access_password):
        time.sleep(0.8)   # 粗暴但有效：拖慢暴力尝试
        raise HTTPException(status_code=401, detail="口令不正确")
    https = (request.headers.get("x-forwarded-proto") or request.url.scheme) == "https"
    response.set_cookie(COOKIE, _token(), max_age=COOKIE_MAX_AGE, httponly=True,
                        samesite="lax", secure=https, path="/")
    return {"ok": True, "enabled": True}


@router.post("/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}
