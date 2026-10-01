#!/usr/bin/env python3
"""
YouTube 热门音乐监控 + 音乐要素拆解（文旅MV 实验室用）

功能
  collect   抓取各地区 YouTube 音乐热门榜 + 近 N 天细分关键词热门视频，按天存快照
  report    基于快照计算近 N 天热度排行（窗口内播放增量 / 日均播放）
  analyze   对排行前 K 首做音乐要素拆解，输出「二创素材卡」（可接任意 OpenAI 兼容大模型）
  run       collect + report + analyze

合规约定（写在代码里，不靠自觉）
  - 只调用 YouTube Data API v3 读取公开元数据，不下载 YouTube 音视频（违反平台条款）
  - 音频特征只分析你本地合法持有的音频文件（--audio-dir，文件名 = video_id）
  - 二创素材卡只输出「风格要素」，风格提示词里自动剔除原曲名 / 艺人名，不输出旋律、不引用歌词

用法
  pip install -r requirements.txt
  cp .env.example .env   # 填 YT_API_KEY、LLM_*
  python monitor.py run --days 30 --top 10
  # 建议用系统定时任务每天跑一次 collect，每周跑一次 report + analyze
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
YT = "https://www.googleapis.com/youtube/v3"


# ---------------------------------------------------------------- config
def load_env(path=HERE / ".env"):
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def cfg(key, default=None):
    return os.environ.get(key, default)


def csv_list(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


# ---------------------------------------------------------------- storage
SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshot (
  snap_date TEXT, source TEXT, region TEXT, rank INTEGER,
  video_id TEXT, title TEXT, channel TEXT, published_at TEXT,
  views INTEGER, likes INTEGER, comments INTEGER, duration_s INTEGER,
  tags TEXT, description TEXT,
  PRIMARY KEY (snap_date, source, region, video_id)
);
CREATE TABLE IF NOT EXISTS analysis (
  video_id TEXT PRIMARY KEY, analyzed_at TEXT, result_json TEXT
);
"""


def db_connect(path):
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- YouTube API
def yt_get(endpoint, params):
    params = dict(params, key=cfg("YT_API_KEY"))
    r = requests.get(f"{YT}/{endpoint}", params=params, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"{endpoint} {r.status_code}: {r.text[:300]}")
    return r.json()


def iso_dur(s):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


def video_rows(items):
    rows = []
    for it in items:
        sn, stt, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
        rows.append({
            "video_id": it["id"] if isinstance(it["id"], str) else it["id"].get("videoId"),
            "title": sn.get("title", ""), "channel": sn.get("channelTitle", ""),
            "published_at": sn.get("publishedAt", ""),
            "views": int(stt.get("viewCount", 0)), "likes": int(stt.get("likeCount", 0)),
            "comments": int(stt.get("commentCount", 0)),
            "duration_s": iso_dur(cd.get("duration")),
            "tags": json.dumps(sn.get("tags", [])[:30], ensure_ascii=False),
            "description": (sn.get("description") or "")[:1500],
        })
    return rows


def fetch_trending(region, n=50):
    data = yt_get("videos", {"part": "snippet,statistics,contentDetails", "chart": "mostPopular",
                             "videoCategoryId": "10", "regionCode": region, "maxResults": n})
    return video_rows(data.get("items", []))


def fetch_niche(keyword, days, n=25):
    after = (dt.datetime.utcnow() - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    s = yt_get("search", {"part": "id", "q": keyword, "type": "video", "videoCategoryId": "10",
                          "publishedAfter": after, "order": "viewCount", "maxResults": n})
    ids = [it["id"]["videoId"] for it in s.get("items", [])]
    if not ids:
        return []
    v = yt_get("videos", {"part": "snippet,statistics,contentDetails", "id": ",".join(ids)})
    return video_rows(v.get("items", []))


def collect(con, days):
    today = dt.date.today().isoformat()
    jobs = [("trending", r, lambda r=r: fetch_trending(r)) for r in csv_list(cfg("REGIONS", "US,GB,DE,JP,KR"))]
    jobs += [(f"search:{k}", "ALL", lambda k=k: fetch_niche(k, days)) for k in csv_list(cfg("NICHE_KEYWORDS"))]
    total = 0
    for source, region, fn in jobs:
        try:
            rows = fn()
        except Exception as e:  # 单个地区/关键词失败不影响其他
            print(f"[skip] {source} {region}: {e}", file=sys.stderr)
            continue
        for rank, row in enumerate(rows, 1):
            con.execute("INSERT OR REPLACE INTO snapshot VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (today, source, region, rank, row["video_id"], row["title"], row["channel"],
                         row["published_at"], row["views"], row["likes"], row["comments"],
                         row["duration_s"], row["tags"], row["description"]))
        total += len(rows)
        print(f"[ok] {source:<28} {region:<4} {len(rows)} 条")
    con.commit()
    print(f"collect 完成：{today} 共 {total} 条快照")


