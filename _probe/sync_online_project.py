"""把线上「本地没有」的项目同步回本地库。

背景（必读）：发布工具**不读 .gitignore**，`data/wenlv.db` 会随包上传并覆盖线上库。
用户直接在域名上新建的项目（如「天津·海河夜航 · 二创」）如果不先补回本地，
一次发布就没了。

用法：
    python _probe/sync_online_project.py <project_id> [--dry]

只动指定的这个项目：project / brief / lyric_version / lyric_check 四张表。
幂等：重复执行结果一致。
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "wenlv.db"
BASE = "https://tourmv-pipeline.app.workbuddy.host"

# 各表的写入列（顺序即 INSERT 列顺序）
COLS = {
    "project": ["id", "name", "destination", "type", "language", "subtitle_language",
                "status", "status_note", "selected_candidate_id", "total_cost", "reject_count",
                "created_at", "updated_at", "locked_at", "parent_project_id"],
    "brief": ["id", "project_id", "mood", "genre", "bpm_min", "bpm_max", "duration_s",
              "vocal", "style_prompt", "trend_card_ids", "kb_refs", "forbidden",
              "created_at", "updated_at"],
    "lyric_version": ["id", "project_id", "version", "sections", "source", "checks_done",
                      "created_at", "updated_at"],
    "lyric_check": ["id", "project_id", "lyric_version_id", "type", "line_no", "section",
                    "text", "status", "evidence", "suggestions", "keep_reason",
                    "needs_manual", "kb_entry_ids", "created_at", "updated_at"],
}
JSON_COLS = {"mood", "trend_card_ids", "kb_refs", "sections", "suggestions", "kb_entry_ids"}


def _dt(s) -> str:
    """ISO8601 → 本地库的 DateTime 文本（'2026-10-02 14:55:16.953912'）。"""
    return (str(s or "")).replace("T", " ").replace("+00:00", "").replace("Z", "").strip()


def get(path: str, tries: int = 3) -> dict:
    for i in range(tries):
        try:
            with urllib.request.urlopen(BASE + path, timeout=45) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            print(f"  重试 {i + 1}：{type(e).__name__}")
            time.sleep(2 + i * 2)
    return {}


def _val(row: dict, col: str, default=None):
    v = row.get(col, default)
    if col in JSON_COLS:
        return json.dumps(v if v is not None else [], ensure_ascii=False)
    if col in ("created_at", "updated_at", "locked_at"):
        return _dt(v) or None
    return v


def upsert(cur: sqlite3.Cursor, table: str, row: dict, key: str = "id") -> None:
    cols = COLS[table]
    present = [c for c in cols if c in row or c == "project_id"]
    values = [_val(row, c) for c in present]
    sql = (f"INSERT INTO {table} ({','.join(present)}) VALUES ({','.join('?' * len(present))}) "
           f"ON CONFLICT({key}) DO UPDATE SET "
           + ",".join(f"{c}=excluded.{c}" for c in present if c != key))
    cur.execute(sql, values)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("project_id")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    pid = args.project_id

    print(f"拉取线上项目 {pid} …")
    d = get(f"/api/projects/{pid}")
    ly = get(f"/api/projects/{pid}/lyrics")
    p = d.get("project") or {}
    if not p:
        print("线上没有这个项目")
        return 2
    versions = ly.get("versions") or []
    checks = ly.get("checks") or []
    brief = d.get("brief") or {}
    print(f"  {p['name']}  状态={p['status']}  歌词版本={len(versions)}  检查项={len(checks)}  "
          f"简报={'有' if brief else '无'}")

    if args.dry:
        print("（--dry，未写库）")
        return 0

    db = sqlite3.connect(DB)
    cur = db.cursor()
    now = _dt(p.get("updated_at"))

    upsert(cur, "project", p)
    if brief:
        brief = dict(brief)
        brief["project_id"] = pid
        brief.setdefault("created_at", now)
        brief.setdefault("updated_at", now)
        upsert(cur, "brief", brief)

    for v in versions:
        row = dict(v)
        row["project_id"] = pid
        row.setdefault("updated_at", v.get("created_at"))
        upsert(cur, "lyric_version", row)

    # 检查项只挂在「当前版本」上（接口只返回当前版本的检查结果）
    cur_ver = (ly.get("current") or {}).get("id")
    for c in checks:
        row = dict(c)
        row["project_id"] = pid
        row.setdefault("lyric_version_id", cur_ver)
        row.setdefault("created_at", now)
        row.setdefault("updated_at", now)
        upsert(cur, "lyric_check", row)

    db.commit()
    print("\n写入结果：")
    for t, sql in (("project", "SELECT name,status,total_cost FROM project WHERE id=?"),
                   ("brief", "SELECT genre,bpm_min,bpm_max,duration_s FROM brief WHERE project_id=?"),
                   ("lyric_version", "SELECT version,source,checks_done FROM lyric_version WHERE project_id=? ORDER BY version"),
                   ("lyric_check", "SELECT type,status,line_no FROM lyric_check WHERE project_id=?")):
        print(f"  {t}: {list(cur.execute(sql, (pid,)))}")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
