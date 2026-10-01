"""项目状态机 —— 严格对应 PRD §4.2。

进入「待人工」状态时流程暂停；任一人工节点可驳回到上游状态重新生成。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StateSpec:
    key: str
    label: str
    column: str  # 看板列
    next: str | None
    human: bool = False
    enter_note: str = ""


STATES: dict[str, StateSpec] = {
    # 确认简报也是人工节点（PRD V1.1 §4.2）：V1.0 标成非人工，看板「待人工处理」就漏算了它
    "briefing": StateSpec("briefing", "简报中", "创作简报", "lyric_generating", True, "创作简报待确认"),
    "lyric_generating": StateSpec("lyric_generating", "写词中", "词曲生成", "lyric_review", False, "LLM 生成歌词与自动检查"),
    "lyric_review": StateSpec("lyric_review", "待确认歌词", "词曲生成", "music_generating", True, "等待人工确认歌词"),
    "music_generating": StateSpec("music_generating", "生曲中", "词曲生成", "music_review", False, "多模型并行生成候选"),
    "music_review": StateSpec("music_review", "待选曲", "词曲生成", "storyboard_generating", True, "等待人工评分选定"),
    # 逐句配图：每句歌词配一张真实景区图，出图后必须人工审核
    "storyboard_generating": StateSpec("storyboard_generating", "配图中", "影像匹配", "storyboard_review", False, "按歌词逐句生成景区配图"),
    "storyboard_review": StateSpec("storyboard_review", "待审配图", "影像匹配", "audio_analyzing", True, "等待人工审核逐句配图"),
    "audio_analyzing": StateSpec("audio_analyzing", "音频分析", "影像匹配", "rough_cutting", False, "节拍检测与歌词时间轴对齐"),
    "rough_cutting": StateSpec("rough_cutting", "粗剪中", "影像匹配", "edit_review", False, "镜头匹配与卡点"),
    "edit_review": StateSpec("edit_review", "待精剪", "影像匹配", "rendering", True, "等待人工精剪确认"),
    "rendering": StateSpec("rendering", "合成中", "多版本合成", "delivered", False, "多版本合成"),
    "delivered": StateSpec("delivered", "已交付", "交付", None, False, "成片可下载，可回填投放数据"),
}

COLUMNS: list[str] = ["创作简报", "词曲生成", "影像匹配", "多版本合成", "交付"]

HUMAN_STATES: list[str] = [k for k, v in STATES.items() if v.human]

# 人工节点可驳回的上游状态（PRD §4.2「任一人工节点可驳回到上游状态重新生成」）
REJECT_TARGETS: dict[str, list[str]] = {
    "lyric_review": ["briefing", "lyric_generating"],
    "music_review": ["lyric_review", "music_generating"],
    "storyboard_review": ["music_review", "storyboard_generating"],
    "edit_review": ["storyboard_review", "music_review", "rough_cutting", "audio_analyzing"],
}

# 待办类型标签（看板 F1-5）
TODO_TYPE: dict[str, str] = {
    "briefing": "简报",
    "lyric_review": "歌词",
    "music_review": "选曲",
    "storyboard_review": "配图",
    "edit_review": "精剪",
}


class StateTransitionError(Exception):
    pass


def spec(state: str) -> StateSpec:
    if state not in STATES:
        raise StateTransitionError(f"未知状态：{state}")
    return STATES[state]


def next_state(state: str) -> str | None:
    return spec(state).next


def can_advance(state: str) -> bool:
    return STATES.get(state, StateSpec("", "", "", None)).next is not None


def is_human(state: str) -> bool:
    return state in HUMAN_STATES


def column_of(state: str) -> str:
    return spec(state).column


def assert_can_reject(state: str, target: str) -> None:
    allowed = REJECT_TARGETS.get(state, [])
    if target not in allowed:
        raise StateTransitionError(
            f"当前状态「{STATES[state].label if state in STATES else state}」"
            f"不能驳回到「{target}」，可选：{allowed or '无'}"
        )


def describe() -> list[dict]:
    return [
        {
            "key": s.key,
            "label": s.label,
            "column": s.column,
            "next": s.next,
            "human": s.human,
            "note": s.enter_note,
        }
        for s in STATES.values()
    ]