# ---------------------------------------------------------------- ranking
def rank(con, days, top):
    since = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    rows = con.execute("""
      SELECT video_id, MIN(snap_date), MAX(snap_date), MIN(views), MAX(views),
             MAX(title), MAX(channel), MAX(published_at), MAX(duration_s), MAX(likes), MAX(comments),
             MIN(CASE WHEN source='trending' THEN rank END),
             COUNT(DISTINCT CASE WHEN source='trending' THEN region END),
             GROUP_CONCAT(DISTINCT source), MAX(tags), MAX(description)
      FROM snapshot WHERE snap_date >= ? GROUP BY video_id""", (since,)).fetchall()
    out, now = [], dt.datetime.utcnow()
    for (vid, d0, d1, v0, v1, title, ch, pub, dur, likes, cmts, best, nreg, srcs, tags, desc) in rows:
        try:
            pub_dt = dt.datetime.strptime(pub[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            pub_dt = now - dt.timedelta(days=365)
        age = max((now - pub_dt).days, 1)
        new_in_window = age <= days
        gained = v1 if new_in_window else max(v1 - v0, 0)
        span = age if new_in_window else max((dt.date.fromisoformat(d1) - dt.date.fromisoformat(d0)).days, 1)
        out.append({
            "video_id": vid, "title": title, "channel": ch, "published_at": pub[:10],
            "age_days": age, "new_in_window": new_in_window, "views_total": v1,
            "views_gained": gained, "views_per_day": round(gained / span),
            "engagement": round((likes + cmts) / v1, 4) if v1 else 0,
            "best_trending_rank": best, "trending_regions": nreg, "sources": srcs,
            "duration_s": dur, "tags": json.loads(tags or "[]"), "description": desc or "",
        })
    # 排序：窗口内播放增量为主；只有一天快照时退化为日均播放
    out.sort(key=lambda x: (x["views_gained"], x["views_per_day"]), reverse=True)
    return out[:top]


# ---------------------------------------------------------------- local audio features (optional)
MAJOR = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88]
MINOR = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17]
NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def audio_features(path):
    """只用于你合法持有的本地音频：速度、调性（Krumhansl 估计）、时长、能量。"""
    import numpy as np
    import librosa
    y, sr = librosa.load(path, sr=22050, mono=True)
    tempo, _ = librosa.beat.beat_track(y=y, sr=sr)
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr).mean(axis=1)
    best = max(((np.corrcoef(np.roll(prof, i), chroma)[0, 1], NOTES[i] + mode)
                for prof, mode in ((MAJOR, " major"), (MINOR, " minor")) for i in range(12)))
    rms = librosa.feature.rms(y=y)[0]
    return {"bpm": round(float(np.atleast_1d(tempo)[0])), "key_estimate": best[1],
            "duration_s": round(len(y) / sr), "energy_rms": round(float(rms.mean()), 4),
            "energy_curve_8": [round(float(seg.mean()), 4) for seg in np.array_split(rms, 8)]}


def find_audio(audio_dir, vid):
    if not audio_dir:
        return None
    for ext in (".mp3", ".wav", ".flac", ".m4a", ".ogg"):
        p = Path(audio_dir) / f"{vid}{ext}"
        if p.exists():
            return p
    return None


