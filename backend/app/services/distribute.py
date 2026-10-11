"""一键分发：把已交付项目的成片按渠道规则复制到本地数字资产目录，并按规范命名。

规则可配置（存 data/distribute.json）：每个渠道要哪些成片版本、放到哪个文件夹。
默认根目录 D:\\09_OPC\\文旅MV数字资产，渠道文件夹沿用本机已有的 youtube / 小红书 / 抖音 / 视频号，另加 tiktok。

目录与命名：
    <根目录>/<渠道文件夹>/<目的地>_<日期>/<目的地>_<渠道>_<版本>_<时长>s_<日期>.mp4
每个子文件夹附一份「说明.md」（歌词、所用模型、费用、是否含 AI 画面）与「分发清单.json」。

只复制、不移动、不改动项目本身；同名文件覆盖（重新分发即更新）。出片自检没通过的版本不分发。
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import media
from ..config import settings
from ..models import (AiCall, Brief, DistributeLog, LyricVersion, MusicCandidate, Project, RenderJob,
                      StoryboardShot)
from .projects import ProjectError

DEFAULT_ROOT = r"D:\09_OPC\文旅MV数字资产"

# 版本的短名（用在文件名里）
VARIANT_NAME = {
    "landscape": "横版完整版", "portrait": "竖版完整版", "sub_zh_en": "横版中英字幕", "short": "竖版短版",
    "clean": "横版无字幕", "highlight30": "高光版", "essence60": "精华版",
    "cover": "横版封面", "cover_portrait": "竖版封面",
}

DEFAULT_CHANNELS = [
    {"key": "youtube", "label": "YouTube", "folder": "youtube",
     "variants": ["sub_zh_en", "essence60", "highlight30", "cover"],
     "note": "横版中英字幕完整版；竖版 60 / 30 秒用于 Shorts"},
    {"key": "tiktok", "label": "TikTok", "folder": "tiktok",
     "variants": ["highlight30", "essence60", "cover_portrait"],
     "note": "竖版 30 / 60 秒。目前切片只有中文字幕，海外投放建议补英文字幕"},
    {"key": "douyin", "label": "抖音", "folder": "抖音",
     "variants": ["highlight30", "essence60", "cover_portrait"], "note": "竖版 30 / 60 秒，中文字幕"},
    {"key": "xiaohongshu", "label": "小红书", "folder": "小红书",
     "variants": ["essence60", "cover_portrait"], "note": "竖版 60 秒，中文字幕"},
    {"key": "shipinhao", "label": "视频号", "folder": "视频号",
     "variants": ["essence60", "portrait", "cover_portrait"], "note": "竖版 60 秒与竖版完整版，中文字幕"},
]
# 封面是可选物料：项目里没有封面不算「缺版本」
OPTIONAL = {"cover", "cover_portrait"}


# ------------------------------------------------------------------ 配置
def _cfg_path() -> Path:
    return settings().data_dir / "distribute.json"


def get_config() -> dict:
    cfg = {"root": os.environ.get("DISTRIBUTE_ROOT", DEFAULT_ROOT), "channels": DEFAULT_CHANNELS}
    try:
        saved = json.loads(_cfg_path().read_text(encoding="utf-8"))
        if isinstance(saved.get("root"), str) and saved["root"].strip():
            cfg["root"] = saved["root"].strip()
        if isinstance(saved.get("channels"), list) and saved["channels"]:
            cfg["channels"] = saved["channels"]
    except (OSError, ValueError):
        pass
    cfg["root_exists"] = Path(cfg["root"]).is_dir()
    cfg["variant_names"] = VARIANT_NAME
    return cfg


def save_config(root: str | None, channels: list | None) -> dict:
    cur = get_config()
    out = {"root": (root or cur["root"]).strip(), "channels": cur["channels"]}
    if not Path(out["root"]).is_absolute():
        raise ProjectError("分发根目录必须是完整路径，例如 D:\\09_OPC\\文旅MV数字资产")
    if channels is not None:
        clean = []
        for c in channels:
            key, folder = str(c.get("key", "")).strip(), _safe(str(c.get("folder", "")).strip())
            vs = [v for v in (c.get("variants") or []) if v in VARIANT_NAME]
            if not key or not folder or not vs:
                raise ProjectError("每个渠道都要有标识、文件夹名和至少一个成片版本")
            clean.append({"key": key, "label": str(c.get("label") or key), "folder": folder,
                          "variants": vs, "note": str(c.get("note") or "")})
        out["channels"] = clean
    _cfg_path().write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    return get_config()


_BAD = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _safe(name: str) -> str:
    """去掉 Windows 文件名不允许的字符；「吉林·长白山」→「吉林长白山」。"""
    return _BAD.sub("", (name or "").replace("·", "").replace("•", "")).strip(" .")[:60]


# ------------------------------------------------------------------ 计划
def _renders(db: Session, project_id: str) -> dict[str, RenderJob]:
    return {r.variant: r for r in db.execute(select(RenderJob).where(RenderJob.project_id == project_id)).scalars()}


def plan(db: Session, project_id: str) -> dict:
    """这支片分发到各渠道会放哪些文件、缺什么。不写任何文件。"""
    p = db.get(Project, project_id)
    if p is None:
        raise ProjectError("项目不存在")
    cfg = get_config()
    renders = _renders(db, project_id)
    day = datetime.now().strftime("%Y%m%d")
    dest = _safe(p.destination or p.name) or "未命名"
    logs = {}
    for g in db.execute(select(DistributeLog).where(DistributeLog.project_id == project_id)
                        .order_by(DistributeLog.created_at)).scalars():
        logs[g.channel] = g
    channels = []
    missing_all: set[str] = set()
    for ch in cfg["channels"]:
        folder = Path(cfg["root"]) / ch["folder"] / f"{dest}_{day}"
        files, missing = [], []
        for v in ch["variants"]:
            r = renders.get(v)
            src = Path(r.output_path) if r and r.output_path else None
            ok = bool(r and r.status == "done" and src and src.exists())
            qc_ok = (r.qc or {}).get("ok", True) if r else True
            if not ok:
                if v not in OPTIONAL:
                    missing.append(v)
                    missing_all.add(v)
                continue
            dur = int(round(float(media.probe(src).get("duration_s") or 0))) if src.suffix.lower() == ".mp4" else 0
            name = "_".join(x for x in (dest, ch["label"], VARIANT_NAME.get(v, v),
                                        f"{dur}s" if dur else "", day) if x) + src.suffix.lower()
            files.append({"variant": v, "variant_name": VARIANT_NAME.get(v, v), "name": name,
                          "size_mb": round(src.stat().st_size / 1048576, 2), "duration_s": dur,
                          "qc_ok": bool(qc_ok), "source": str(src)})
        last = logs.get(ch["key"])
        channels.append({
            "key": ch["key"], "label": ch["label"], "note": ch.get("note", ""), "folder": str(folder),
            "files": [{k: v for k, v in f.items() if k != "source"} for f in files],
            "_files": files,
            "missing": [{"variant": v, "variant_name": VARIANT_NAME.get(v, v)} for v in missing],
            "blocked": [f["variant_name"] for f in files if not f["qc_ok"]],
            "last": {"folder": last.folder, "count": len(last.files or []),
                     "at": last.created_at.isoformat() + "Z"} if last else None,
        })
    return {"project": {"id": p.id, "name": p.name, "destination": p.destination, "status": p.status,
                        "delivered": p.locked_at is not None},
            "root": cfg["root"], "root_exists": cfg["root_exists"], "channels": channels,
            "missing_variants": sorted(missing_all)}


def public_plan(db: Session, project_id: str) -> dict:
    d = plan(db, project_id)
    for c in d["channels"]:
        c.pop("_files", None)
    return d


# ------------------------------------------------------------------ 执行
def run(db: Session, project_id: str, channel_keys: list[str] | None = None) -> dict:
    d = plan(db, project_id)
    if not d["project"]["delivered"]:
        raise ProjectError("只有已交付的项目可以分发（成片要先通过出片自检）")
    root = Path(d["root"])
    if not root.is_dir():
        raise ProjectError(f"分发根目录不存在：{root}。请先创建该文件夹，或在分发设置里改成正确的路径")
    want = set(channel_keys or [c["key"] for c in d["channels"]])
    readme = _readme(db, project_id)
    done = []
    for ch in d["channels"]:
        if ch["key"] not in want:
            continue
        usable = [f for f in ch["_files"] if f["qc_ok"]]
        if not usable:
            done.append({"key": ch["key"], "label": ch["label"], "copied": 0, "folder": ch["folder"],
                         "skipped": "没有可分发的成片"})
            continue
        folder = Path(ch["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        names = []
        for f in usable:
            shutil.copy2(f["source"], folder / f["name"])
            names.append(f["name"])
        (folder / "说明.md").write_text(readme.replace("{{渠道}}", ch["label"]), encoding="utf-8")
        (folder / "分发清单.json").write_text(json.dumps({
            "project_id": project_id, "project": d["project"]["name"], "channel": ch["label"],
            "distributed_at": datetime.now().isoformat(timespec="seconds"),
            "files": [{k: f[k] for k in ("name", "variant", "variant_name", "duration_s", "size_mb")} for f in usable],
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        db.add(DistributeLog(project_id=project_id, channel=ch["key"], folder=str(folder), files=names))
        done.append({"key": ch["key"], "label": ch["label"], "copied": len(names), "folder": str(folder),
                     "missing": [m["variant_name"] for m in ch["missing"]]})
    db.commit()
    return {"channels": done, "total_files": sum(c["copied"] for c in done)}


def _readme(db: Session, project_id: str) -> str:
    """随片说明：投放和事后复盘都用得上的信息。"""
    p = db.get(Project, project_id)
    b = db.execute(select(Brief).where(Brief.project_id == project_id)).scalar_one_or_none()
    lv = db.execute(select(LyricVersion).where(LyricVersion.project_id == project_id)
                    .order_by(LyricVersion.version.desc()).limit(1)).scalar_one_or_none()
    song = db.get(MusicCandidate, p.selected_candidate_id) if p.selected_candidate_id else None
    shots = list(db.execute(select(StoryboardShot).where(StoryboardShot.project_id == project_id)).scalars())
    # 修订版沿用了原项目的曲和画面，钱记在原项目上：费用按整条修订链合计，
    # 否则修订版会显示「本片费用 ¥0.00」
    chain, cur = [], p
    while cur is not None and cur.id not in chain:
        chain.append(cur.id)
        cur = db.get(Project, cur.parent_project_id) if cur.parent_project_id else None
    calls = list(db.execute(select(AiCall).where(AiCall.project_id.in_(chain), AiCall.status == "ok")).scalars())
    songs = [x for x in db.execute(select(MusicCandidate).where(MusicCandidate.project_id.in_(chain))).scalars()
             if x.status == "done" and not x.is_mock and (x.cost or 0) > 0]
    cost = sum(c.cost or 0 for c in calls) + sum(x.cost or 0 for x in songs)
    img_models = sorted({s.model for s in shots if s.model})
    llm_models = sorted({c.model for c in calls if c.kind == "llm" and c.model})
    ai_shots = [s for s in shots if s.provider in ("wanx", "seedream", "jimeng", "library_ai", "borrowed")]
    lines = [f"# {p.name}", "",
             f"- 渠道：{{{{渠道}}}}",
             f"- 目的地：{p.destination}",
             f"- 分发时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}",
             f"- 曲风 / 情绪：{(b.genre if b else '') or '—'} / {'、'.join((b.mood if b else []) or []) or '—'}",
             f"- 写词模型：{'、'.join(llm_models) or '—'}",
             f"- 生曲模型：{(song.model_name or song.model_key) if song else '—'}",
             f"- 生图模型：{'、'.join(img_models) or '—'}",
             f"- 本片模型调用费用：约 ¥{cost:.2f}（含修订前各版；不含人工与本地处理）" if len(chain) > 1
             else f"- 本片模型调用费用：约 ¥{cost:.2f}（不含人工与本地处理）",
             f"- 含 AI 生成画面：{'是（片尾已加字卡；发布时请按平台规则勾选 AI 内容标注）' if ai_shots else '否'}",
             "", "## 歌词", ""]
    for s in (lv.sections if lv else []):
        lines.append(f"**{s.get('name', '')}**")
        lines += [str(x) for x in (s.get("lines") or [])]
        lines.append("")
    return "\n".join(lines)
