"""路由汇总。"""
from fastapi import APIRouter

from . import assets, edit, lyrics, meta, music, projects, storyboard

api_router = APIRouter()
for r in (projects.router, lyrics.router, music.router, storyboard.router,
          edit.router, assets.router, meta.router):
    api_router.include_router(r)

__all__ = ["api_router"]
