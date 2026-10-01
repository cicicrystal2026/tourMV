"""热歌监控接入 —— 只读 monitor.py 的 trends.db，不修改原脚本一行。

两条路径：
  sync_from_monitor()  真实数据：读取 trends.db 的 snapshot / analysis 表，
                       直接复用 monitor.py 的 rank() 与要素拆解结果，零逻辑重复。
  seed_demo(db)        无 YouTube Key 时生成结构等价的示例数据，供界面联调。
"""
from __future__ import annotations

import datetime as dt
import importlib
import json
import logging
import sqlite3
import sys
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from ..config import settings
from ..models import TrendCard, TrendSnapshot

log = logging.getLogger("wenlv.trends")

ORIGINAL_MONITOR = Path(
    r"D:\WPS_Storage\WPS Cloud\WPSDrive\460887772\WPS云盘\AIGA-CODE\04-tourMV\monitor.py"
)


# ------------------------------------------------------------------ 工具
def iso_week(d: dt.date | None = None) -> str:
    d = d or dt.date.today()
    y, w, _ = d.isocalendar()
    return f"{y}-W{w:02d}"


def _load_monitor():
    """按需导入 monitor.py，复用它的 rank()/analyze_one()。"""
    for p in (settings().monitor_script, ORIGINAL_MONITOR):
        try:
            if p and Path(p).exists():
                parent = str(Path(p).parent)
                if parent not in sys.path:
                    sys.path.insert(0, parent)
                return importlib.import_module(Path(p).stem)
        except Exception as e:  # noqa: BLE001
            log.warning("导入 monitor.py 失败（%s）：%s", p, e)
    return None


def source_status() -> dict:
    mon = _load_monitor()
    db = settings().monitor_db
    return {
        "monitor_script": str(settings().monitor_script),
        "monitor_script_found": Path(settings().monitor_script).exists() or ORIGINAL_MONITOR.exists(),
        "monitor_module_importable": mon is not None,
        "monitor_db": str(db),
        "monitor_db_found": db.exists(),
        "report_dir": str(settings().monitor_reports),
    }


# ------------------------------------------------------------------ 真实同步
def sync_from_monitor(db: Session, days: int = 30, top: int = 10) -> dict:
    src = settings().monitor_db
    if not src.exists():
        raise FileNotFoundError(
            f"找不到 monitor.py 的数据库：{src}。先运行 `python monitor.py run --days 30`，"
            f"或在 .env 里设置 MONITOR_DB 指向已有的 trends.db。"
        )
    con = sqlite3.connect(str(src))
    con.row_factory = sqlite3.Row
    week = iso_week()
    n_snap = 0
    for r in con.execute("SELECT * FROM snapshot"):
        exists = db.execute(
            select(TrendSnapshot).where(
                TrendSnapshot.snap_date == r["snap_date"],
                TrendSnapshot.source == r["source"],
                TrendSnapshot.region == r["region"],
                TrendSnapshot.video_id == r["video_id"],
            )
        ).scalar_one_or_none()
        if exists:
            continue
        db.add(TrendSnapshot(
            snap_date=r["snap_date"], source=r["source"], region=r["region"], rank=r["rank"],
            video_id=r["video_id"], title=r["title"], channel=r["channel"],
            published_at=r["published_at"], views=r["views"], likes=r["likes"],
            comments=r["comments"], duration_s=r["duration_s"],
            tags=r["tags"] or "[]", description=r["description"] or "",
        ))
        n_snap += 1
    db.commit()

    mon = _load_monitor()
    if mon is None:
        con.close()
        return {"snapshots": n_snap, "cards": 0, "note": "monitor.py 不可导入，仅同步了快照"}

    items = mon.rank(con, days, top)
    analyses = {}
    for r in con.execute("SELECT video_id, analyzed_at, result_json FROM analysis"):
        analyses[r["video_id"]] = (r["analyzed_at"], json.loads(r["result_json"]))
    con.close()

    n_cards = 0
    for i, it in enumerate(items, 1):
        vid = it["video_id"]
        analyzed_at, a = analyses.get(vid, ("", {}))
        card = db.execute(select(TrendCard).where(TrendCard.video_id == vid)).scalar_one_or_none()
        fields = dict(
            week=iso_week(dt.date.fromisoformat(analyzed_at[:10])) if analyzed_at else week,
            rank=i, song=a.get("song") or it["title"], artist=a.get("artist") or it["channel"],
            why_hot=a.get("why_hot", ""),
            elements=a.get("elements", {}) or {},
            remix=a.get("remix_card", {}) or {},
            metrics={k: it[k] for k in ("views_total", "views_gained", "views_per_day",
                                        "engagement", "trending_regions", "sources",
                                        "published_at", "age_days", "duration_s")},
            analyzed_at=analyzed_at,
        )
        if card:
            for k, v in fields.items():
                setattr(card, k, v)
        else:
            db.add(TrendCard(video_id=vid, **fields))
            n_cards += 1
    db.commit()
    return {"snapshots": n_snap, "cards": n_cards, "items": len(items)}


