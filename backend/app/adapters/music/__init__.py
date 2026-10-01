"""音乐模型注册表。新增模型：写一个 MusicAdapter 子类 + 在这里 register()。"""
from __future__ import annotations

from .base import MockMusicAdapter, MusicAdapter, register
from .eleven import ElevenAdapter
from .lyria import LyriaAdapter
from .minimax import MiniMaxAdapter
from .mureka import MurekaAdapter

register(MockMusicAdapter())
# Mureka 有多个档位，价差 3 倍以上（每首）：7.6 ¥0.25 / 8·9 ¥0.33 / 9.5 ¥1。
# 每个档位注册成独立实例，界面上就能像切换音乐模型一样切换版本，
# 试听完一档直接换另一档，不必改 .env 再重启。
register(MurekaAdapter())                                            # 跟随 .env 的 MUREKA_MODEL
register(MurekaAdapter("mureka-76", "mureka-7.6", "Mureka 7.6"))      # 最便宜
register(MurekaAdapter("mureka-95", "mureka-9.5", "Mureka 9.5"))      # 最新最贵
register(ElevenAdapter())
register(LyriaAdapter())
register(MiniMaxAdapter())

__all__ = ["MusicAdapter", "MockMusicAdapter", "register"]
