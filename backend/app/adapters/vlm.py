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

from .. import vocab

DIMS = vocab.DIMS

# 兜底路径用的地点类别词（真模型给的是具体景点名，不受这张表限制）
_PLACE_HINTS = ["古塔", "古城", "老街", "山川", "江河", "湖泊", "海岸", "古镇", "村落", "园林",
                "沙漠", "草原", "雪原", "石窟", "寺院", "梯田", "大桥", "城市天际线"]
VOCAB: dict[str, list[str]] = {"place": _PLACE_HINTS, **vocab.BASE}


def _system(destination: str) -> str:
    """打标提示词：受控维度把允许的取值逐个列给模型（PRD V1.1 F4-12），只能从中选。"""
    v = vocab.all_values()
    lines = "\n".join(f"- {k}（{label}）：{' / '.join(v[k])}" for k, label in (
        ("season", "季节"), ("time", "时段"), ("weather", "天气"), ("mood", "情绪"),
        ("camera", "镜头运动"), ("subject", "画面主体")))
    dest = f"这批关键帧来自目的地「{destination}」。" if destination else ""
    return f"""你是文旅视频素材的标注员。{dest}根据给定的视频关键帧输出标签。
下面六个维度**只能从给出的取值里选**，每个维度最多 2 个，看不出来就给空数组，不要自造词：
{lines}
另外：
- place：画面里的具体景点名（如「断桥」「天池」），2–10 个字的名称，不要写句子；认不出具体景点就给空数组
- confidence：你对整组标签的把握，0.0–1.0；低于 0.7 会转人工纠错，没把握就如实给低分
- has_identifiable_person：画面里有没有能认出长相的人（涉及肖像权）
只输出 JSON：
{{"place":[], "season":[], "time":[], "weather":[], "mood":[], "camera":[], "subject":[],
 "confidence": 0.0, "has_identifiable_person": false}}"""


# 备用模型：阿里云会下线旧模型（qwen-vl-plus 于 2026-10 起返回 404 Model not exist，
# 打标和图文预审同时失效且没有任何提示）。主模型不存在时按顺序换下一个，并记住可用的那个。
FALLBACK_MODELS = ["qwen3-vl-plus", "qwen3-vl-flash", "qwen-vl-max-latest"]
_working_model: str | None = None


def _chat(messages: list[dict], temperature: float = 0.2, timeout: int = 180) -> str:
    """调用 VLM，返回文本内容。模型不存在时自动换备用模型；其余错误带上游原因抛出。"""
    global _working_model
    from .llm import explain_error

    s = settings()
    order = [m for m in dict.fromkeys([_working_model or s.vlm_model, s.vlm_model, *FALLBACK_MODELS]) if m]
    last = ""
    for m in order:
        r = requests.post(f"{s.vlm_base_url}/chat/completions",
                          headers={"Authorization": f"Bearer {s.vlm_api_key}"},
                          json={"model": m, "messages": messages, "temperature": temperature},
                          timeout=timeout)
        if r.status_code == 404 or "model_not_found" in r.text[:300]:
            last = f"模型 {m} 不存在（可能已被平台下线）"
            continue
        if r.status_code >= 400:
            raise RuntimeError(explain_error(r.status_code, r.text))
        _working_model = m
        return r.json()["choices"][0]["message"]["content"]
    raise RuntimeError(f"{last}；备用模型也不可用，请在 .env 里把 VLM_MODEL 改成当前可用的视觉模型")


def current_model() -> str:
    return _working_model or settings().vlm_model


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


def _real(frame_paths: list[str], destination: str = "") -> dict:
    import time

    s = settings()
    t0 = time.time()
    content: list[dict] = [{"type": "text", "text": "标注这些关键帧。"}]
    for p in frame_paths[:4]:
        b64 = base64.b64encode(Path(p).read_bytes()).decode()
        content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}})
    raw = _chat([{"role": "system", "content": _system(destination)},
                 {"role": "user", "content": content}])
    raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(raw)
    # 过词表：模型偶尔仍会写近义词或整句，归并不了的直接丢弃
    for dim in DIMS:
        data[dim] = vocab.normalize_many(dim, data.get(dim) if isinstance(data.get(dim), list) else [data.get(dim)],
                                         limit=1 if dim == "place" else 2)
    try:
        data["confidence"] = max(0.0, min(1.0, float(data.get("confidence") or 0.0)))
    except (TypeError, ValueError):
        data["confidence"] = 0.0
    data["engine"] = "vlm"
    record(kind="vlm", provider="qwen-vl", model=current_model(), purpose="tagging", status="ok",
           elapsed_s=time.time() - t0, cost=0.004)
    return data


