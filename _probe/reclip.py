"""按新运镜参数重出分镜片段（**不重新出图、不花 API 钱**）。

为什么需要它：`clip_path` 是分镜阶段预渲染好的 20 秒缓动片段，
`media.PUSH_AMOUNT` 改了以后**老项目仍留着旧片段**，改参数不会自动生效。
本脚本就用已有的 `image_path` 重新生成一遍片段，路径不变（Asset 记录无需改动）。

用法：
    python _probe/reclip.py [项目名关键字 ...]
    python _probe/reclip.py --dry          # 只统计，不出片段

⚠️ 跑前先停掉常驻后端（队列 worker 会抢任务、用旧配置执行）。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, "backend")

from sqlalchemy import select  # noqa: E402

from app.adapters import media  # noqa: E402
from app.db import init_db, session_scope  # noqa: E402
from app.models import Project, StoryboardShot  # noqa: E402
from app.services.storyboard import CLIP_SECONDS  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    names = args or ["E2E"]

    print(f"运镜参数：PUSH_AMOUNT={media.PUSH_AMOUNT}  PRE_SCALE={media.PRE_SCALE}")
    print(f"（旧值是 0.10 / 1.0 —— 每镜头实际只推近 2.8%，几乎静止）\n")
    init_db()

    done = skipped = 0
    for kw in names:
        with session_scope() as db:
            p = db.execute(select(Project).where(Project.name.like(f"%{kw}%"))).scalar_one()
            shots = db.execute(
                select(StoryboardShot).where(StoryboardShot.project_id == p.id)
                .order_by(StoryboardShot.order)
            ).scalars().all()
            print(f"项目：{p.name}  分镜 {len(shots)} 句")
            if dry:
                print("  （--dry，未出片段）")
                continue
            t0 = time.time()
            for s in shots:
                img, clip = Path(s.image_path or ""), Path(s.clip_path or "")
                if not img.exists():
                    skipped += 1
                    continue
                old = media.probe(clip).get("duration_s") if clip.exists() else 0
                media.image_to_clip(img, clip, seconds=CLIP_SECONDS,
                                    width=1920, height=1080)
                new = media.probe(clip).get("duration_s")
                done += 1
                print(f"  #{s.order:>2} {str(s.place)[:22]:<24} {old or 0:>5.1f}s → {new:>5.1f}s")
            print(f"  共重出 {done} 段，跳过（无图）{skipped} 段，耗时 {time.time()-t0:.1f}s")

    print(f"\n完成：{done} 段片段已按新运镜参数重建")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
