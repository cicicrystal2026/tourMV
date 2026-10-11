"""FastAPI 入口。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from urllib.parse import quote

import requests
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import queue as q
from .api import api_router
from .auth import AuthMiddleware
from .auth import router as auth_router
from .config import settings
from .db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("wenlv")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    init_db()
    # 让任务处理器注册到队列（import 即注册）
    from .workers import tasks  # noqa: F401

    from .db import session_scope
    from .services import assets as assets_svc
    from .services import music as music_svc
    from .services import trends as trends_svc

    with session_scope() as db:
        music_svc.ensure_models(db)
        # 战绩是派生数据：启动时按当前口径（只算已揭晓的选型轮）重算一遍，
        # 口径变更后不会残留旧值（V1.0 的「均分 12」就是旧口径的三项总和）
        music_svc.recompute_model_stats(db)
        trends_svc.seed_demo(db)          # 无真实 trends.db 时提供示例素材卡
        assets_svc.seed_demo_assets(db)   # 空素材库时生成占位镜头，便于跑通全流程
        cleaned = assets_svc.clean_tags(db)   # 存量标签按受控词表清洗（幂等）
        if any(cleaned.values()):
            log.info("素材标签已按受控词表清洗：%s", cleaned)
    q.start(s.api_concurrency, s.gpu_concurrency)
    # 队列起来之后再收拾「生成中」卡住的分镜（要先让队列把中断的任务重新入队）
    from .services import storyboard as storyboard_svc
    with session_scope() as db:
        storyboard_svc.recover_stuck(db)

    # 加载 Chinese-CLIP（首次还要下载权重）可能要几十秒，放后台线程，不拖慢启动
    import threading

    def _bg_reembed() -> None:
        try:
            with session_scope() as db2:
                _queue_reembed_if_needed(db2)
        except Exception as e:  # noqa: BLE001
            log.warning("向量重建检查失败：%s", e)
    threading.Thread(target=_bg_reembed, name="reembed-check", daemon=True).start()
    log.info("文旅MV生产管线 backend 就绪：%s", s.data_dir)
    yield
    q.stop()


def _queue_reembed_if_needed(db) -> None:
    """装上（或更换）向量模型后，库里旧编码器产出的向量不可比：后台排一个重建任务。"""
    from sqlalchemy import select

    from .adapters import clip as clip_ad
    from .models import Asset, Job

    want = clip_ad.encoder.model_id
    stale = any((a.embedding_model or "hash") != want for a in db.execute(select(Asset)).scalars())
    busy = db.execute(select(Job).where(Job.type == "reembed_assets",
                                        Job.status.in_(("queued", "running")))).first()
    if stale and not busy:
        q.enqueue(db, "reembed_assets", queue="gpu", name=f"重建素材向量（{want}）")
        db.commit()


app = FastAPI(title="文旅MV生产管线 V1.1", version="1.1.3", lifespan=lifespan)

# 访问控制先加、CORS 后加：Starlette 里后加的中间件在外层，
# 这样 401 响应也会带上 CORS 头，本地 5173 开发时前端能读到「需要登录」。
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(api_router)

_s = settings()
for name in ("assets", "thumbs", "music", "storyboard"):
    d = _s.data_dir / name
    d.mkdir(parents=True, exist_ok=True)
    app.mount(f"/media/{name}", StaticFiles(directory=str(d)), name=f"media-{name}")

# 成片单独处理：本地有就直出，本地没有就回源到 CDN。
#
# 为什么不直接挂 StaticFiles：成片是体积最大的一类产物（一条 3 分钟的 MV 五个版本
# 就是上百 MB），把它塞进发布包会让上传体量直接顶爆（实测 322MB 发布会失败、
# 报「sandbox /workspace is empty」）。而 EdgeOne 那边是独立部署的静态托管，
# 天然适合放大文件，所以线上包不带成片，后端按需回源。
_RENDERS = _s.data_dir / "renders"
_RENDERS.mkdir(parents=True, exist_ok=True)


@app.api_route("/media/renders/{filename:path}", methods=["GET", "HEAD"],
               include_in_schema=False)
def media_renders(filename: str, request: Request):
    # HEAD 必须一起支持：验收线上文件是否完整靠的就是 HEAD + Content-Length
    # （大文件用 GET 下载容易被超时截断，看到的是假象）。
    local = (_RENDERS / filename).resolve()
    if local.is_file() and _RENDERS.resolve() in local.parents:
        return FileResponse(local)

    base = _s.media_fallback_base
    if not base:
        raise HTTPException(status_code=404, detail="Not Found")

    # 回源：把 Range 头一起带上，浏览器才能拖拽/边下边播。
    headers = {k: v for k, v in request.headers.items()
               if k.lower() in ("range", "if-range", "accept")}
    # URL 用 `raw_path`（原样保留百分号编码）而不是重新 quote(filename) ——
    # 框架已经把路径参数解码成中文名了，再 quote 一次会变成双层编码，
    # CDN 收到 `%25E6%25B0%25B8` 直接 403（这个坑踩过）。
    raw = request.scope.get("raw_path") or b""
    path = raw.decode("latin-1") or f"/media/renders/{quote(filename)}"
    url = f"{base}{path}"
    log.info("成片回源：%s", url)
    try:
        if request.method == "HEAD":
            up = requests.head(url, headers=headers, timeout=30, allow_redirects=True)
            keep = ("content-type", "content-length", "content-range",
                    "accept-ranges", "last-modified", "etag")
            return Response(status_code=up.status_code,
                            headers={k: v for k, v in up.headers.items()
                                     if k.lower() in keep})
        upstream = requests.get(url, headers=headers, stream=True, timeout=30)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"回源失败：{e}") from e

    if upstream.status_code >= 400:
        upstream.close()
        raise HTTPException(status_code=upstream.status_code, detail="回源未命中")

    keep = ("content-type", "content-length", "content-range",
            "accept-ranges", "last-modified", "etag")

    def body():
        try:
            for chunk in upstream.iter_content(1 << 16):
                yield chunk
        finally:
            upstream.close()

    return StreamingResponse(
        body(), status_code=upstream.status_code,
        headers={k: v for k, v in upstream.headers.items() if k.lower() in keep},
    )


@app.get("/api/health")
def health() -> dict:
    # 不返回 data_dir 等服务器路径（PRD V1.1 §11）；该接口免登录，只给探活用
    return {"ok": True}


@app.api_route("/api/media/audio/{token}", methods=["GET", "HEAD"], include_in_schema=False)
def media_audio(token: str):
    """候选音频只通过随机 token 访问（PRD V1.1 §9 盲听约束）。

    文件名里带着模型或 mock 字样（如 mureka-xxx.mp3 / mock-xxx.mp3），
    直接用 /media/music/<文件名> 就等于在盲听时把答案写在地址栏里。
    走 /api 前缀，是为了在 EdgeOne 线上也由边缘函数反代到后端（/media 是静态镜像）。
    """
    from sqlalchemy import select

    from .db import SessionLocal
    from .models import MusicCandidate

    if not token or len(token) > 40:
        raise HTTPException(status_code=404, detail="Not Found")
    with SessionLocal() as db:
        c = db.execute(select(MusicCandidate).where(MusicCandidate.audio_token == token)
                       ).scalar_one_or_none()
        path = c.audio_path if c else ""
    music_dir = (_s.data_dir / "music").resolve()
    local = (music_dir / path.replace("\\", "/").rsplit("/", 1)[-1]).resolve() if path else None
    if not local or not local.is_file() or music_dir not in local.parents:
        raise HTTPException(status_code=404, detail="Not Found")
    media_type = "audio/wav" if local.suffix.lower() == ".wav" else "audio/mpeg"
    # 不带原文件名（Content-Disposition），避免下载时暴露
    return FileResponse(local, media_type=media_type)


# ---------------------------------------------------------------- 前端单端口托管
# 线上沙箱只暴露一个端口，所以把 vite build 的产物放到 backend/webapp，
# 由本进程连同 /api、/media 一起托管。本地开发不放这个目录，前端仍走 5173。
_webapp = _s.webapp_dir
if (_webapp / "index.html").exists():
    if (_webapp / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=str(_webapp / "assets")), name="spa-assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        """前端是 BrowserRouter，深链接（如 /projects/<id>/lyrics）必须回落到 index.html。

        未匹配到的 /api、/media 一律保持 404 JSON，不能被 SPA 吞掉——
        否则接口写错路径时会收到一坨 HTML，排查起来很费劲。
        """
        if full_path.startswith(("api/", "media/")):
            raise HTTPException(status_code=404, detail="Not Found")
        root = _webapp.resolve()
        target = (root / full_path).resolve()
        if full_path and target.is_file() and root in target.parents:
            return FileResponse(target)
        return FileResponse(root / "index.html")

    log.info("已启用单端口模式：前端产物来自 %s", _webapp)
