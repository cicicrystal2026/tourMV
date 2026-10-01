"""Google Lyria（Gemini API）音乐生成适配器。

选它的理由（2026-09 核价）：
- **官方 API 里最便宜**：$0.08/首（按首，不按分钟），约为 ElevenLabs 的 1/4
- 44.1kHz 立体声，支持**自定义歌词**（`[Verse]` / `[Chorus]` / `[Bridge]` 段落标签），
  多语言人声（中文可用），长度靠提示词控制（约 2–3 分钟）
- 同步返回（`generateContent` 一次调用即出音频），无需轮询任务

必须知道的三个约束：
1. **单一轮生成，没有二次编辑**。想改某一段只能整首重生成 —— 因此不适合「只重写副歌」这类
   局部迭代；这类需求走本项目的「局部重写歌词 → 整首重新生成」路径。
2. **每首输出都带 SynthID 水印**（人耳不可闻，但是持续性的、可被检测的标识）。
3. 提示词若点名具体歌手音色、或要求生成受版权保护的歌词，会被安全策略拦截。

⚠️ 请求字段（`generationConfig.responseModalities`、`inlineData`）按 Gemini API 公开文档编写，
W1 首次真实调用时必须对照当期文档核对一次。任何失败都会被上层 base.resolve() 捕获并降级到
Mock，不会中断流程。
"""
from __future__ import annotations

import base64
import os
import re
import uuid

import requests

from ...config import settings
from ..media import write_bytes
from .base import GenRequest, GenResult, MusicAdapter

# 官方定价 $0.08/首（Gemini API 付费层，无免费额度）。按汇率折人民币。
PRICE_PER_SONG_USD = float(os.environ.get("LYRIA_PRICE_PER_SONG_USD", "0.08"))


class LyriaAdapter(MusicAdapter):
    key = "lyria"
    name = "Google Lyria 3.5"
    run_mode = "api"
    price_per_min = 0.0  # 按首计价，见 estimate_cost

    @property
    def ready(self) -> bool:
        s = settings()
        return bool(s.gemini_api_key) and not s.force_mock

    def capabilities(self) -> dict:
        return {
            "supports_chinese": True,        # 官方声明多语言人声；中文咬字质量待实听
            "max_duration_s": 180,           # 文档为「a couple of minutes」，最长约 3 分钟
            "supports_custom_lyrics": True,
            "concurrency": 2,
            "single_turn": True,             # 不支持对已生成音频做二次编辑
            "watermark": "SynthID",
        }

    def estimate_cost(self, req: GenRequest) -> float:
        # 按首计价，与时长无关
        return round(PRICE_PER_SONG_USD * settings().usd_cny, 4)

    # ---------------------------------------------------------------- 提交
    def generate(self, req: GenRequest) -> str:
        """Lyria 是同步接口：这一次调用就把音频拿回来了。"""
        s = settings()
        body = {
            "contents": [{"parts": [{"text": self._build_prompt(req)}]}],
            "generationConfig": {"responseModalities": ["AUDIO"]},
        }
        r = requests.post(
            f"{s.gemini_base_url}/v1beta/models/{s.lyria_model}:generateContent",
            headers={"x-goog-api-key": s.gemini_api_key, "Content-Type": "application/json"},
            json=body, timeout=300,
        )
        if r.status_code >= 400:
            # 把 Google 的结构化错误带出来，便于定位（安全拦截 / 配额 / Key 无效）
            raise RuntimeError(f"Lyria HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()

        audio, mime = self._extract_audio(data)
        if not audio:
            raise RuntimeError(f"Lyria 未返回音频块：{str(data)[:400]}")

        job_id = f"lyria-{uuid.uuid4().hex[:12]}"
        ext = ".wav" if "wav" in (mime or "").lower() else ".mp3"
        dest = s.data_dir / "music" / f"{job_id}{ext}"
        write_bytes(audio, dest)
        self._jobs[job_id] = req
        self._out[job_id] = GenResult(
            status="done", audio_path=str(dest),
            duration_s=float(req.duration_s), bpm=0.0,
            cost=self.estimate_cost(req), elapsed_s=0.0,
            raw_meta={"model": s.lyria_model, "mime": mime, "bytes": len(audio),
                      "lyrics_from_model": self._extract_lyrics(data)},
        )
        return job_id

    def get_result(self, job_id: str) -> GenResult:
        return self._out.get(job_id, GenResult(status="failed", error="未知 job_id"))

    def note(self) -> str:
        s = settings()
        if not s.gemini_api_key:
            return "未配置：填 GEMINI_API_KEY 即可（Gemini API 付费层，$0.08/首，无免费额度）。"
        return (f"官方 API（{s.lyria_model}）：${PRICE_PER_SONG_USD:.2f}/首 ≈ "
                f"¥{PRICE_PER_SONG_USD * s.usd_cny:.2f}，44.1kHz 立体声。"
                "单轮生成无法局部改，输出带 SynthID 水印。")

    # ---------------------------------------------------------------- 内部
    def _build_prompt(self, req: GenRequest) -> str:
        """官方建议把「歌词」与「音乐指令」分开写，这里按该建议组织提示词。"""
        secs = max(30, int(req.duration_s or 150))
        m, s2 = divmod(secs, 60)
        dur = f"{m} 分 {s2} 秒" if m else f"{secs} 秒"
        vocal = {"female": "女声独唱", "male": "男声独唱", "choir": "合唱"}.get(req.vocal, "人声")
        lines = [
            f"请创作一首约 {dur} 的中文歌曲。",
            f"音乐风格与情绪：{req.style_prompt}",
            f"演唱方式：{vocal}，中文咬字务必清晰准确。",
        ]
        if req.title:
            lines.append(f"歌曲标题：《{req.title}》")
        if req.lyrics.strip():
            lines += ["", "必须逐字使用以下歌词，并保留其中的段落标记：", req.lyrics.strip()]
        return "\n".join(lines)

    @staticmethod
    def _iter_parts(data: dict):
        for cand in (data.get("candidates") or []):
            content = cand.get("content") or {}
            for p in (content.get("parts") or []):
                yield p

    def _extract_audio(self, data: dict) -> tuple[bytes, str]:
        """兼容 camelCase（REST）与 snake_case（部分 SDK 文档）两种字段名。"""
        for p in self._iter_parts(data):
            blob = p.get("inlineData") or p.get("inline_data")
            if not blob or not blob.get("data"):
                continue
            return (base64.b64decode(blob["data"]),
                    blob.get("mimeType") or blob.get("mime_type") or "")
        return b"", ""

    def _extract_lyrics(self, data: dict) -> str:
        # Lyria 会在 text part 里返回 [[A0]] / [[B1]] 这类段落结构标记 —— 那不是歌词，
        # 直接当歌词存下来会污染 raw_meta，这里把纯标记的 part 滤掉。
        texts = (p["text"].strip() for p in self._iter_parts(data) if p.get("text"))
        keep = [t for t in texts
                if t and not re.fullmatch(r"(?:\[\[[A-Za-z0-9_]+\]\]\s*)+", t)]
        return "\n".join(keep)[:2000]
