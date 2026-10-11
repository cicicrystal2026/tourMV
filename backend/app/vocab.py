"""素材标签受控词表（PRD V1.1 F4-12）。

- 季节 / 时段 / 天气 / 情绪 / 镜头运动 / 画面主体：取值只能来自词表；
  模型或人写了近义词（傍晚、静谧…）先归并到词表词，归并不了的丢弃 / 拒绝。
- 地点（place）是两级里的第二级「景点」：第一级是素材的 destination 字段。
  景点是专有名词，没法穷举，只要求是**短名称**（去掉括号说明、不超过 20 字、不是句子）。
- 人工新增取值走 add_value()，写入 data/vocab.json，属于词表维护动作，不是随手打字。

V1.0 的问题：生图提示词被整句写进「画面主体」（最长 91 字），情绪维度有 224 个互不相同的值，
筛选下拉里全是整句，筛选等于失效。
"""
from __future__ import annotations

import json
import re
import threading

from .config import settings

CONTROLLED = ["season", "time", "weather", "mood", "camera", "subject"]
DIMS = ["place", *CONTROLLED]
PLACE_MAX = 20

BASE: dict[str, list[str]] = {
    "season": ["春", "夏", "秋", "冬"],
    "time": ["清晨", "正午", "黄昏", "夜晚", "星空"],
    "weather": ["晴", "多云", "阴", "雨", "雾", "雪"],
    "mood": ["宁静", "壮阔", "治愈", "热闹", "神秘", "苍凉", "怀旧", "浪漫", "庄严"],
    "camera": ["固定", "慢推", "平移", "慢摇", "环绕", "航拍", "手持", "延时"],
    "subject": ["山水", "建筑", "街景", "花木", "动物", "人物", "无人"],
}

# 近义词 → 词表词（只收常见写法；收不全没关系，归并不了的会被丢弃而不是混进筛选项）
SYNONYMS: dict[str, dict[str, str]] = {
    "season": {"春季": "春", "春天": "春", "夏季": "夏", "夏天": "夏", "秋季": "秋", "秋天": "秋",
               "冬季": "冬", "冬天": "冬", "深秋": "秋", "初春": "春", "盛夏": "夏", "隆冬": "冬"},
    "time": {"早晨": "清晨", "日出": "清晨", "晨曦": "清晨", "拂晓": "清晨", "上午": "正午", "中午": "正午",
             "白天": "正午", "午后": "正午", "下午": "正午", "傍晚": "黄昏", "日落": "黄昏", "夕阳": "黄昏",
             "暮色": "黄昏", "夜": "夜晚", "夜景": "夜晚", "深夜": "夜晚", "夜间": "夜晚", "银河": "星空"},
    "weather": {"晴天": "晴", "晴朗": "晴", "阴天": "阴", "雨天": "雨", "小雨": "雨", "下雨": "雨",
                "雾气": "雾", "薄雾": "雾", "晨雾": "雾", "云雾": "雾", "下雪": "雪", "雪天": "雪",
                "积雪": "雪", "少云": "多云", "云": "多云"},
    "mood": {"静谧": "宁静", "安静": "宁静", "恬静": "宁静", "平静": "宁静", "悠然": "宁静", "闲适": "宁静",
             "壮丽": "壮阔", "雄伟": "壮阔", "磅礴": "壮阔", "辽阔": "壮阔", "恢弘": "壮阔", "震撼": "壮阔",
             "温暖": "治愈", "温馨": "治愈", "清新": "治愈", "舒缓": "治愈", "喧闹": "热闹", "繁华": "热闹",
             "欢快": "热闹", "烟火气": "热闹", "幽深": "神秘", "空灵": "神秘", "梦幻": "神秘",
             "荒凉": "苍凉", "孤寂": "苍凉", "冷峻": "苍凉", "清冷": "苍凉", "肃杀": "苍凉",
             "复古": "怀旧", "古朴": "怀旧", "沧桑": "怀旧", "诗意": "浪漫", "唯美": "浪漫",
             "肃穆": "庄严", "庄重": "庄严", "神圣": "庄严"},
    "camera": {"静止": "固定", "定镜": "固定", "推进": "慢推", "推镜": "慢推", "推": "慢推", "拉远": "慢推",
               "横移": "平移", "摇": "慢摇", "摇镜": "慢摇", "俯拍": "航拍", "鸟瞰": "航拍", "无人机": "航拍",
               "跟拍": "手持", "延时摄影": "延时"},
    "subject": {"风景": "山水", "山川": "山水", "湖泊": "山水", "江河": "山水", "自然风光": "山水",
                "古建筑": "建筑", "寺庙": "建筑", "塔": "建筑", "桥": "建筑", "街道": "街景", "市集": "街景",
                "植物": "花木", "树木": "花木", "花": "花木", "森林": "花木", "鸟": "动物",
                "人": "人物", "游客": "人物", "空镜": "无人"},
}

_LOCK = threading.Lock()
_PUNCT_SENT = re.compile(r"[，。、；：！？,.!?;:\n]")
_BRACKETS = re.compile(r"[（(【\[].*?[）)】\]]")


def _extra_path():
    return settings().data_dir / "vocab.json"


def _extra() -> dict[str, list[str]]:
    try:
        d = json.loads(_extra_path().read_text(encoding="utf-8"))
        return {k: [str(x) for x in v] for k, v in d.items() if k in CONTROLLED and isinstance(v, list)}
    except (OSError, ValueError):
        return {}


def values(dim: str) -> list[str]:
    """某个受控维度当前允许的全部取值（内置 + 维护时新增的）。"""
    if dim not in CONTROLLED:
        return []
    out = list(BASE[dim])
    for v in _extra().get(dim, []):
        if v not in out:
            out.append(v)
    return out


def all_values() -> dict[str, list[str]]:
    return {d: values(d) for d in CONTROLLED}


def clean_place(value: str) -> str:
    """景点名：去括号说明，遇到句读就截断，最长 PLACE_MAX 字。空串表示这不是一个可用的景点名。"""
    v = _BRACKETS.sub("", str(value or "")).strip().replace("•", "·")
    v = _PUNCT_SENT.split(v)[0].strip()
    return v[:PLACE_MAX]


def normalize(dim: str, value: str) -> str | None:
    """把一个标签值规范成可入库的值；不合规返回 None。"""
    v = str(value or "").strip()
    if not v or dim not in DIMS:
        return None
    if dim == "place":
        return clean_place(v) or None
    allowed = values(dim)
    if v in allowed:
        return v
    return SYNONYMS.get(dim, {}).get(v) if SYNONYMS.get(dim, {}).get(v) in allowed else None


def normalize_many(dim: str, vals, limit: int = 3) -> list[str]:
    out: list[str] = []
    for v in vals or []:
        n = normalize(dim, v)
        if n and n not in out:
            out.append(n)
    return out[:limit]


def add_value(dim: str, value: str) -> list[str]:
    """词表维护：给受控维度新增一个取值（2–6 个字的短词）。"""
    v = str(value or "").strip()
    if dim not in CONTROLLED:
        raise ValueError("只有季节、时段、天气、情绪、镜头运动、画面主体可以维护词表；地点直接填景点名")
    if not 1 <= len(v) <= 6 or _PUNCT_SENT.search(v) or " " in v:
        raise ValueError("词表取值必须是 1–6 个字的短词，不能是句子")
    with _LOCK:
        extra = _extra()
        if v not in values(dim):
            extra.setdefault(dim, []).append(v)
            _extra_path().write_text(json.dumps(extra, ensure_ascii=False, indent=1), encoding="utf-8")
    return values(dim)
