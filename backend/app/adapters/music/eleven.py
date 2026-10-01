"""Eleven Music（ElevenLabs）适配器。

官方定价 $0.15/分钟（≈ ¥1.08/分钟），一首 3 分钟歌约 $0.45 ≈ ¥3.24 —— 三家里最贵。
但它有两个别家没有的东西：
1. **授权利落最清楚**：训练数据经 Merlin / Kobalt / Believe 授权，2026-09 又签了 UMG 多年协议；
   自助套餐（Starter 起）允许线上线下商用，但**不能**用于影视/电视/电台/工作室游戏，
   且 Free / Starter 档不得把曲目分发到流媒体平台（Creator $22/月起才可以）。
2. 支持 3 秒 – 5 分钟、WAV 输出、音色参考与分段编辑。

⚠️ PRD §13 已标注：Eleven Music 官方未列出中文支持。这里 supports_chinese 先置 False，
W1 实测后修正（2026-09 的 v2.5 版本已声称多语言人声，需实听中文咬字再定）。
"""
from __future__ import annotations

import uuid

import requests

from ...config import settings
from ..media import write_bytes
from .base import GenRequest, GenResult, MusicAdapter

# 官方定价 $0.15/分钟
PRICE_PER_MIN_USD = 0.15


class ElevenAdapter(MusicAdapter):
    key = "eleven"
    name = "Eleven Music"
    run_mode = "api"
    price_per_min = 0.0  # 按美元定价，见 estimate_cost

    @property
    def ready(self) -> bool:
        s = settings()
        return bool(s.eleven_api_key) and not s.force_mock

    def _headers(self) -> dict:
        return {"xi-api-key": settings().eleven_api_key}

    def capabilities(self) -> dict:
        return {
            "supports_chinese": False,      # 待 W1 实听
            "max_duration_s": 300,
            "supports_custom_lyrics": False,  # 以提示词驱动为主
            "concurrency": 2,
            "commercial_exclusions": "影视/电视/电台/工作室游戏；Free/Starter 不可分发到流媒体",
        }

    def estimate_cost(self, req: GenRequest) -> float:
        s = settings()
        return round(req.duration_s / 60 * PRICE_PER_MIN_USD * s.usd_cny, 4)

    def generate(self, req: GenRequest) -> str:
        """Eleven Music 为同步返回音频流的接口，这里在 generate 内完成取回。"""
        s = settings()
        prompt = req.style_prompt
        if req.lyrics:
            prompt = f"{prompt} | vocal: {req.vocal}"
        body = {
            "prompt": prompt[:2000],
            "music_length_ms": int(req.duration_s * 1000),
            "model_id": settings().eleven_music_model,
        }
        r = requests.post(f"{s.eleven_base_url}/v1/music",
                          headers={**self._headers(), "Content-Type": "application/json"},
                          json=body, timeout=300)
        r.raise_for_status()
        job_id = f"eleven-{uuid.uuid4().hex[:12]}"
        dest = s.data_dir / "music" / f"{job_id}.mp3"
        write_bytes(r.content, dest)
        self._jobs[job_id] = req
        self._out[job_id] = GenResult(
            status="done", audio_path=str(dest), duration_s=float(req.duration_s),
            cost=self.estimate_cost(req), elapsed_s=0.0, raw_meta={"bytes": len(r.content)},
        )
        return job_id

    def get_result(self, job_id: str) -> GenResult:
        return self._out.get(job_id, GenResult(status="failed", error="未知 job_id"))

    def note(self) -> str:
        s = settings()
        if not s.eleven_api_key:
            return "未配置：填 ELEVEN_API_KEY（elevenlabs.io 选 ElevenAPI → Developers → API Keys）。"
        return (f"官方 API（{s.eleven_music_model}）：${PRICE_PER_MIN_USD:.2f}/分钟 ≈ "
                f"¥{PRICE_PER_MIN_USD * s.usd_cny:.2f}/分钟，3 分钟歌约 "
                f"¥{PRICE_PER_MIN_USD * 3 * s.usd_cny:.2f}。商用授权最清晰，"
                "但影视/电视/电台/工作室游戏除外。")
