"""发布前必做：逐项比对本地库与线上库，找出会被发布覆盖掉的线上数据。

为什么必须做：发布工具**不读 .gitignore**，`data/wenlv.db` 会随包上传并覆盖线上库
（工具说明只排除 node_modules/.git/build）。所以「线上有、本地没有」的项目
会在发布后消失。

只读，不写任何东西。输出：每个项目的 本地/线上 状态与各表条数对照。
"""
from __future__ import annotations

import json
import pathlib
import sqlite3
import time
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "wenlv.db"
BASE = "https://tourmv-pipeline.app.workbuddy.host"

TABLES = ["lyric_version", "music_candidate", "render_job", "storyboard_shot", "timeline"]


def get(path: str, tries: int = 3) -> dict:
    for i in range(tries):
        try:
            with urllib.request.urlopen(BASE + path, timeout=40) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(2 + i * 2)
    return {}


def main() -> int:
    online = get("/api/projects")
    if not online:
        print("线上不可达，中止")
        return 2

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    local = {r["id"]: dict(r) for r in db.execute("SELECT id, name, status, total_cost FROM project")}
    counts: dict[str, dict[str, int]] = {}
    for pid in list(local) + [p["id"] for p in online]:
        counts[pid] = {}
        for t in TABLES:
            try:
                counts[pid][t] = db.execute(f"SELECT count(*) FROM {t} WHERE project_id=?", (pid,)).fetchone()[0]
            except sqlite3.Error:
                counts[pid][t] = -1

    print(f"本地 {len(local)} 个项目 / 线上 {len(online)} 个项目\n")
    hdr = f"{'项目':<22}{'来源':<6}{'状态':<16}{'歌词':>5}{'候选':>5}{'成片':>5}{'分镜':>5}"
    print(hdr)
    print("-" * 66)
    only_online, mismatched = [], []
    for p in online:
        pid, name = p["id"], p["name"]
        lc = counts[pid]
        on_c = get(f"/api/projects/{pid}/music/candidates").get("items", [])
        on_d = get(f"/api/projects/{pid}")
        on_renders = len(on_d.get("renders") or [])
        lv_online = len(((on_d.get("lyric") or {}).get("versions") or [])) or (
            1 if (on_d.get("lyric") or {}).get("current") else 0)
        if pid not in local:
            only_online.append(name)
            print(f"{name:<22}{'仅线上':<6}{p['status']:<16}{lv_online:>5}{len(on_c):>5}{on_renders:>5}{'?':>5}")
            continue
        same = local[pid]["status"] == p["status"]
        if not same:
            mismatched.append(name)
        print(f"{name:<22}{'both':<6}{p['status']:<16}{lv_online:>5}{len(on_c):>5}{on_renders:>5}"
              f"{counts[pid]['storyboard_shot']:>5}{'' if same else '  ← 状态不一致'}")
    for pid, row in local.items():
        if any(p["id"] == pid for p in online):
            continue
        print(f"{row['name']:<22}{'仅本地':<6}{row['status']:<16}{'':>5}{'':>5}{'':>5}"
              f"{counts[pid]['storyboard_shot']:>5}")

    print()
    if only_online:
        print("⚠ 仅线上存在（发布会被覆盖丢失）：", "、".join(only_online))
    if mismatched:
        print("⚠ 状态不一致：", "、".join(mismatched))
    if not only_online and not mismatched:
        print("✓ 本地库覆盖线上库不会丢数据")
    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
