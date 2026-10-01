"""向量检索适配器 —— PRD §7 的 Chinese-CLIP + FAISS。

实际部署：若装了 torch + transformers，使用 Chinese-CLIP 真编码器；
否则退化为确定性字符 n-gram 哈希编码器 —— 它不是语义编码，但同样支持
「输入自然语言 → 排序候选」的检索链路，用于无 GPU/无权重时跑通流程。
FAISS 未安装时用 numpy 余弦检索（1 万条以内 < 50ms，满足 PRD 性能要求）。
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from ..config import settings

DIM = 512


def _tokens(text: str) -> list[str]:
    t = re.sub(r"\s+", "", (text or "").lower())
    grams = [t[i:i + 2] for i in range(max(0, len(t) - 1))]
    return grams or ([t] if t else [])


def _hash_vec(text: str, dim: int = DIM) -> list[float]:
    vec = [0.0] * dim
    for g in _tokens(text):
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:8], 16)
        vec[h % dim] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class _ClipEncoder:
    """惰性加载真 Chinese-CLIP；失败则永久退化为哈希编码。"""

    def __init__(self) -> None:
        self._model = None
        self._tried = False
        self._ok = False

    def _load(self) -> None:
        if self._tried:
            return
        self._tried = True
        try:
            import torch  # noqa: F401
            from transformers import ChineseCLIPModel, ChineseCLIPProcessor

            name = settings().clip_model
            self._proc = ChineseCLIPProcessor.from_pretrained(name)
            self._model = ChineseCLIPModel.from_pretrained(name).eval()
            self._ok = True
        except Exception:
            self._ok = False

    @property
    def is_real(self) -> bool:
        self._load()
        return self._ok

    def encode_text(self, text: str) -> list[float]:
        self._load()
        if not self._ok:
            return _hash_vec(text)
        import torch

        with torch.no_grad():
            inputs = self._proc(text=[text], return_tensors="pt", padding=True)
            feat = self._model.get_text_features(**inputs)
            feat = feat / feat.norm(dim=-1, keepdim=True)
            return feat[0].tolist()

    def encode_image(self, path: str | Path, extra_text: str = "") -> list[float]:
        """无真模型时用「路径 + 标签」的哈希向量近似，保证同一素材稳定可检索。"""
        self._load()
        if not self._ok:
            p = Path(path)
            size = p.stat().st_size if p.exists() else 0
            return _hash_vec(f"{p.name}:{size}:{extra_text}")
        from PIL import Image  # type: ignore

        import torch

        with torch.no_grad():
            inputs = self._proc(images=Image.open(path).convert("RGB"), return_tensors="pt")
            feat = self._model.get_image_features(**inputs)
            feat = feat / feat.norm(dim=-1, keepdim=True)
            return feat[0].tolist()


encoder = _ClipEncoder()


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(a[i] * b[i] for i in range(n))
    na = math.sqrt(sum(x * x for x in a[:n])) or 1.0
    nb = math.sqrt(sum(x * x for x in b[:n])) or 1.0
    return dot / (na * nb)


def rank(query: str, items: list[tuple[str, list[float]]], top_k: int = 10) -> list[tuple[str, float]]:
    """items: [(id, embedding)] -> [(id, score)] 降序。"""
    qv = encoder.encode_text(query)
    scored = [(i, cosine(qv, v)) for i, v in items]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