def tag_frames(frame_paths: list[str], hint: str = "", destination: str = "") -> dict:
    s = settings()
    if frame_paths and s.vlm_base_url and s.vlm_api_key and not s.force_mock:
        try:
            return _real(frame_paths, destination)
        except Exception as e:  # noqa: BLE001 - 打标失败不能阻塞上传
            record(kind="vlm", provider="qwen-vl", model=s.vlm_model, purpose="tagging",
                   status="error", error=str(e))
            # 真模型配了却失败：留空并标成待纠错，**不**用随机词补位 ——
            # 实测模型下线期间，「天池」被随机标成了「星空 / 动物」，比没有标签更糟
            return {**{d: [] for d in DIMS}, "confidence": 0.0, "has_identifiable_person": False,
                    "engine": "failed", "error": str(e)[:200]}
    return _heuristic(frame_paths, hint)


# ------------------------------------------------------------------ 图文一致度预审
JUDGE_SYSTEM = """你是严格的文旅 MV 分镜审核员，负责把「不能直接用」的画面挑出来交给人看。
给你一张画面、一句歌词和它对应的景点。逐项回答，宁严勿松：
place：画面像不像这个景点（地貌、建筑形制、植被、水色、气候）？
  "是" = 一眼能认出；"存疑" = 泛泛风景、辨识度弱、或更像别处；"否" = 明显是别的地方
lyric：画面有没有表现这句歌词的具体意象？"是" / "部分" / "否"
text：画面里有没有任何文字、汉字、字母或类似文字的刻痕/符号（碑刻、牌匾、招牌都算）？"有" / "无"。
  有的话照样读出来写进 reason —— 生图模型写的字几乎都是乱码，有字就是硬伤
defects：其他硬伤列表（水印、人物畸形、明显 AI 痕迹、年代或地域错误元素），没有就给 []
reason：30 字以内，先说最主要的问题；没有问题才写优点
只输出 JSON：{"place":"是|存疑|否","lyric":"是|部分|否","text":"有|无","defects":[],"reason":"…"}"""

# 分数由代码按结构化结论计算，不让模型自己给分：
# 实测模型常常理由写着「疑似黄石公园」、分数却给 85，自由打分不可靠
_PLACE_PENALTY = {"是": 0, "存疑": 25, "否": 50}
_LYRIC_PENALTY = {"是": 0, "部分": 12, "否": 30}


def _score(data: dict) -> int:
    place = str(data.get("place", "存疑")).strip()
    lyric = str(data.get("lyric", "部分")).strip()
    defects = [d for d in (data.get("defects") or []) if str(d).strip()]
    has_text = str(data.get("text", "无")).strip() == "有"
    score = (95 - _PLACE_PENALTY.get(place, 25) - _LYRIC_PENALTY.get(lyric, 12)
             - 20 * len(defects) - (40 if has_text else 0))
    return max(0, min(100, score))


def judge_ready() -> bool:
    s = settings()
    return bool(s.vlm_base_url and s.vlm_api_key) and not s.force_mock


def judge_image(img_path: str, line: str, place: str, destination: str,
                project_id: str = "") -> dict | None:
    """视觉模型给「这张图配这句歌词」打分（PRD V1.1 审核减负：机器先审、人只看例外）。

    未配置或调用失败返回 None —— 界面显示「未预审」，绝不伪造分数。
    """
    import time

    if not judge_ready() or not img_path or not Path(img_path).exists():
        return None
    s = settings()
    b64 = base64.b64encode(Path(img_path).read_bytes()).decode()
    t0 = time.time()
    try:
        raw = _chat([
            {"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": f"目的地：{destination}\n景点：{place}\n歌词：{line}"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ], temperature=0.1, timeout=120)
        raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(raw)
        score = _score(data)
        # qwen-vl-plus 单图一次约千余 token，按 ¥0.004/次估
        record(kind="vlm", provider="qwen-vl", model=s.vlm_model, purpose="shot_judge",
               status="ok", elapsed_s=time.time() - t0, cost=0.004, project_id=project_id)
        reason = str(data.get("reason") or "")[:120]
        if str(data.get("text", "")).strip() == "有" and "文字" not in reason:
            reason = ("画面含文字（AI 生成的字多为乱码，需人工核对）；" + reason)[:120]
        return {"score": score, "reason": reason}
    except Exception as e:  # noqa: BLE001 - 预审失败不影响出图，人工照常审
        record(kind="vlm", provider="qwen-vl", model=s.vlm_model, purpose="shot_judge",
               status="error", elapsed_s=time.time() - t0, error=str(e), project_id=project_id)
        return None
