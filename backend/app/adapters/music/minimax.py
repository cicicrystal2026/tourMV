"""MiniMax Music 适配器 —— 官方开放平台 API 与本地自建服务二选一。

按 .env 自动选择，官方 API 优先：

A. 官方 API（配 MINIMAX_API_KEY）
     POST {MINIMAX_API_HOST}/v1/music_generation
     headers: Authorization: Bearer <KEY>
     body: {model, prompt, lyrics, audio_setting, output_format}
     resp: {data: {audio | audio_url, status}, base_resp: {status_code, status_msg}}

     ⚠️ 官方公告（2026-08-20）：付费的音乐生成 / 歌词生成 API 不再对新用户开放，
     免费版（music-3.0-free / music-2.6-free / music-cover-free）已停止服务；
     Token Plan 也不含音乐（只覆盖 M3 / M2.7 / image / speech）。
     故本路径仅对存量付费账号有效。模型枚举：music-3.0（推荐）/ music-2.6 / music-cover。

     国内站 MINIMAX_API_HOST=https://api.minimaxi.com（仅存量账号可用）
     海外站 api.minimax.io

B. 本地自建服务（配 MINIMAX_LOCAL_ENDPOINT，本地 4090 部署开源权重）
     POST {MINIMAX_LOCAL_ENDPOINT}/generate   body {lyrics, prompt, duration_s, vocal, seed} → {job_id}
     GET  {MINIMAX_LOCAL_ENDPOINT}/jobs/{job_id} → {status, audio_path|audio_url, duration_s, bpm}

⚠️ 关于 PRD §13
    PRD 当时判断「MiniMax 付费 API 已不对新用户开放」，因此把 MiniMax 定为本地部署路线。
    该结论已过期：官方 Music 系列 API 已对外开放，无需本地 GPU 也能参与三模型对比。
    故此适配器改为双模；两条路都不配时 ready=False，自动降级到 Mock，流程不断。
"""
from __future__ import annotations

import time
import uuid

import requests

from ...config import settings
from ..media import download, write_bytes
from .base import GenRequest, GenResult, MusicAdapter

# 官方 API 按首计价（≈ $0.15/首，歌词生成另 +$0.01），这里折人民币做成本估算。
PRICE_PER_SONG_CNY = 1.10


