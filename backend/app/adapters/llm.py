"""LLM 适配器 —— OpenAI 兼容接口（通义千问 / DeepSeek 均可，切换只改 .env）。

关键设计：所有调用都要求调用方提供 fallback 载荷。
未配置 Key 或 FORCE_MOCK=1 时直接返回 fallback，使全流程在无 Key 状态下可跑通，
且降级是「静默可用」而非抛错——实验室阶段这一点很重要。
"""
from __future__ import annotations

import json
import re
import time

import requests

from ..config import settings
from ..observability import record

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def _strip_fence(text: str) -> str:
    m = _FENCE.search(text or "")
    return (m.group(1) if m else text or "").strip()


class LLMAdapter:
    provider = "openai-compatible"

    def __init__(self) -> None:
        s = settings()
        self.base_url = s.llm_base_url
        self.api_key = s.llm_api_key
        self.model = s.llm_model

    @property
    def ready(self) -> bool:
        s = settings()
        return s.llm_ready

    def chat(self, messages: list[dict], *, json_mode: bool = False, temperature: float = 0.3,
             purpose: str = "", project_id: str = "") -> str:
        if not self.ready:
            raise RuntimeError("LLM 未配置（LLM_BASE_URL / LLM_API_KEY 缺失或 FORCE_MOCK=1）")
        t0 = time.time()
        try:
            r = requests.post(
                f"{self.base_url}/chat/completions",
                timeout=180,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "temperature": temperature,
                    **({"response_format": {"type": "json_object"}} if json_mode else {}),
                },
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            record(kind="llm", provider=self.provider, model=self.model, purpose=purpose,
                   params={"messages": len(messages)}, status="ok",
                   elapsed_s=time.time() - t0, project_id=project_id)
            return content
        except Exception as e:
            record(kind="llm", provider=self.provider, model=self.model, purpose=purpose,
                   status="error", elapsed_s=time.time() - t0, error=str(e), project_id=project_id)
            raise

    def json_chat(self, messages: list[dict], *, fallback: dict | None = None,
                  purpose: str = "", project_id: str = "", temperature: float = 0.3) -> tuple[dict, bool]:
        """返回 (payload, 是否走了真实模型)。失败时降级到 fallback，绝不中断流程。"""
        if not self.ready:
            return (fallback or {}), False
        try:
            raw = self.chat(messages, json_mode=True, temperature=temperature,
                            purpose=purpose, project_id=project_id)
            return json.loads(_strip_fence(raw)), True
        except Exception:
            return (fallback or {}), False


llm = LLMAdapter()
