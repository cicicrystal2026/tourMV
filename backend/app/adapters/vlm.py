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
        r = requests.post(
            f"{s.vlm_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {s.vlm_api_key}"},
            json={
                "model": s.vlm_model,
                "messages": [
                    {"role": "system", "content": JUDGE_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": f"目的地：{destination}\n景点：{place}\n歌词：{line}"},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ]},
                ],
                "temperature": 0.1,
            },
            timeout=120,
        )
        r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
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