# ---------------------------------------------------------------- LLM analysis
SYSTEM = """你是流行音乐制作人兼文旅内容策划。根据给定的 YouTube 视频公开元数据（以及可能附带的本地音频测量值），
拆解这首歌的音乐要素，并为「中国文旅MV原创歌曲」提供二创素材。
硬性规则：
1. 只输出风格层面的要素（曲风、速度区间、调式、配器、结构、情绪、歌词主题方向、画面风格），不复述或改写原歌词，不描述具体旋律音高走向。
2. style_prompt 必须是通用风格描述（英文，给 AI 音乐模型用），不得包含原曲名、艺人名、厂牌名。
3. 元数据无法支持的字段写 "unknown"；凡是推断得出的值，在 confidence 里标 "inferred"；只有本地音频测量值标 "measured"。
4. 只输出 JSON，不要任何解释文字。"""

SCHEMA_HINT = {
    "song": "原曲名", "artist": "艺人",
    "why_hot": "走红原因（从播放、地区、上传时间、描述中推断）",
    "elements": {
        "genre": "", "subgenre": "", "bpm": "数字或区间", "key_mode": "", "time_feel": "如 half-time / four-on-the-floor",
        "structure": "如 intro-verse-prechorus-chorus…；hook 出现时间", "length": "",
        "instrumentation": [], "vocal_style": "", "production_traits": [],
        "mood": [], "energy": "low/mid/high", "lyric_theme": "主题方向，不引用原词",
        "language": "", "mv_visual_style": "", "place_or_culture_elements": "",
        "confidence": {"字段名": "measured/inferred/unknown"}
    },
    "remix_card": {
        "borrowable": ["可借鉴的风格要素"],
        "style_prompt": "英文风格提示词，不含原曲/艺人名",
        "lyric_angles_for_wenlv": ["适配中国文旅目的地的歌词方向 2-3 条"],
        "visual_ideas": ["MV 画面方向 2-3 条"],
        "avoid": ["不能碰的：原旋律、原歌词、标志性 hook 句式等"]
    }
}


def llm_chat(messages):
    base = cfg("LLM_BASE_URL", "").rstrip("/")
    r = requests.post(f"{base}/chat/completions", timeout=120,
                      headers={"Authorization": f"Bearer {cfg('LLM_API_KEY')}"},
                      json={"model": cfg("LLM_MODEL"), "messages": messages, "temperature": 0.3,
                            "response_format": {"type": "json_object"}})
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def scrub_names(text, item):
    """风格提示词里剔除原曲名和艺人名（大小写不敏感）。"""
    names = {item["title"], item["channel"]}
    for sep in (" - ", " – ", " | ", "(", "["):
        names |= {p.strip() for n in list(names) for p in n.split(sep)}
    for n in sorted((n for n in names if len(n) >= 3), key=len, reverse=True):
        text = re.sub(re.escape(n), "", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", text).strip(" ,;")


def analyze_one(item, audio_dir, use_llm):
    meta = {k: item[k] for k in ("title", "channel", "published_at", "views_total", "views_gained",
                                 "views_per_day", "engagement", "trending_regions", "sources", "duration_s", "tags")}
    meta["description"] = item["description"][:800]
    ap = find_audio(audio_dir, item["video_id"])
    if ap:
        try:
            meta["local_audio_measured"] = audio_features(ap)
        except Exception as e:
            print(f"[audio] {ap.name}: {e}", file=sys.stderr)
    if not use_llm:
        return {"song": item["title"], "artist": item["channel"], "metadata": meta,
                "note": "未调用大模型，仅元数据"}
    user = ("按下列 JSON 结构输出：\n" + json.dumps(SCHEMA_HINT, ensure_ascii=False) +
            "\n\n视频元数据：\n" + json.dumps(meta, ensure_ascii=False))
    res = json.loads(llm_chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]))
    card = res.get("remix_card", {})
    if isinstance(card.get("style_prompt"), str):
        card["style_prompt"] = scrub_names(card["style_prompt"], item)
    res["metadata"] = meta
    return res


# ---------------------------------------------------------------- output
def fmt_n(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}K" if n >= 1e3 else str(n)


