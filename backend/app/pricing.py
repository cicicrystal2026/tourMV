"""模型价格表与按 token 计费。

V1.1 之前：文字模型（qwen-plus）的费用全部记成 0，视觉模型按固定 ¥0.004/次估算。
做模型选型对比时这两项必须是真数，所以改为「记录每次调用的真实 token × 价格表」。

价格单位：元 / 百万 token，(输入, 输出)。来源与日期写在每一行，价格会变 ——
用环境变量 `MODEL_PRICES` 覆盖或新增，格式是 JSON：
    MODEL_PRICES={"qwen-plus":[0.8,2.0],"my-model":[1.0,4.0]}
不在表里的模型费用记 0，并在调用记录里标 price_known=false（界面显示「未配置单价」，
不会让人误以为真的不花钱）。
"""
from __future__ import annotations

import json
import os

# (输入, 输出) 元/百万 token
_DEFAULT: dict[str, tuple[float, float]] = {
    # 阿里云百炼 · 文字模型。qwen-plus 为第三方整理的优惠价（2026-10-10），以控制台账单为准
    "qwen-plus": (0.8, 2.0),
    "qwen-turbo": (0.3, 0.6),
    "qwen-flash": (0.15, 1.5),
    "qwen-max": (2.4, 9.6),
    "qwen3-max": (2.5, 10.0),
    # 阿里云百炼 · 视觉模型。qwen3-vl-plus 取自官方计费页（北京，0–32K 档）$0.1716 / $1.7208 折 7 元
    "qwen3-vl-plus": (1.2, 12.0),
    "qwen3-vl-flash": (0.15, 1.5),
    "qwen-vl-max-latest": (1.6, 4.0),
    "qwen-vl-plus": (0.8, 2.0),
    # DeepSeek 官方（如经兼容接口接入）
    "deepseek-chat": (2.0, 8.0),
    "deepseek-v3": (2.0, 8.0),
}


def table() -> dict[str, tuple[float, float]]:
    out = dict(_DEFAULT)
    raw = os.environ.get("MODEL_PRICES", "").strip()
    if raw:
        try:
            for k, v in json.loads(raw).items():
                out[str(k)] = (float(v[0]), float(v[1]))
        except (ValueError, TypeError, IndexError, KeyError):
            pass
    return out


def lookup(model: str) -> tuple[float, float] | None:
    """按模型名取单价；带日期后缀的快照名（qwen-plus-2025-07-28）回落到主名。"""
    t = table()
    m = (model or "").strip()
    if m in t:
        return t[m]
    for k in sorted(t, key=len, reverse=True):
        if m.startswith(k):
            return t[k]
    return None


def token_cost(model: str, input_tokens: int, output_tokens: int) -> tuple[float, bool]:
    """返回 (费用元, 单价是否已知)。"""
    p = lookup(model)
    if p is None:
        return 0.0, False
    return round((input_tokens * p[0] + output_tokens * p[1]) / 1_000_000, 6), True


def describe() -> list[dict]:
    return [{"model": k, "input_per_m": v[0], "output_per_m": v[1]} for k, v in sorted(table().items())]
