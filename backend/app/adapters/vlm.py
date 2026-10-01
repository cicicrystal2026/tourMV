"""视觉理解适配器 —— 素材自动打标（PRD F4-2，七个维度 + 置信度）。

真路径：Qwen-VL（API）或 Qwen2.5-VL-7B（本地）。
兜底路径：从文件名/人工提示中抽取关键词，未命中的维度用确定性伪随机补齐并给低置信度，
从而让「置信度 < 0.7 进入打标待纠错」这条业务规则在无模型时也能被真实触发。
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
from pathlib import Path

import requests

from ..config import settings
from ..observability import record

DIMS = ["place", "season", "time", "weather", "mood", "camera", "subject"]

VOCAB: dict[str, list[str]] = {
    "place": ["古塔", "古城", "老街", "山川", "江河", "湖泊", "海岸", "古镇", "村落", "园林",
              "沙漠", "草原", "雪原", "石窟", "寺院", "梯田", "大桥", "城市天际线"],
    "season": ["春", "夏", "秋", "冬"],
    "time": ["清晨", "正午", "黄昏", "夜晚", "星空"],
    "weather": ["晴", "阴", "雨", "雾", "雪", "多云"],
    "mood": ["宁静", "壮阔", "治愈", "热闹", "神秘", "苍凉"],
    "camera": ["慢推", "平移", "航拍", "环绕", "手持", "固定", "延时", "慢摇"],
    "subject": ["人物", "建筑", "山水", "花木", "动物", "街景", "无人"],
}

SYSTEM = """你是文旅视频素材的标注员。根据给定的视频关键帧，输出七个维度的中文标签。
只输出 JSON，结构为：
{"place":[], "season":[], "time":[], "weather":[], "mood":[], "camera":[], "subject":[],
 "confidence": 0.0-1.0, "has_identifiable_person": true/false}
要求：宁缺毋滥，不确定的维度给空数组；置信度低于 0.7 表示需要人工纠错。"""


def _heuristic(frame_paths: list[str], hint: str = "") -> dict:
    """按文件名/提示词命中词表；命中越多置信度越高。"""
    hay = (hint + " " + " ".join(Path(p).stem for p in frame_paths)).lower()
    seed = int(hashlib.md5(hay.encode("utf-8")).hexdigest()[:8], 16)
    rnd = random.Random(seed)
    out: dict[str, list[str]] = {}
    hits = 0
    for dim, words in VOCAB.items():
        matched = [w for w in words if w.lower() in hay]
        if matched:
            hits += 1
            out[dim] = matched[:3]
        else:
            out[dim] = []
    # 未命中的维度随机补 1 个（模拟"模型给了但没把握"）
    for dim in DIMS:
        if not out[dim]:
            out[dim] = [rnd.choice(VOCAB[dim])]
    confidence = round(min(0.95, 0.42 + 0.09 * hits + rnd.random() * 0.08), 2)
    out["confidence"] = confidence
    out["has_identifiable_person"] = "人物" in out.get("subject", [])
    out["engine"] = "heuristic"
    return out


def _real(frame_paths: list[str]) -> dict:
    s = settings()
    content: list[dict] = [{"type": "text", "text": "标注这些关键帧。"}]
    for p in frame_paths[:4]:
        b64 = base64.b64encode(Path(p).read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    r = requests.post(
        f"{s.vlm_base_url}/chat/completions",
        headers={"Authorization": f"Bearer {s.vlm_api_key}"},
        json={
            "model": s.vlm_model,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": content}],
            "temperature": 0.2,
        },
        timeout=180,
    )
    r.raise_for_status()
    raw = r.json()["choices"][0]["message"]["content"]
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    data["engine"] = "vlm"
    return data


def tag_frames(frame_paths: list[str], hint: str = "") -> dict:
    s = settings()
    if s.vlm_base_url and s.vlm_api_key and not s.force_mock:
        try:
            return _real(frame_paths)
        except Exception as e:  # noqa: BLE001 - 打标失败不能阻塞上传
            record(kind="vlm", provider="qwen-vl", model=s.vlm_model, purpose="tagging",
                   status="error", error=str(e))
    return _heuristic(frame_paths, hint)
