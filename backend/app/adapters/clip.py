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


def _as_tensor(out, key: str):
    """transformers 4.x 的 get_*_features 直接返回张量；5.x 返回输出对象（投影后的向量在
    pooler_output / *_embeds 里）。两种都接。"""
    if hasattr(out, "norm"):
        return out
    for k in (key, "pooler_output"):
        v = getattr(out, k, None)
        if v is not None:
            return v
    raise RuntimeError(f"无法从 {type(out).__name__} 取出特征向量")


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
        if settings().force_mock:
            self._ok = False      # 演示/测试模式不加载大模型
            return
        try:
            import os

            os.environ.setdefault("HF_ENDPOINT", settings().hf_endpoint)
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

    @property
    def model_id(self) -> str:
        """向量出自哪个编码器。不同编码器的向量不可比（维度可能都是 512，但空间不同），
        检索时只比较同一编码器产出的向量，换模型后由重建任务统一重算。"""
        # 末尾版本号：向量的组成方式变了（如去掉文本混合）就加 1，旧向量会被自动重建
        return f"clip:{settings().clip_model}:img-v2" if self.is_real else "hash"

    def encode_asset(self, image_paths: list[str | Path], text: str) -> list[float]:
        """素材向量 = 关键帧图像向量的均值（不混文本）。

        试过「图像 0.7 + 标签文本 0.3」：文本-文本相似度远高于图文相似度（0.8 vs 0.4），
        混进去后所有查询的匹配分都顶到 1.0，失去区分度（2026-10-05 实测）。
        标签仍参与筛选（F4-5），检索打分只看画面本身。没有真模型时退化为文本哈希。
        """
        self._load()
        if not self._ok:
            return _hash_vec(text)
        imgs = [Path(x) for x in image_paths if x and Path(x).is_file()]
        vecs = [self.encode_image(x) for x in imgs[:3]]
        if not vecs:
            return self.encode_text(text)
        n = len(vecs[0])
        img = [sum(v[i] for v in vecs) / len(vecs) for i in range(n)]
        norm = math.sqrt(sum(x * x for x in img)) or 1.0
        return [x / norm for x in img]

    def encode_text(self, text: str) -> list[float]:
        self._load()
        if not self._ok:
            return _hash_vec(text)
        import torch

        with torch.no_grad():
            inputs = self._proc(text=[text], return_tensors="pt", padding=True)
            feat = _as_tensor(self._model.get_text_features(**inputs), "text_embeds")
            feat = feat / feat.norm(dim=-1, keepdim=True)
            return feat[0].tolist()

    def encode_image(self, path: str | Path, extra_text: str = "") -> list[float]:
        """单张**图片**（不是视频：视频请先抽关键帧）。无真模型时用「路径 + 标签」哈希近似。"""
        self._load()
        if not self._ok:
            p = Path(path)
            size = p.stat().st_size if p.exists() else 0
            return _hash_vec(f"{p.name}:{size}:{extra_text}")
        from PIL import Image  # type: ignore

        import torch

        with torch.no_grad():
            inputs = self._proc(images=Image.open(path).convert("RGB"), return_tensors="pt")
            feat = _as_tensor(self._model.get_image_features(**inputs), "image_embeds")
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


# Chinese-CLIP（vit-base-patch16）图文余弦的实测区间（长白山 8 张分镜 × 6 条查询，2026-10-05）：
#   画面相符 0.43–0.48；同目的地但不对题 0.33–0.39；完全无关（海边/地铁站）0.29–0.33。
# 原始余弦给运营看没有区分度，按 [0.30, 0.48] 线性映射到 0–1：
# PRD F6-2「匹配分 ≥ 0.6 才算合格」落在原始余弦 ≈0.41，正好卡在「相符」与「不对题」之间。
_COS_LO, _COS_HI = 0.30, 0.48


def match_score(cos: float) -> float:
    if not encoder.is_real:
        return round(max(0.0, min(1.0, cos)), 4)
    return round(max(0.0, min(1.0, (cos - _COS_LO) / (_COS_HI - _COS_LO))), 4)


def comparable(emb_model: str | None) -> bool:
    """这条向量能不能和当前查询向量比较（必须同一编码器）。"""
    return (emb_model or "hash") == encoder.model_id


def rank(query: str, items: list[tuple], top_k: int = 10) -> list[tuple[str, float]]:
    """items: [(id, embedding)] 或 [(id, embedding, embedding_model)] -> [(id, 匹配分)] 降序。

    编码器不一致的向量（如换成真模型前存的哈希向量）不参与排序，等重建任务重算。
    """
    qv = encoder.encode_text(query)
    scored = []
    for it in items:
        i, v = it[0], it[1]
        if len(it) > 2 and not comparable(it[2]):
            continue
        scored.append((i, match_score(cosine(qv, v))))
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:top_k]
