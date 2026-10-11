"""路由汇总。"""
from fastapi import APIRouter

from . import assets, bench, edit, events, kb, lyrics, meta, music, projects, storyboard

api_router = APIRouter()
for r in (projects.router, lyrics.router, music.router, storyboard.router,
          edit.router, assets.router, kb.router, meta.router, events.router, bench.router):
    api_router.include_router(r)

__all__ = ["api_router"]
