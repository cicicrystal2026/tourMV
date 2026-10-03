"""音乐模型统一适配接口 —— PRD §7.1。

业务代码只依赖本模块的 MusicAdapter 抽象；新增/替换模型只需新增一个适配器文件
并在 adapters/music/__init__.py 注册。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ...config import settings


@dataclass
class GenRequest:
    lyrics: str                      # 带段落标记的歌词
    style_prompt: str
    duration_s: int
    vocal: str = "female"            # female / male / choir
    seed: int | None = None
    title: str = ""


@dataclass
class GenResult:
    status: str                      # done / running / failed
    audio_path: str = ""
    duration_s: float = 0.0
    bpm: float = 0.0
    cost: float = 0.0
    elapsed_s: float = 0.0
    raw_meta: dict[str, Any] = field(default_factory=dict)
    error: str = ""


class MusicAdapter(ABC):
    key: str = ""
    name: str = ""
    run_mode: str = "api"            # api / local
    price_per_min: float = 0.0       # 元/分钟，用于成本估算

    def __init__(self) -> None:
        self._jobs: dict[str, GenRequest] = {}
        self._out: dict[str, GenResult] = {}

    # ------------------------------------------------------------ 契约
    @abstractmethod
    def capabilities(self) -> dict:
        """是否支持中文、最长时长、是否支持自定义歌词、并发上限。"""

    @abstractmethod
    def generate(self, req: GenRequest) -> str:
        """提交生成任务，立即返回 job_id。"""

    @abstractmethod
    def get_result(self, job_id: str) -> GenResult:
        """查询任务结果。"""

    # ------------------------------------------------------------ 可选
    @property
    def ready(self) -> bool:
        """凭证/环境是否就绪。未就绪时由 registry 降级到 Mock。"""
        return True

    def estimate_cost(self, req: GenRequest) -> float:
        return round(req.duration_s / 60 * self.price_per_min, 4)

    def note(self) -> str:
        return ""

    def attach_request(self, job_id: str, req: GenRequest) -> None:
        """把「已经提交过的上游任务」与请求重新绑上（重试续跑时用）。

        为什么需要它：任务失败重试时**不能重新提交** ——
        上游（如 Mureka）在首次提交时就已经开始生成并按首计费了，
        重提交等于同一首歌付两次钱（线上真发生过）。
        正确做法是拿原来的任务号继续轮询；但轮询结果里要用的 req
        （时长兜底、费用估算）只存在内存里，换线程/进程重启后就没了，
        所以续跑前要重新绑一次。
        """
        self._jobs[job_id] = req


class MockMusicAdapter(MusicAdapter):
    """无凭证时的兜底：用 ffmpeg 合成真实可播放、时长与 BPM 符合要求的音频。

    这样即使一个 Key 都没配，也能跑出真实成片，用于验证上下游链路。
    """

    key = "mock"
    name = "Mock 合成器（占位）"
    run_mode = "local"
    price_per_min = 0.0

    def capabilities(self) -> dict:
        return {
            "supports_chinese": True,
            "max_duration_s": 600,
            "supports_custom_lyrics": True,
            "concurrency": 4,
            "mock": True,
        }

    def generate(self, req: GenRequest) -> str:
        from .synthesis import synth_track

        job_id = f"mock-{abs(hash((req.style_prompt, req.duration_s, req.seed)))%10**8:08d}"
        self._jobs[job_id] = req
        out = settings().data_dir / "music" / f"{job_id}.mp3"
        info = synth_track(out, duration_s=req.duration_s, seed=req.seed or 1,
                           hint=f"{req.style_prompt} {req.vocal}")
        self._out[job_id] = GenResult(
            status="done", audio_path=str(out), duration_s=float(req.duration_s),
            bpm=float(info["bpm"]), cost=0.0, raw_meta=info,
        )
        return job_id

    def get_result(self, job_id: str) -> GenResult:
        return self._out.get(job_id, GenResult(status="failed", error="未知 job_id"))


# 全局注册表：key -> adapter 实例
_REGISTRY: dict[str, MusicAdapter] = {}


def register(adapter: MusicAdapter) -> MusicAdapter:
    _REGISTRY[adapter.key] = adapter
    return adapter


def get(key: str) -> MusicAdapter | None:
    return _REGISTRY.get(key)


def all_adapters() -> list[MusicAdapter]:
    return list(_REGISTRY.values())


def resolve(key: str) -> MusicAdapter:
    """取适配器；凭证缺失时降级到 Mock，保证流程不断。"""
    a = _REGISTRY.get(key)
    if a is None:
        raise KeyError(f"未注册的音乐模型：{key}")
    if not a.ready:
        return get("mock") or a
    return a