# ------------------------------------------------------------------ 示例数据
_DEMO_CARDS = [
    {
        "video_id": "demo0001", "song": "Healing River", "artist": "Ambient Lab",
        "why_hot": "近 30 天在美/英/德三地音乐榜同时上榜，日均播放 42 万，属治愈系长时背景音乐",
        "elements": {
            "genre": "ambient pop", "subgenre": "healing", "bpm": "72-78", "key_mode": "D major",
            "time_feel": "half-time", "structure": "intro-verse-prechorus-chorus-bridge-chorus",
            "length": "3:20", "instrumentation": ["钢琴", "弦乐", "轻打击", "原声吉他"],
            "vocal_style": "气声女声", "production_traits": ["宽混响", "低频收敛", "留白多"],
            "mood": ["治愈", "宁静"], "energy": "low", "lyric_theme": "归途与自然",
            "language": "en", "mv_visual_style": "自然光空镜 + 慢推",
            "place_or_culture_elements": "河谷、晨雾",
            "confidence": {"bpm": "inferred", "key_mode": "inferred", "instrumentation": "inferred"},
        },
        "remix": {
            "borrowable": ["half-time 鼓组", "钢琴与弦乐的对答", "副歌前留一拍空白"],
            "style_prompt": "warm ambient pop, half-time drums, piano and strings dialogue, "
                            "breathy female vocal, wide reverb, 74 bpm, D major",
            "lyric_angles_for_wenlv": ["把『归途』落到具体目的地的一条路", "用晨雾与河谷做副歌意象"],
            "visual_ideas": ["清晨河谷航拍开场", "石桥与晨雾慢推", "人物背影走向水边"],
            "avoid": ["原曲旋律走向", "原歌词句式", "标志性弦乐动机"],
        },
    },
    {
        "video_id": "demo0002", "song": "Old Town Lights", "artist": "Night Walkers",
        "why_hot": "日/韩两地榜单上升最快，互动率 6.8%，评论区集中在『想起老家』",
        "elements": {
            "genre": "city pop revival", "subgenre": "retro", "bpm": "96-102", "key_mode": "A minor",
            "time_feel": "four-on-the-floor", "structure": "intro-verse-chorus-verse-chorus-outro",
            "length": "3:05", "instrumentation": ["电钢琴", "slap bass", "合成弦乐", "电子鼓"],
            "vocal_style": "中性中音", "production_traits": ["80s 复古混音", "宽立体声"],
            "mood": ["热闹", "怀旧"], "energy": "mid", "lyric_theme": "夜晚街市与旧友",
            "language": "en", "mv_visual_style": "夜景霓虹 + 手持跟拍",
            "place_or_culture_elements": "夜市、灯牌、老街区",
            "confidence": {"bpm": "inferred", "genre": "inferred"},
        },
        "remix": {
            "borrowable": ["four-on-the-floor 底鼓", "电钢琴切分和弦", "预副歌的半音上行"],
            "style_prompt": "retro city pop revival, four-on-the-floor kick, electric piano stabs, "
                            "slap bass, warm analog synth strings, 99 bpm, A minor",
            "lyric_angles_for_wenlv": ["把夜市写成可以走进去的场景", "用灯牌与摊位做副歌记忆点"],
            "visual_ideas": ["夜市灯牌延时", "手持跟拍走过小吃街", "雨后地面反光特写"],
            "avoid": ["原曲 bass line", "原曲和弦进行", "原歌词"],
        },
    },
]


def seed_demo(db: Session, weeks: int = 3) -> dict:
    """生成示例快照与素材卡。真实数据同步后会自动覆盖同 video_id 的卡片。"""
    today = dt.date.today()
    made = 0
    if db.execute(select(TrendSnapshot).limit(1)).scalar_one_or_none() is None:
        for w in range(weeks):
            snap_date = (today - dt.timedelta(days=7 * w)).isoformat()
            for region in ("US", "GB", "DE", "JP", "KR"):
                for rank, c in enumerate(_DEMO_CARDS, 1):
                    base = 3_000_000 - rank * 400_000 - w * 250_000
                    db.add(TrendSnapshot(
                        snap_date=snap_date, source="trending", region=region, rank=rank,
                        video_id=c["video_id"], title=c["song"], channel=c["artist"],
                        published_at=(today - dt.timedelta(days=40 + rank * 5)).isoformat(),
                        views=max(base, 1000), likes=int(base * 0.05), comments=int(base * 0.004),
                        duration_s=200, tags="[]", description="示例数据（未接入 YouTube API）",
                    ))
                    made += 1
        db.commit()

    for i, c in enumerate(_DEMO_CARDS, 1):
        if db.execute(select(TrendCard).where(TrendCard.video_id == c["video_id"])).scalar_one_or_none():
            continue
        db.add(TrendCard(
            video_id=c["video_id"], week=iso_week(today - dt.timedelta(days=7 * (i - 1))),
            rank=i, song=c["song"], artist=c["artist"], why_hot=c["why_hot"],
            elements=c["elements"], remix=c["remix"],
            metrics={"views_total": 3_000_000 - i * 400_000, "views_gained": 2_100_000 - i * 300_000,
                     "views_per_day": 420_000 - i * 60_000, "engagement": 0.068 - i * 0.012,
                     "trending_regions": 3 - i + 1, "sources": "trending,search:healing"},
            analyzed_at=dt.datetime.now().isoformat(),
        ))
    db.commit()
    return {"snapshots": made, "cards": len(_DEMO_CARDS)}


def clear_demo(db: Session) -> None:
    db.execute(delete(TrendSnapshot).where(TrendSnapshot.description.like("示例数据%")))
    db.execute(delete(TrendCard).where(TrendCard.video_id.like("demo%")))
    db.commit()


def list_cards(db: Session, week: str | None = None) -> list[TrendCard]:
    stmt = select(TrendCard).order_by(TrendCard.week.desc(), TrendCard.rank)
    if week:
        stmt = stmt.where(TrendCard.week == week)
    return list(db.execute(stmt).scalars())


def weeks(db: Session) -> list[str]:
    rows = db.execute(select(TrendCard.week).distinct().order_by(TrendCard.week.desc())).scalars()
    return [r for r in rows if r]
