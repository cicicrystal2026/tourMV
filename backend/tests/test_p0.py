"""PRD V1.1 P0（W2：安全与数据卫生 / 闸门与状态 / 词曲）验收用例。"""
from __future__ import annotations

import datetime as dt

import pytest

from conftest import wait_status

LIJIANG = "ad4bd963-32fd-4f32-81ff-a4b976220e47"     # 已交付，含 6 个 mock 候选
BEIJING = "1c722bfb-ee67-4f46-a7fa-5e719d239141"     # 已交付，18 张 AI 配图
CHANGBAI = "ff856d7a-5015-4561-9939-d5a2ee77d949"    # 待确认歌词，3 条史实疑点


def _settings():
    from app.config import settings
    return settings()


# ------------------------------------------------------------------ 迁移（附录 B）
def test_migration_on_real_data(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Asset, MusicCandidate, MusicRound, Project, TrendCard

    with SessionLocal() as db:
        lj = db.get(Project, LIJIANG)
        assert lj.locked_at is not None, "已交付项目应被锁定"
        cands = list(db.execute(select(MusicCandidate).where(MusicCandidate.project_id == LIJIANG)).scalars())
        assert sum(c.is_mock for c in cands) == 6, "漓江 A–F 六个占位候选应标为 mock"
        assert all(c.round_id and c.audio_token for c in cands)
        kinds = {r.kind for r in db.execute(select(MusicRound)).scalars()}
        assert kinds == {"production"}, "V1.0 存量轮次一律视为生产模式"
        ai = list(db.execute(select(Asset).where(Asset.source == "ai")).scalars())
        assert len(ai) >= 18 and all(a.license_status == "ai_generated" for a in ai)
        assert all(c.is_seed for c in db.execute(select(TrendCard)).scalars() if c.video_id.startswith("demo"))

    models = {m["key"]: m for m in client.get("/api/music/models").json()}
    assert all(m["wins"] == 0 and m["avg_score"] == 0 for m in models.values()), \
        "没有选型轮之前，模型战绩必须为 0（V1.0 的 mock 预置评分不再计入）"


# ------------------------------------------------------------------ 路径与安全
def test_no_server_paths_leak(client):
    info = client.get("/api/system/info").json()
    for k in ("project_root", "data_dir", "db_path"):
        assert k not in info
    assert "budget" in info and "auth_enabled" in info
    assert "data_dir" not in client.get("/api/health").json()
    csv = client.get("/api/assets/export").text
    assert ":\\" not in csv and "/workspace" not in csv
    tl = client.get(f"/api/projects/{LIJIANG}/timeline").json()
    assert "audio_path" not in (tl.get("timeline") or {})


def test_access_password(client):
    s = _settings()
    s.access_password, s.session_secret = "pw-123", "test-secret"
    try:
        assert client.get("/api/health").status_code == 200
        client.cookies.clear()
        r = client.get("/api/projects")
        assert r.status_code == 401 and r.json()["auth_required"]
        assert client.get("/api/media/audio/whatever").status_code == 401
        assert client.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        assert client.post("/api/auth/login", json={"password": "pw-123"}).status_code == 200
        assert client.get("/api/projects").status_code == 200
        client.cookies.clear()
        assert client.get("/api/projects", headers={"X-Access-Token": "pw-123"}).status_code == 200
    finally:
        s.access_password, s.session_secret = "", ""
        client.cookies.clear()


# ------------------------------------------------------------------ 锁定与修订版
def test_delivered_project_is_read_only(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    d = client.get(f"/api/projects/{BEIJING}").json()
    assert d["project"]["locked"] and not d["gate"]["ok"]
    assert client.post(f"/api/projects/{BEIJING}/advance").status_code == 400
    assert client.post(f"/api/projects/{BEIJING}/storyboard/plan").status_code == 400
    assert client.post(f"/api/projects/{BEIJING}/timeline/resnap").status_code == 400
    # 投放回填不受锁定影响
    assert client.post(f"/api/projects/{BEIJING}/metrics",
                       json={"platform": "YouTube", "views_7d": 10}).status_code == 200

    r = client.post(f"/api/projects/{LIJIANG}/revise")
    assert r.status_code == 200, r.text
    rev = r.json()
    assert rev["parent_project_id"] == LIJIANG and rev["status"] == "lyric_review"
    assert "修订" in rev["name"] and not rev["locked"]


# ------------------------------------------------------------------ 歌词闸门
def test_lyric_gate_and_keep_reason(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    # 本地库里长白山还在「简报中」：确认简报 → 后台写词 → 停在待确认歌词
    if client.get(f"/api/projects/{CHANGBAI}").json()["project"]["status"] == "briefing":
        assert client.post(f"/api/projects/{CHANGBAI}/advance").status_code == 200
        wait_status(client, CHANGBAI, "lyric_review")
    lyr = client.get(f"/api/projects/{CHANGBAI}/lyrics").json()
    assert lyr["summary"]["state"] == "done"
    pending = [c for c in lyr["checks"] if c["status"] == "pending"]
    for c in lyr["checks"]:
        for s in c["suggestions"]:
            assert 6 <= len(s.replace(" ", "").replace("·", "")) <= 12, f"不合格建议：{s}"
    if not pending:
        pytest.skip("兜底检查未产出疑点，闸门用例无从验证")

    r = client.post(f"/api/projects/{CHANGBAI}/advance", json={"models": ["mureka"]})
    assert r.status_code == 409 and "未处理" in r.json()["detail"]

    # 保留原文必须写理由
    assert client.post(f"/api/lyrics/checks/{pending[0]['id']}/keep", json={}).status_code == 400
    for c in pending:
        r = client.post(f"/api/lyrics/checks/{c['id']}/keep", json={"reason": "已查证，属诗意表达"})
        assert r.status_code == 200 and r.json()["status"] == "kept"
    assert client.get(f"/api/projects/{CHANGBAI}").json()["gate"]["ok"]


def test_lyrics_not_editable_outside_review(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    hz = next(p for p in client.get("/api/projects").json() if p["status"] == "briefing")
    assert client.post(f"/api/projects/{hz['id']}/lyrics/generate?sync=true").status_code == 400


def test_check_summary_states(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.db import SessionLocal
    from app.models import LyricVersion

    hz = next(p for p in client.get("/api/projects").json() if p["status"] == "briefing")
    assert client.get(f"/api/projects/{hz['id']}/lyrics").json()["summary"]["state"] == "none"
    with SessionLocal() as db:
        db.add(LyricVersion(project_id=hz["id"], version=1, sections=[{"name": "主歌1", "lines": ["测试一行歌词"]}],
                            checks_done=False))
        db.commit()
    # 有歌词但检查未完成 → checking，绝不能是「通过」
    assert client.get(f"/api/projects/{hz['id']}/lyrics").json()["summary"]["state"] == "checking"


def test_suggestion_filter():
    from app.services.lyrics import _good_suggestion

    assert not _good_suggestion("我想带你去桂林·", "我想带你去桂林·漓江 看一次日落")   # 截断
    assert not _good_suggestion("如果远方有名字 那一", "如果远方有名字 那一定叫桂林·漓江")
    assert not _good_suggestion("桂林·漓江的清晨 雾还没江", "桂林·漓江的清晨 雾还没散")  # 只换尾字
    assert not _good_suggestion("太短", "原句很长很长很长很长")
    assert _good_suggestion("鸭绿江源隐翠微", "鸭绿江源细如线")


# ------------------------------------------------------------------ 生曲：生产模式 + mock 隔离
def test_production_mode_and_mock_isolation(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    r = client.post(f"/api/projects/{CHANGBAI}/advance", json={"models": ["mureka"]})
    assert r.status_code == 200, r.text
    wait_status(client, CHANGBAI, "music_review")
    c = client.get(f"/api/projects/{CHANGBAI}/music/candidates").json()
    assert c["round_meta"]["kind"] == "production" and not c["round_meta"]["blind"]
    item = c["items"][0]
    assert item["is_mock"] and item["fit_score"] is None, "占位结果不打匹配分"
    assert item["model_name"] and item["audio_url"].startswith("/api/media/audio/")
    assert client.get(item["audio_url"]).status_code == 200

    # 关掉 ALLOW_MOCK：未配置的模型不能生成，占位候选不能选定、也过不了闸门
    s = _settings()
    old = s.allow_mock
    s.allow_mock = False
    try:
        r = client.post(f"/api/projects/{CHANGBAI}/music/generate", json={"models": ["mureka"]})
        assert r.status_code == 400 and "未配置" in r.json()["detail"]
        r = client.post(f"/api/projects/{CHANGBAI}/music/select", json={"candidate_id": item["id"]})
        assert r.status_code == 400 and "占位" in r.json()["detail"]
    finally:
        s.allow_mock = old


# ------------------------------------------------------------------ 生曲：选型轮盲听 + 统计口径
def test_selection_round_blind_and_stats(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import MusicCandidate

    # 选型轮至少 2 个模型
    r = client.post(f"/api/projects/{CHANGBAI}/music/generate", json={"mode": "selection", "models": ["mureka"]})
    assert r.status_code == 400
    r = client.post(f"/api/projects/{CHANGBAI}/music/generate",
                    json={"mode": "selection", "models": ["mureka", "lyria"], "variants": 2})
    assert r.status_code == 200, r.text
    rid = r.json()["round_id"]

    import time
    for _ in range(180):
        items = client.get(f"/api/projects/{CHANGBAI}/music/candidates?round_id={rid}").json()["items"]
        if items and all(i["status"] in ("done", "failed") for i in items):
            break
        time.sleep(0.5)
    payload = client.get(f"/api/projects/{CHANGBAI}/music/candidates?round_id={rid}&blind=false").json()
    assert payload["round_meta"]["blind"] and payload["round_meta"]["model_keys"] is None
    for it in payload["items"]:
        assert it["model_name"] is None and it["model_key"] is None and it["cost"] is None
        assert "mock" not in it["audio_url"] and "mureka" not in it["audio_url"]
    # 任务队列里也不能出现模型名
    jobs = client.get("/api/jobs").json()["items"]
    for j in jobs:
        if j["type"] == "music_one" and "选型轮" in j["name"]:
            assert "Mureka" not in j["name"] and "Lyria" not in j["name"]

    # 回归（E2E 抓到）：占位候选也必须全部评分后才能揭晓
    first_mock = payload["items"][0]
    r = client.post(f"/api/projects/{CHANGBAI}/music/select", json={"candidate_id": first_mock["id"]})
    assert r.status_code == 400 and "未评分" in r.json()["detail"], "未评分时选型轮不能揭晓（含占位候选）"

    # 测统计口径：把本轮候选当作真实结果（mock 不计入统计）
    with SessionLocal() as db:
        for c in db.execute(select(MusicCandidate).where(MusicCandidate.round_id == rid)).scalars():
            c.is_mock = False
        db.commit()
    first = payload["items"][0]
    r = client.post(f"/api/projects/{CHANGBAI}/music/select", json={"candidate_id": first["id"]})
    assert r.status_code == 400 and "未评分" in r.json()["detail"]
    for i, it in enumerate(payload["items"]):
        v = 5 if it["id"] == first["id"] else 3
        assert client.put(f"/api/music/candidates/{it['id']}/rating",
                          json={"diction": v, "melody": v, "arrangement": v}).status_code == 200
    # 揭晓前统计不变（否则看均分变化就能反推模型）
    models = {m["key"]: m for m in client.get("/api/music/models").json()}
    assert models["mureka"]["rounds"] == 0 and models["lyria"]["rounds"] == 0
    r = client.post(f"/api/projects/{CHANGBAI}/music/select", json={"candidate_id": first["id"]})
    assert r.status_code == 200 and r.json()["revealed"]
    winner_key = r.json()["model_key"]
    models = {m["key"]: m for m in client.get("/api/music/models").json()}
    assert models[winner_key]["wins"] == 1
    assert models["mureka"]["rounds"] == 1 and models["lyria"]["rounds"] == 1
    assert 1 <= models[winner_key]["avg_score"] <= 5, "均分是 1–5 分，不是三项总和"
    after = client.get(f"/api/projects/{CHANGBAI}/music/candidates?round_id={rid}").json()
    assert not after["round_meta"]["blind"] and after["items"][0]["model_name"]


# ------------------------------------------------------------------ 费用上限
def test_daily_budget(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.db import SessionLocal
    from app.models import AiCall

    s = _settings()
    old = s.daily_cost_limit
    s.daily_cost_limit = 0.5
    try:
        with SessionLocal() as db:
            db.add(AiCall(kind="image", provider="wanx", cost=1.0))
            db.commit()
        info = client.get("/api/system/info").json()["budget"]
        assert info["spent"] >= 1.0 and info["remaining"] == 0
        r = client.post(f"/api/projects/{CHANGBAI}/storyboard/enqueue")
        assert r.status_code == 400 and "费用上限" in r.json()["detail"]
    finally:
        s.daily_cost_limit = old


def test_dashboard_timestamps_have_timezone(client):
    d = client.get("/api/dashboard/summary").json()
    stamps = [it["updated_at"] for col in d["columns"] for it in col["items"]]
    assert stamps and all(("+00:00" in t) or t.endswith("Z") for t in stamps)
    dt.datetime.fromisoformat(stamps[0])


# ------------------------------------------------------------------ 配图兜底链（实拍优先、AI 补位）
def test_storyboard_fallback_chain(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.db import SessionLocal
    from app.models import StoryboardShot

    # 演示模式 + 生图模型未就绪：素材库先匹配，匹配不到的出占位，不能卡在「待生成」
    r = client.post(f"/api/projects/{CHANGBAI}/storyboard/plan?force=true")
    assert r.status_code == 200, r.text
    r = client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")
    assert r.status_code == 200, r.text
    res = r.json()
    assert res["failed"] == 0 and res["ok"] == res["total"] > 0
    assert res["by_source"].get("library_seed", 0) >= 1, "长白山的演示素材应先被素材库匹配到"
    sb = client.get(f"/api/projects/{CHANGBAI}/storyboard").json()
    assert all(it["image_url"] and it["status"] == "pending" for it in sb["items"])
    lib = [it["asset_id"] for it in sb["items"] if it["source"].startswith("library")]
    assert len(lib) == len(set(lib)), "一支片内同一个素材库镜头不能重复用"
    assert not sb["job_active"]

    # 生产环境（禁用占位）：素材库没有实拍时如实标「缺镜头」，不出占位；占位画面过不了闸门
    s = _settings()
    old = s.allow_mock
    s.allow_mock = False
    try:
        with SessionLocal() as db:
            for row in db.query(StoryboardShot).filter(StoryboardShot.project_id == CHANGBAI):
                row.status = "approved"
            db.commit()
        from app.gates import check
        from app.models import Project
        with SessionLocal() as db:
            p = db.get(Project, CHANGBAI)
            p.status = "storyboard_review"
            db.commit()
            g = check(db, p)
        assert not g.ok and any("占位" in x for x in g.reasons)

        r = client.post(f"/api/projects/{CHANGBAI}/storyboard/plan?force=true")
        r = client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")
        assert r.status_code == 200, r.text
        res = r.json()
        assert "placeholder" not in res["by_source"] and "library_seed" not in res["by_source"]
        assert res["failed"] >= 1 and "素材库没有" in res["errors"][0]["error"]
    finally:
        s.allow_mock = old
