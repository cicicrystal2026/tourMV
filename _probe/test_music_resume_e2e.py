"""端到端验证：候选任务重试时**续跑上游任务**，而不是重新提交（防重复计费）。

场景复刻线上真实故障：
  1) 第一次执行：提交成功（计费）→ 轮询时 ReadTimeout → 候选被判 failed；
  2) 队列重试：第二次执行 —— 如果是旧逻辑，会**再提交一次**（再扣一次钱）；
     新逻辑应识别出 provider_job_id 已存在，改为续跑轮询。

断言两条：
  - 上游提交次数 == 1（关键，直接对应钱包）；
  - 最终候选 done 且 error 被清空（不再挂红字）。

运行方式：复制本地库到临时目录后执行，**不触碰真实项目数据**，也不发任何真实请求。
"""
from __future__ import annotations

import pathlib
import shutil
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[1]

# 必须在 import app 之前把数据目录指到副本，否则会动到真实库
TMP = pathlib.Path(tempfile.mkdtemp(prefix="wenlv-mureka-e2e-"))
shutil.copy(ROOT / "data" / "wenlv.db", TMP / "wenlv.db")
import os                                          # noqa: E402

os.environ["DATA_DIR"] = str(TMP)

sys.path.insert(0, str(ROOT / "backend"))

from unittest import mock                          # noqa: E402

from sqlalchemy import select                      # noqa: E402

from app.adapters.music import base as mb          # noqa: E402
from app.db import init_db, session_scope          # noqa: E402
from app.models import LyricVersion, MusicCandidate, Project   # noqa: E402
from app.queue import JobContext, enqueue          # noqa: E402
from app.workers import tasks                      # noqa: E402


class FakeMureka(mb.MusicAdapter):
    """只记账、不发请求的假适配器。"""

    key, name, run_mode = "mureka", "Mureka", "api"
    submits = 0            # 提交次数 —— 每次提交在真实世界里都要花钱
    first_poll_fails = True

    def capabilities(self) -> dict:
        return {}

    def ready(self) -> bool:
        return True

    def generate(self, req) -> str:
        type(self).submits += 1
        return "up-job-1470001"

    def get_result(self, job_id: str) -> mb.GenResult:
        if type(self).first_poll_fails:
            type(self).first_poll_fails = False
            raise RuntimeError(
                "ReadTimeout: HTTPSConnectionPool(host='api.mureka.ai', port=443): "
                "Read timed out. (read timeout=60)")
        return mb.GenResult(status="done", audio_path=str(TMP / "fake.mp3"),
                            duration_s=161.95, bpm=84.0, cost=0.324)


def run_once(jid: str, tag: str) -> None:
    with session_scope() as db:
        ctx = JobContext(jid, db)
        try:
            tasks.h_music_one(ctx)
            print(f"  {tag}: 执行完成")
        except Exception as e:                       # noqa: BLE001
            print(f"  {tag}: 抛出 {type(e).__name__}: {str(e)[:70]}")


def main() -> None:
    init_db()
    mb.register(FakeMureka())
    init_db()                                        # 触发自愈（清残留 error）

    with session_scope() as db:
        p = db.execute(select(Project).where(Project.name.like("%中轴%"))).scalars().first()
        ver = db.execute(select(LyricVersion).where(LyricVersion.project_id == p.id)
                         .order_by(LyricVersion.version.desc())).scalars().first()
        c = MusicCandidate(project_id=p.id, lyric_version_id=ver.id, round=9, model_key="mureka",
                           model_name="Mureka", variant=1, label="Z", status="queued",
                           audio_token="testtoken")
        db.add(c)
        db.flush()
        j = enqueue(db, "music_one", queue="api", project_id=p.id, ref_id=c.id,
                    name="回归测试任务", payload={"candidate_id": c.id})
        j.status = "running"
        db.commit()
        cid, jid = c.id, j.id
        print(f"项目：{p.name} / 候选 {cid[:8]} / 任务 {jid[:8]}\n")

    patcher = (
        mock.patch.object(tasks.media, "probe", lambda _p: {"duration_s": 161.95}),
        mock.patch.object(tasks.llm, "json_chat",
                          lambda *a, **k: ({"score": 60, "reason": "test"}, True)),
    )
    for pt in patcher:
        pt.start()
    try:
        print("① 第一次执行（模拟轮询读超时）")
        run_once(jid, "第一次")
        with session_scope() as db:
            c = db.get(MusicCandidate, cid)
            print(f"     状态={c.status}  上游任务号={c.provider_job_id or '(空)'}  "
                  f"提交次数={FakeMureka.submits}")

        print("\n② 队列重试（关键：必须续跑，不能重新提交）")
        run_once(jid, "第二次")
    finally:
        for pt in patcher:
            pt.stop()

    with session_scope() as db:
        c = db.get(MusicCandidate, cid)
        print(f"\n最终：状态={c.status}  时长={c.duration_s}  "
              f"error={'（空）' if not c.error else c.error[:60]}")
        print(f"上游提交次数 = {FakeMureka.submits}  (期望 1)")

    ok = FakeMureka.submits == 1 and c.status == "done" and not c.error
    print("\n结论：" + ("通过 —— 重试未重复提交，且残留错误已清除 ✓"
                      if ok else "失败 ✗"))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
