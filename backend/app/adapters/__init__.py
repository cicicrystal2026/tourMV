"""适配器层统一出口。业务代码只 import 这里，不直接依赖具体厂商。"""
from __future__ import annotations

from . import align, clip, media, vlm
from .llm import llm
from .music import base as music

__all__ = ["llm", "vlm", "clip", "align", "media", "music"]
