"""数据模型 —— 对应 PRD §8。全部表含 id(UUID) / created_at / updated_at。

与 PRD 的一处有意偏差：PRD 的 gpu_job 只描述本地任务，但 §9 又要求
GET /api/jobs 统一列任务。这里把它泛化成 job 表（增加 queue 字段区分 api/gpu），
其余字段保持 PRD 语义。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class IdMixin:
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now, onupdate=_now)


# --------------------------------------------------------------------- 项目
class Project(IdMixin, TimestampMixin, Base):
    __tablename__ = "project"

    name: Mapped[str] = mapped_column(String(200))
    destination: Mapped[str] = mapped_column(String(100), default="")
    type: Mapped[str] = mapped_column(String(20), default="sample")  # sample / demo
    language: Mapped[str] = mapped_column(String(40), default="zh")  # 演唱语种
    subtitle_language: Mapped[str] = mapped_column(String(40), default="zh_en")
    status: Mapped[str] = mapped_column(String(40), default="briefing", index=True)
    selected_candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)  # 累计 API 费用(元)
    status_note: Mapped[str] = mapped_column(String(300), default="")
    # 驳回/返工一次记一笔，用于复盘
    reject_count: Mapped[int] = mapped_column(Integer, default=0)
    # PRD V1.1 §4.2 已交付锁定：非空即只读，要改只能「新建修订版」
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 修订版指向的原项目
    parent_project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class Brief(IdMixin, TimestampMixin, Base):
    __tablename__ = "brief"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("project.id"), index=True)
    mood: Mapped[list] = mapped_column(JSON, default=list)
    genre: Mapped[str] = mapped_column(String(120), default="")
    bpm_min: Mapped[int] = mapped_column(Integer, default=70)
    bpm_max: Mapped[int] = mapped_column(Integer, default=100)
    duration_s: Mapped[int] = mapped_column(Integer, default=180)
    vocal: Mapped[str] = mapped_column(String(20), default="female")
    style_prompt: Mapped[str] = mapped_column(Text, default="")
    trend_card_ids: Mapped[list] = mapped_column(JSON, default=list)
    kb_refs: Mapped[list] = mapped_column(JSON, default=list)  # 知识库未建，保留字段为空
    forbidden: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------- 词曲
class LyricVersion(IdMixin, TimestampMixin, Base):
    __tablename__ = "lyric_version"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("project.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    # [{name: "主歌1", lines: ["..."]}]
    sections: Mapped[list] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(20), default="ai")  # ai / manual
    # 自动检查是否已跑完。没有这个标记时，「还没检查」和「检查后零疑点」在数据上
    # 无法区分，前端就会在检查跑完之前显示「全部通过」（V1.0 现网的真实缺陷）。
    checks_done: Mapped[bool] = mapped_column(Boolean, default=False)


class LyricCheck(IdMixin, TimestampMixin, Base):
    __tablename__ = "lyric_check"

    lyric_version_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("lyric_version.id"), index=True
    )
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(20))  # rhyme / meter / fact
    line_no: Mapped[int] = mapped_column(Integer, default=0)
    section: Mapped[str] = mapped_column(String(60), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    # pending / adopted / kept（保留原文，须有理由）；resolved 为 V1.0 历史值，等同 kept
    status: Mapped[str] = mapped_column(String(20), default="pending")
    evidence: Mapped[str] = mapped_column(Text, default="")
    suggestions: Mapped[list] = mapped_column(JSON, default=list)
    keep_reason: Mapped[str] = mapped_column(Text, default="")
    # 史实疑点没有可追溯出处时为 True（知识库上线前所有史实疑点都是这种）
    needs_manual: Mapped[bool] = mapped_column(Boolean, default=False)


# --------------------------------------------------------------------- 音乐
class MusicModel(IdMixin, TimestampMixin, Base):
    __tablename__ = "music_model"

    key: Mapped[str] = mapped_column(String(40), unique=True)  # mureka / eleven / minimax
    name: Mapped[str] = mapped_column(String(120))
    run_mode: Mapped[str] = mapped_column(String(20), default="api")  # api / local
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    available: Mapped[bool] = mapped_column(Boolean, default=True)  # Key/显存就绪
    wins: Mapped[int] = mapped_column(Integer, default=0)
    rounds: Mapped[int] = mapped_column(Integer, default=0)
    score_sum: Mapped[float] = mapped_column(Float, default=0.0)
    score_n: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str] = mapped_column(Text, default="")

    @property
    def avg_score(self) -> float:
        return round(self.score_sum / self.score_n, 2) if self.score_n else 0.0


class MusicRound(IdMixin, TimestampMixin, Base):
    """生曲轮次（PRD V1.1 D2）。

    production：日常出片，可反复追加候选，显示模型名，**不计入模型战绩**；
    selection ：选型轮，参赛模型并行各出 N 版，强制盲听，选定后揭晓并计入战绩。
    """

    __tablename__ = "music_round"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("project.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20), default="production")
    round_no: Mapped[int] = mapped_column(Integer, default=1)        # 项目内序号
    selection_no: Mapped[int] = mapped_column(Integer, default=0)    # 全局选型轮序号（仅 selection）
    model_keys: Mapped[list] = mapped_column(JSON, default=list)
    variants: Mapped[int] = mapped_column(Integer, default=1)
    winner_candidate_id: Mapped[str | None] = mapped_column(String(36), nullable=True)


class MusicCandidate(IdMixin, TimestampMixin, Base):
    __tablename__ = "music_candidate"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("project.id"), index=True)
    lyric_version_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    round: Mapped[int] = mapped_column(Integer, default=1)
    model_key: Mapped[str] = mapped_column(String(40), index=True)  # 盲听已去除：直接记录模型
    variant: Mapped[int] = mapped_column(Integer, default=1)
    label: Mapped[str] = mapped_column(String(8), default="")  # A/B/C… 仅作候选指代
    audio_path: Mapped[str] = mapped_column(String(500), default="")
    bpm: Mapped[float] = mapped_column(Float, default=0.0)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    vocal: Mapped[str] = mapped_column(String(20), default="")
    fit_score: Mapped[int] = mapped_column(Integer, default=0)  # AI 简报匹配分 0-100
    fit_reason: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="queued")  # queued/running/done/failed
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    elapsed_s: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
    model_name: Mapped[str] = mapped_column(String(120), default="")
    round_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    # 占位结果（模型未配置时由 mock 适配器产出）：不打匹配分、不可选定交付、不进统计
    is_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    # 对外的音频地址只用随机 token，文件名里的模型 / mock 字样不外泄（盲听约束）
    audio_token: Mapped[str] = mapped_column(String(40), default="", index=True)


class Rating(IdMixin, TimestampMixin, Base):
    __tablename__ = "rating"
    __table_args__ = (UniqueConstraint("candidate_id", "rater", name="uq_rating_cand_rater"),)

    candidate_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("music_candidate.id"), index=True
    )
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    diction: Mapped[int] = mapped_column(Integer, default=0)  # 咬字 1-5
    melody: Mapped[int] = mapped_column(Integer, default=0)  # 旋律 1-5
    arrangement: Mapped[int] = mapped_column(Integer, default=0)  # 编曲 1-5
    rater: Mapped[str] = mapped_column(String(60), default="default")

    @property
    def total(self) -> int:
        return self.diction + self.melody + self.arrangement


# --------------------------------------------------------------------- 素材
class Asset(IdMixin, TimestampMixin, Base):
    __tablename__ = "asset"

    name: Mapped[str] = mapped_column(String(200))
    file_path: Mapped[str] = mapped_column(String(500))
    destination: Mapped[str] = mapped_column(String(100), default="")
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    fps: Mapped[float] = mapped_column(Float, default=0.0)
    thumb_path: Mapped[str] = mapped_column(String(500), default="")
    license_status: Mapped[str] = mapped_column(String(20), default="licensed")
    licensor: Mapped[str] = mapped_column(String(200), default="")
    license_no: Mapped[str] = mapped_column(String(100), default="")
    scope: Mapped[str] = mapped_column(String(200), default="")
    valid_until: Mapped[str] = mapped_column(String(20), default="")
    portrait: Mapped[str] = mapped_column(String(200), default="")
    has_portrait: Mapped[bool] = mapped_column(Boolean, default=False)
    usable: Mapped[bool] = mapped_column(Boolean, default=True)
    tag_confidence: Mapped[float] = mapped_column(Float, default=1.0)
    embedding: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # PRD V1.1 F4-10：来源 shot（实拍）/ ai（AI 生成）。AI 素材不计入「已授权」
    source: Mapped[str] = mapped_column(String(10), default="shot")
    # 种子演示数据（占位镜头与虚构授权），补登真实授权前不得进入样片
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False)


# ------------------------------------------------------- 逐句分镜（配图审核）
class StoryboardShot(IdMixin, TimestampMixin, Base):
    """一句歌词 = 一张景区图。

    为什么单独建表而不复用 asset：这一层要承载**审核中间态**
    （待生成 / 待审核 / 已通过 / 已驳回）和「这句话该配哪个景区」的原始意图，
    资产表是「已经定稿、可被引用」的结果。分开之后：
      · 驳回重生成只动分镜表，不会污染素材库；
      · 审核通过才把图片注册成 asset，再进入卡点合成。
    """

    __tablename__ = "storyboard_shot"

    project_id: Mapped[str] = mapped_column(String(36), index=True)
    order: Mapped[int] = mapped_column(Integer, default=0, index=True)
    section: Mapped[str] = mapped_column(String(40), default="")
    line_no: Mapped[int] = mapped_column(Integer, default=1)
    line_text: Mapped[str] = mapped_column(String(300), default="")

    # 「这句配哪个景区」——必须是真实可考的地点，note 里写依据
    place: Mapped[str] = mapped_column(String(120), default="")
    place_note: Mapped[str] = mapped_column(String(200), default="")
    keywords: Mapped[str] = mapped_column(String(300), default="")  # 逗号分隔的意象词
    prompt: Mapped[str] = mapped_column(Text, default="")

    provider: Mapped[str] = mapped_column(String(20), default="")   # wanx / seedream / upload
    model: Mapped[str] = mapped_column(String(80), default="")
    image_path: Mapped[str] = mapped_column(String(500), default="")
    clip_path: Mapped[str] = mapped_column(String(500), default="")
    asset_id: Mapped[str] = mapped_column(String(36), default="")

    # draft(待生成) → generating → pending(待审核) → approved / rejected
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    rev: Mapped[int] = mapped_column(Integer, default=0)            # 重生成次数，用于文件名去重
    # —— PRD V1.1 审核减负 ——
    # 与第几句歌词重复（副歌复唱）：0 = 本句是「母句」；>0 = 跟随该序号的母句，只审一次
    dup_of: Mapped[int] = mapped_column(Integer, default=0)
    # 视觉模型预审：图文一致度 0–100，-1 = 未预审（未配置 / 调用失败，不伪造分数）
    ai_score: Mapped[int] = mapped_column(Integer, default=-1)
    ai_reason: Mapped[str] = mapped_column(String(300), default="")
    # 素材库里可替换的实拍镜头（AI 先出分镜，有实拍再一键换）
    suggest_asset_id: Mapped[str] = mapped_column(String(36), default="")
    note: Mapped[str] = mapped_column(String(300), default="")      # 驳回理由 / 人工备注
    error: Mapped[str] = mapped_column(Text, default="")


class AssetTag(IdMixin, TimestampMixin, Base):
    __tablename__ = "asset_tag"

    asset_id: Mapped[str] = mapped_column(String(36), ForeignKey("asset.id"), index=True)
    dim: Mapped[str] = mapped_column(String(20))  # place/season/time/weather/mood/camera/subject
    value: Mapped[str] = mapped_column(String(120))
    source: Mapped[str] = mapped_column(String(20), default="ai")  # ai / manual


# --------------------------------------------------------------------- 剪辑
class Timeline(IdMixin, TimestampMixin, Base):
    __tablename__ = "timeline"

    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("project.id"), index=True, unique=True
    )
    audio_path: Mapped[str] = mapped_column(String(500), default="")
    bpm: Mapped[float] = mapped_column(Float, default=0.0)
    beats: Mapped[list] = mapped_column(JSON, default=list)  # 强拍时间点(秒)
    duration_s: Mapped[float] = mapped_column(Float, default=0.0)
    lyric_lines: Mapped[list] = mapped_column(JSON, default=list)  # [{text, start_s, end_s}]
    # 歌词时间轴的来源与质量：{engine, note, match_ratio, anchored, vocal_span}
    # engine='whisper' 表示时间来自歌声（强制对齐）；'analytic' 表示按字数估算。
    align_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    auto_version: Mapped[int] = mapped_column(Integer, default=1)


class TimelineClip(IdMixin, TimestampMixin, Base):
    __tablename__ = "timeline_clip"

    timeline_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("timeline.id"), index=True
    )
    project_id: Mapped[str] = mapped_column(String(36), index=True)
    order: Mapped[int] = mapped_column(Integer, default=0)
    asset_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    in_s: Mapped[float] = mapped_column(Float, default=0.0)
    out_s: Mapped[float] = mapped_column(Float, default=0.0)
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    lyric_line: Mapped[str] = mapped_column(Text, default="")
    scene_prompt: Mapped[str] = mapped_column(Text, default="")  # LLM 生成的画面描述
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)  # 是否被人工替换过


class RenderJob(IdMixin, TimestampMixin, Base):
    __tablename__ = "render_job"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("project.id"), index=True)
    variant: Mapped[str] = mapped_column(String(30))  # landscape/portrait/sub_zh_en/sub_zh/short
    status: Mapped[str] = mapped_column(String(20), default="queued")
    progress: Mapped[int] = mapped_column(Integer, default=0)
    output_path: Mapped[str] = mapped_column(String(500), default="")
    size_mb: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------- 任务队列
class Job(IdMixin, TimestampMixin, Base):
    __tablename__ = "job"

    queue: Mapped[str] = mapped_column(String(10), default="api", index=True)  # api / gpu
    type: Mapped[str] = mapped_column(String(40))  # music_gen/tagging/embedding/align/render/...
    ref_id: Mapped[str] = mapped_column(String(36), default="")
    project_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    vram_mb: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    retries: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------- 热歌监控
class TrendSnapshot(IdMixin, TimestampMixin, Base):
    """与 monitor.py 的 snapshot 表结构一致；由导入服务从 trends.db 同步。"""

    __tablename__ = "trend_snapshot"
    __table_args__ = (
        UniqueConstraint("snap_date", "source", "region", "video_id", name="uq_snapshot"),
    )

    snap_date: Mapped[str] = mapped_column(String(20), index=True)
    source: Mapped[str] = mapped_column(String(60))
    region: Mapped[str] = mapped_column(String(10))
    rank: Mapped[int] = mapped_column(Integer, default=0)
    video_id: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(400), default="")
    channel: Mapped[str] = mapped_column(String(300), default="")
    published_at: Mapped[str] = mapped_column(String(40), default="")
    views: Mapped[int] = mapped_column(Integer, default=0)
    likes: Mapped[int] = mapped_column(Integer, default=0)
    comments: Mapped[int] = mapped_column(Integer, default=0)
    duration_s: Mapped[int] = mapped_column(Integer, default=0)
    tags: Mapped[str] = mapped_column(Text, default="[]")
    description: Mapped[str] = mapped_column(Text, default="")


class TrendCard(IdMixin, TimestampMixin, Base):
    """二创素材卡。等价于 monitor.py analysis.result_json 的规范化表达。"""

    __tablename__ = "trend_card"

    video_id: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    week: Mapped[str] = mapped_column(String(12), index=True)  # ISO 年-周，如 2026-W39
    rank: Mapped[int] = mapped_column(Integer, default=0)
    song: Mapped[str] = mapped_column(String(400), default="")
    artist: Mapped[str] = mapped_column(String(300), default="")
    why_hot: Mapped[str] = mapped_column(Text, default="")
    elements: Mapped[dict] = mapped_column(JSON, default=dict)
    remix: Mapped[dict] = mapped_column(JSON, default=dict)  # borrowable/style_prompt/...
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    analyzed_at: Mapped[str] = mapped_column(String(40), default="")
    # 演示卡（monitor 未接入时的示例数据），不应出现在生产选项里
    is_seed: Mapped[bool] = mapped_column(Boolean, default=False)

    @property
    def style_prompt(self) -> str:
        return (self.remix or {}).get("style_prompt", "")


class PublishMetric(IdMixin, TimestampMixin, Base):
    __tablename__ = "publish_metric"

    project_id: Mapped[str] = mapped_column(String(36), ForeignKey("project.id"), index=True)
    platform: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(String(500), default="")
    published_at: Mapped[str] = mapped_column(String(20), default="")
    views_7d: Mapped[int] = mapped_column(Integer, default=0)
    completion: Mapped[float] = mapped_column(Float, default=0.0)
    engagement: Mapped[float] = mapped_column(Float, default=0.0)
    note: Mapped[str] = mapped_column(Text, default="")


# --------------------------------------------------------------------- 调用日志
class AiCall(IdMixin, TimestampMixin, Base):
    """PRD §11 可观测性：所有 AI 调用记录（模型/参数摘要/耗时/费用/状态）。"""

    __tablename__ = "ai_call"

    project_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    kind: Mapped[str] = mapped_column(String(30))  # llm/music/vlm/clip/align/render
    provider: Mapped[str] = mapped_column(String(60), default="")
    model: Mapped[str] = mapped_column(String(120), default="")
    purpose: Mapped[str] = mapped_column(String(60), default="")
    params_digest: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="ok")
    elapsed_s: Mapped[float] = mapped_column(Float, default=0.0)
    cost: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str] = mapped_column(Text, default="")