def write_report(items, analyses, days, outdir):
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = dt.date.today().isoformat()
    md = [f"# YouTube 热门音乐拆解 · 近 {days} 天 · {stamp}", "",
          "数据：YouTube Data API v3 公开数据（音乐类热门榜 + 细分关键词近期高播放视频）。"
          "要素中标 inferred 的为模型推断，需人工试听确认。", "",
          "## 热度排行", "", "| # | 歌曲 | 频道 | 发布 | 窗口内播放 | 日均 | 上榜地区数 | 互动率 |",
          "|---|---|---|---|---|---|---|---|"]
    for i, it in enumerate(items, 1):
        md.append(f"| {i} | {it['title']} | {it['channel']} | {it['published_at']} | {fmt_n(it['views_gained'])} | "
                  f"{fmt_n(it['views_per_day'])} | {it['trending_regions']} | {it['engagement']:.2%} |")
    md += ["", "## 要素拆解与二创素材卡", ""]
    for it, a in zip(items, analyses):
        md.append(f"### {it['title']}")
        el, card = a.get("elements"), a.get("remix_card")
        if a.get("why_hot"):
            md.append(f"- 走红原因：{a['why_hot']}")
        if el:
            for k in ("genre", "subgenre", "bpm", "key_mode", "time_feel", "structure", "length", "instrumentation",
                      "vocal_style", "production_traits", "mood", "energy", "lyric_theme", "mv_visual_style",
                      "place_or_culture_elements"):
                v = el.get(k)
                if v not in (None, "", [], "unknown"):
                    md.append(f"- {k}：{', '.join(v) if isinstance(v, list) else v}")
        if card:
            md.append(f"- **可借鉴**：{'；'.join(card.get('borrowable', []))}")
            md.append(f"- **风格提示词**：`{card.get('style_prompt', '')}`")
            md.append(f"- **文旅歌词方向**：{'；'.join(card.get('lyric_angles_for_wenlv', []))}")
            md.append(f"- **画面方向**：{'；'.join(card.get('visual_ideas', []))}")
            md.append(f"- **不能碰**：{'；'.join(card.get('avoid', []))}")
        md.append("")
    (outdir / f"report_{stamp}.md").write_text("\n".join(md), encoding="utf-8")
    (outdir / f"analysis_{stamp}.json").write_text(
        json.dumps([{"rank": i + 1, "item": {k: v for k, v in it.items() if k != "description"}, "analysis": a}
                    for i, (it, a) in enumerate(zip(items, analyses))], ensure_ascii=False, indent=2),
        encoding="utf-8")
    print(f"报告已输出：{outdir / f'report_{stamp}.md'}")


# ---------------------------------------------------------------- main
def main():
    load_env()
    ap = argparse.ArgumentParser(description="YouTube 热门音乐监控与要素拆解")
    ap.add_argument("cmd", choices=["collect", "report", "analyze", "run"])
    ap.add_argument("--days", type=int, default=30, help="统计窗口天数")
    ap.add_argument("--top", type=int, default=10, help="拆解前 K 首")
    ap.add_argument("--db", default=str(HERE / "trends.db"))
    ap.add_argument("--out", default=str(HERE / "reports"))
    ap.add_argument("--audio-dir", default=cfg("AUDIO_DIR"), help="本地合法音频目录，文件名=video_id")
    ap.add_argument("--no-llm", action="store_true", help="不调用大模型，只出排行和元数据")
    a = ap.parse_args()

    con = db_connect(a.db)
    if a.cmd in ("collect", "run"):
        if not cfg("YT_API_KEY"):
            sys.exit("缺少 YT_API_KEY，见 .env.example")
        collect(con, a.days)
    if a.cmd in ("report", "analyze", "run"):
        items = rank(con, a.days, a.top)
        if not items:
            sys.exit("数据库里没有窗口期内的快照，先运行 collect")
        use_llm = a.cmd != "report" and not a.no_llm and bool(cfg("LLM_BASE_URL"))
        analyses = []
        for it in items:
            try:
                analyses.append(analyze_one(it, a.audio_dir, use_llm))
                print(f"[analyze] {it['title'][:40]}")
            except Exception as e:
                print(f"[analyze fail] {it['title'][:40]}: {e}", file=sys.stderr)
                analyses.append({"error": str(e)})
            if use_llm:
                con.execute("INSERT OR REPLACE INTO analysis VALUES (?,?,?)",
                            (it["video_id"], dt.datetime.utcnow().isoformat(),
                             json.dumps(analyses[-1], ensure_ascii=False)))
        con.commit()
        write_report(items, analyses, a.days, Path(a.out))


if __name__ == "__main__":
    main()