class MiniMaxAdapter(MusicAdapter):
    key = "minimax"
    name = "MiniMax Music"
    price_per_min = 0.0  # 实际按首计价，见 estimate_cost

    # ---------------------------------------------------------------- 模式
    @property
    def mode(self) -> str:
        return settings().minimax_mode

    @property
    def ready(self) -> bool:
        return self.mode != "none" and not settings().force_mock

    @property
    def run_mode(self) -> str:
        # 官方 API 走 api 队列（并发 4）；本地推理走 gpu 队列（串行 1）
        return "api" if self.mode == "cloud" else "local"

    @property
    def name(self) -> str:  # type: ignore[override]
        return {
            "cloud": "MiniMax Music（官方 API）",
            "local": "MiniMax Music（本地部署）",
        }.get(self.mode, "MiniMax Music")

    def capabilities(self) -> dict:
        cloud = self.mode == "cloud"
        return {
            "supports_chinese": True,
            "max_duration_s": 240,
            "supports_custom_lyrics": True,
            # 官方 API 无显存约束；本地 4090 单卡串行
            "concurrency": 4 if cloud else 1,
            "mode": self.mode or "none",
        }

    def estimate_cost(self, req: GenRequest) -> float:
        # 本地部署不按次计费（成本在电费/工时）；官方 API 按首计价。
        return PRICE_PER_SONG_CNY if self.mode == "cloud" else 0.0

    # ---------------------------------------------------------------- 提交
    def generate(self, req: GenRequest) -> str:
        if self.mode == "cloud":
            return self._generate_cloud(req)
        return self._generate_local(req)

    def get_result(self, job_id: str) -> GenResult:
        # 官方 API 是同步返回的，结果在 generate() 阶段就已落盘；
        # 本地服务是异步任务，需要轮询。
        if self.mode == "cloud":
            return self._out.get(job_id, GenResult(status="failed", error="未知 job_id"))
        return self._poll_local(job_id)

    def note(self) -> str:
        if self.mode == "cloud":
            s = settings()
            return (f"官方 API（{s.minimax_api_host} / {s.minimax_music_model}）："
                    f"按首计费 ≈ ¥{PRICE_PER_SONG_CNY:.2f}/首，单曲上限 5 分钟。"
                    f"⚠️ 2026-08-20 起不再对新用户开放，仅存量付费账号可用。")
        if self.mode == "local":
            return "本地部署模式：自建推理服务，需核实 4090 显存与单曲生成耗时。"
        return ("未配置：官方托管 API 已对新用户关闭，请填 MINIMAX_LOCAL_ENDPOINT 部署开源 "
                "MiniMaxAI/MiniMax-Music3；只有存量付费账号才填 MINIMAX_API_KEY。")

    # ---------------------------------------------------------------- A. 官方 API
    def _generate_cloud(self, req: GenRequest) -> str:
        s = settings()
        t0 = time.time()
        payload: dict = {
            "model": s.minimax_music_model,
            "prompt": (req.style_prompt or "")[:2000],
            "audio_setting": {"sample_rate": 44100, "bitrate": 256000, "format": "mp3"},
            "output_format": "url",
        }
        # 有歌词就是「歌词生曲」，没有就让模型按 prompt 出纯音乐
        if req.lyrics:
            payload["lyrics"] = req.lyrics
        headers = {"Authorization": f"Bearer {s.minimax_api_key}",
                   "Content-Type": "application/json"}

        data = self._post_music(s.minimax_api_host, payload, headers)
        if data.get("_retry_without_output_format"):
            payload.pop("output_format", None)
            data = self._post_music(s.minimax_api_host, payload, headers)

        body = data.get("data") or {}
        audio_url = body.get("audio_url") or body.get("url") or ""
        audio_hex = body.get("audio") or ""

        out = s.data_dir / "music" / f"minimax-{uuid.uuid4().hex[:12]}.mp3"
        if audio_url:
            path = download(audio_url, out)
        elif audio_hex:
            path = write_bytes(bytes.fromhex(audio_hex), out)
        else:
            raise RuntimeError(f"MiniMax 官方 API 未返回音频：{str(data)[:300]}")

        job_id = out.stem
        elapsed = time.time() - t0
        self._jobs[job_id] = req
        self._out[job_id] = GenResult(
            status="done", audio_path=path,
            # 官方不返回精确时长，先按请求时长记账，后续由音频分析环节用真实波形纠正
            duration_s=float(req.duration_s),
            bpm=0.0, cost=self.estimate_cost(req), elapsed_s=elapsed, raw_meta=data,
        )
        return job_id

    def _post_music(self, host: str, payload: dict, headers: dict) -> dict:
        r = requests.post(f"{host}/v1/music_generation", json=payload, headers=headers, timeout=600)
        r.raise_for_status()
        data = r.json()
        base = data.get("base_resp") or {}
        code = base.get("status_code", 0)
        if code not in (0, None):
            msg = str(base.get("status_msg") or "")
            # 个别账号/模型不接受 output_format 字段，去掉重试一次
            if "output_format" in payload and "output_format" in msg:
                return {"_retry_without_output_format": True}
            raise RuntimeError(f"MiniMax 返回错误 {code}：{msg}（trace_id={data.get('trace_id')}）")
        return data

    # ---------------------------------------------------------------- B. 本地服务
    def _base_local(self) -> str:
        return settings().minimax_local_endpoint.rstrip("/")

    def _generate_local(self, req: GenRequest) -> str:
        r = requests.post(f"{self._base_local()}/generate", timeout=120, json={
            "lyrics": req.lyrics,
            "prompt": req.style_prompt,
            "duration_s": req.duration_s,
            "vocal": req.vocal,
            "seed": req.seed,
        })
        r.raise_for_status()
        job_id = str(r.json().get("job_id") or "")
        if not job_id:
            raise RuntimeError(f"本地服务未返回 job_id：{r.text[:300]}")
        self._jobs[job_id] = req
        return job_id

    def _poll_local(self, job_id: str) -> GenResult:
        t0 = time.time()
        r = requests.get(f"{self._base_local()}/jobs/{job_id}", timeout=60)
        r.raise_for_status()
        data = r.json()
        status = str(data.get("status", "")).lower()
        if status != "done":
            return GenResult(status="running" if status != "failed" else "failed",
                             error=str(data)[:500], elapsed_s=time.time() - t0, raw_meta=data)
        path = data.get("audio_path") or ""
        if not path and data.get("audio_url"):
            dest = settings().data_dir / "music" / f"minimax-{job_id}.mp3"
            path = download(data["audio_url"], dest)
        req = self._jobs.get(job_id)
        return GenResult(
            status="done", audio_path=path,
            duration_s=float(data.get("duration_s") or (req.duration_s if req else 0)),
            bpm=float(data.get("bpm") or 0),
            cost=0.0, elapsed_s=time.time() - t0, raw_meta=data,
        )
