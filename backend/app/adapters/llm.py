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
            if r.status_code >= 400:
                raise RuntimeError(explain_error(r.status_code, r.text))
            content = r.json()["choices"][0]["message"]["content"]
            record(kind="llm", provider=self.provider, model=self.model, purpose=purpose,
                   params={"messages": len(messages)}, status="ok",
                   elapsed_s=time.time() - t0, project_id=project_id)
            return content
        except Exception as e:
            self.last_error = str(e)[:300]
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


def explain_error(status: int, text: str) -> str:
    """把上游错误翻成运营看得懂的话，同时保留原始错误码便于排查。

    V1.0 只记了「400 Client Error: Bad Request」，实际原因是阿里云百炼账户欠费（Arrearage），
    系统悄悄换成模板歌词，页面上看不出任何异常。
    """
    import json as _json

    code, msg = "", text[:200]
    try:
        err = (_json.loads(text) or {}).get("error") or {}
        code, msg = str(err.get("code") or err.get("type") or ""), str(err.get("message") or msg)
    except ValueError:
        pass
    hint = {
        "Arrearage": "阿里云百炼（DashScope）账户欠费，请到阿里云控制台充值后重试",
        "InvalidApiKey": "API Key 无效，请检查 .env 里的 LLM_API_KEY",
        "invalid_api_key": "API Key 无效，请检查 .env 里的 LLM_API_KEY",
        "Throttling": "调用太频繁被限流，请稍后重试",
        "DataInspectionFailed": "内容被平台安全审核拦截，请调整简报或歌词后重试",
    }.get(code) or ("API Key 无效或无权限" if status in (401, 403) else "")
    head = f"{hint}（" if hint else ""
    tail = "）" if hint else ""
    return f"{head}HTTP {status} {code}: {msg[:160]}{tail}"


llm = LLMAdapter()
llm.last_error = ""
