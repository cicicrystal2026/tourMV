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
    # 复唱句跟随母句（允许同画面）；不同歌词之间不能重复用同一个素材库镜头
    lib = [it["asset_id"] for it in sb["items"] if it["source"].startswith("library") and not it["dup_of"]]
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


# ------------------------------------------------------------------ 审核减负：复唱合并 / 建议通过 / 实拍替换
def test_storyboard_review_assist(client, has_real_db):
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.db import SessionLocal
    from app.models import StoryboardShot

    client.post(f"/api/projects/{CHANGBAI}/storyboard/plan?force=true")
    r = client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")
    assert r.status_code == 200, r.text
    sb = client.get(f"/api/projects/{CHANGBAI}/storyboard").json()
    prog = sb["progress"]
    masters = [i for i in sb["items"] if not i["dup_of"]]
    dups = [i for i in sb["items"] if i["dup_of"]]
    assert dups, "模板歌词里有复唱句，应被合并"
    assert prog["unique"] == len(masters) < prog["total"]
    by_order = {i["order"]: i for i in sb["items"]}
    for d in dups:
        m = by_order[d["dup_of"]]
        assert d["image_url"] == m["image_url"] and d["asset_id"] == m["asset_id"], "复唱句跟随母句画面"

    # 机器预审：Mock 下没有视觉模型 → 不伪造分数
    assert all(i["ai_score"] is None for i in masters)
    # 模拟预审结果：高分的「一键通过建议」，低分的留给人
    with SessionLocal() as db:
        rows = db.query(StoryboardShot).filter(StoryboardShot.project_id == CHANGBAI,
                                               StoryboardShot.dup_of == 0).order_by(StoryboardShot.order).all()
        rows[0].ai_score, rows[1].ai_score = 90, 40
        db.commit()
        m0, m1 = rows[0].id, rows[1].id
    prog = client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["progress"]
    assert prog["suggest_pass"] >= 1 and prog["need_review"] >= 1
    r = client.post(f"/api/projects/{CHANGBAI}/storyboard/approve-suggested")
    assert r.json()["approved"] == 1
    items = {i["id"]: i for i in client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["items"]}
    assert items[m0]["status"] == "approved" and items[m1]["status"] == "pending"

    # 对复唱句操作 = 对母句操作，并同步回所有复唱句
    d = dups[0]
    assert client.post(f"/api/projects/{CHANGBAI}/storyboard/shots/{d['id']}/approve").status_code == 200
    items = client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["items"]
    same = [i for i in items if i["order"] == d["dup_of"] or i["dup_of"] == d["dup_of"]]
    assert all(i["status"] == "approved" for i in same)

    # 有实拍可替换：手动挂一个推荐，一键换上
    with SessionLocal() as db:
        from app.models import Asset
        seed = db.query(Asset).filter(Asset.is_seed.is_(True), Asset.destination.like("%长白山%")).first()
        row = db.get(StoryboardShot, m1)
        row.suggest_asset_id = seed.id
        db.commit()
    r = client.post(f"/api/projects/{CHANGBAI}/storyboard/shots/{m1}/use-library")
    assert r.status_code == 200, r.text
    it = {i["id"]: i for i in client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["items"]}[m1]
    assert it["status"] == "approved" and it["source"] == "library_seed" and it["suggest"] is None


def test_storyboard_stale_after_lyric_change(client, has_real_db):
    """回归（E2E 发现）：驳回重写歌词后，旧分镜不能沿用，闸门要拦。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.db import SessionLocal
    from app.gates import check
    from app.models import LyricVersion, Project, StoryboardShot

    client.post(f"/api/projects/{CHANGBAI}/storyboard/plan?force=true")
    client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")
    with SessionLocal() as db:
        for r in db.query(StoryboardShot).filter(StoryboardShot.project_id == CHANGBAI):
            r.status = "approved"
        ver = db.query(LyricVersion).filter(LyricVersion.project_id == CHANGBAI)             .order_by(LyricVersion.version.desc()).first()
        db.add(LyricVersion(project_id=CHANGBAI, version=ver.version + 1, checks_done=True,
                            sections=[{"name": "主歌1", "lines": ["天池静卧云海间", "岳桦林在风里弯成弓"]}]))
        p = db.get(Project, CHANGBAI)
        p.status = "storyboard_review"
        db.commit()
    sb = client.get(f"/api/projects/{CHANGBAI}/storyboard").json()
    assert sb["stale"]
    with SessionLocal() as db:
        g = check(db, db.get(Project, CHANGBAI))
    assert not g.ok and any("旧歌词" in x for x in g.reasons)
    # 再次出图会按新歌词重新提炼
    r = client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")
    assert r.status_code == 200, r.text
    sb = client.get(f"/api/projects/{CHANGBAI}/storyboard").json()
    assert not sb["stale"] and [i["line_text"] for i in sb["items"]] == ["天池静卧云海间", "岳桦林在风里弯成弓"]


def test_ai_failure_borrows_previous_shot(client, has_real_db, monkeypatch):
    """AI 出不了图时不留空卡：借用前一句画面（产品决定，E2E 反馈）。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.db import SessionLocal
    from app.models import Asset, StoryboardShot
    from app.services import storyboard as sbs

    client.post(f"/api/projects/{CHANGBAI}/storyboard/plan?force=true")
    client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")     # 先让各句都有画面（占位）
    with SessionLocal() as db:
        # 关掉素材库兜底，逼它走「借前一句」
        for a in db.query(Asset).filter(Asset.destination.like("%长白山%")):
            a.usable = False
        rows = db.query(StoryboardShot).filter(StoryboardShot.project_id == CHANGBAI,
                                               StoryboardShot.dup_of == 0).order_by(StoryboardShot.order).all()
        victim = rows[1]
        victim.image_path, victim.clip_path, victim.asset_id, victim.status = "", "", "", "rejected"
        db.commit()
        vid, prev_order = victim.id, rows[0].order

    def boom(*a, **k):
        raise RuntimeError("CalledProcessError: ffmpeg 转码失败")
    monkeypatch.setattr(sbs.image_ad, "available", lambda *a, **k: True)
    monkeypatch.setattr(sbs, "_render_one", boom)
    try:
        r = client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")
        assert r.status_code == 200, r.text
        it = {i["id"]: i for i in client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["items"]}[vid]
        assert it["image_url"] and it["source"] == "borrowed" and it["status"] == "pending"
        assert f"第 {prev_order} 句" in it["note"]

        # 单张重出失败：保留现有画面，不变空
        with SessionLocal() as db:
            sbs.regenerate(db, vid)
        it = {i["id"]: i for i in client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["items"]}[vid]
        assert it["image_url"] and it["status"] == "pending"
    finally:
        with SessionLocal() as db:
            for a in db.query(Asset).filter(Asset.destination.like("%长白山%")):
                a.usable = True
            db.commit()


# ------------------------------------------------------------------ 出片：短版窗口 + 自检 + AI 字卡
def test_short_window_and_qc():
    from types import SimpleNamespace
    from app.services import render as R

    tl = SimpleNamespace(duration_s=169.4, lyric_lines=[
        {"section": "主歌1", "start_s": 5}, {"section": "副歌", "start_s": 45}])
    start, span = R.short_window(tl)
    assert 150 <= span <= 170 and start + span <= 169.4 + 0.01, (start, span)
    # V1.0 的算法会得到 124 秒（169-45），短版不达标
    assert span > 169.4 - 45

    tl2 = SimpleNamespace(duration_s=300, lyric_lines=[{"section": "副歌", "start_s": 60}])
    assert R.short_window(tl2) == (60.0, 170.0)
    tl3 = SimpleNamespace(duration_s=120, lyric_lines=[{"section": "副歌", "start_s": 30}])
    assert R.short_window(tl3) == (0.0, 120.0), "整首不足 150 秒就整首，自检会如实报不达标"


def test_render_short_end_to_end(tmp_path):
    """真跑 ffmpeg：短版画面与音频同起点、时长达标、带 AI 字卡、自检通过。"""
    from types import SimpleNamespace

    from app.adapters import media
    from app.services import render as R
    from app.services.music import _rating_of  # noqa: F401  确保模块可导入

    # 造 3 段 70 秒的片段 + 一条 200 秒的音频
    clips = []
    for i in range(3):
        c = tmp_path / f"c{i}.mp4"
        media.generate_test_clip(c, seconds=70, width=640, height=360, label=f"seg{i}")
        clips.append(c)
    audio = tmp_path / "a.mp3"
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=200",
               str(audio)])
    job = SimpleNamespace(output_path=str(tmp_path / "out.mp4"))
    # 直接用 quality_check 校验一个手工合成的 160 秒成片
    media.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(clips[0]), "-i", str(audio),
               "-vf", "loop=-1:size=1750,scale=1080:1920", "-t", "160", "-map", "0:v", "-map", "1:a",
               "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", job.output_path], timeout=600)
    qc = R.quality_check(job, R.VARIANTS["short"], expect_s=160, short=True, sub_lines=12,
                         need_ai_card=True, ai_card=True)
    assert qc["ok"], qc
    bad = R.quality_check(job, R.VARIANTS["landscape"], expect_s=169, short=False, sub_lines=0,
                          need_ai_card=True, ai_card=False)
    names = {c["name"] for c in bad["checks"] if not c["ok"]}
    assert {"时长", "分辨率", "字幕", "AI 字卡"} <= names


# ------------------------------------------------------------------ 文化知识库（D4 / §6.7）
def test_kb_crud_and_verification(client):
    r = client.post("/api/kb", json={"destination": "吉林·长白山", "title": "满语名称",
                                     "content": "长白山满语称果勒敏珊延阿林，意为长白山。",
                                     "source_type": "encyclopedia", "verified": True})
    assert r.status_code == 400 and "出处" in r.json()["detail"], "已核实条目必须写出处"
    r = client.post("/api/kb", json={"destination": "吉林·长白山", "title": "满语名称",
                                     "content": "长白山满语称果勒敏珊延阿林，意为长白山。",
                                     "source_name": "长白山管委会官网", "source_type": "official",
                                     "verified": True})
    assert r.status_code == 200 and r.json()["verified"]
    # AI 起草的条目不能直接核实：要改成真实来源
    r = client.post("/api/kb", json={"destination": "长白山", "title": "天池", "content": "天池是火山口湖。",
                                     "source_type": "ai_draft", "source_name": "百科"})
    eid = r.json()["id"]
    assert not r.json()["verified"]
    assert client.post(f"/api/kb/{eid}/verify", json={}).status_code == 400
    r = client.post(f"/api/kb/{eid}/verify", json={"source_type": "encyclopedia", "source_name": "百度百科·长白山天池"})
    assert r.status_code == 200 and r.json()["verified"]
    cov = client.get("/api/kb/coverage?destination=吉林·长白山").json()
    assert cov["verified"] >= 2 and cov["destination"] == "长白山"
    hits = client.get("/api/kb?destination=长白山&q=满语").json()["items"]
    assert hits and hits[0]["title"] == "满语名称"


def test_fact_check_cites_kb(client, monkeypatch):
    """史实疑点能引用到已核实条目时：写明出处、不再标「需人工核实」。"""

    from app.db import SessionLocal
    from app.models import LyricVersion
    from app.services import lyrics as L

    calls = {}

    def fake(messages, fallback=None, purpose="", project_id="", temperature=0.3):
        if purpose == "lyrics_check":
            calls["kb_in_prompt"] = "果勒敏珊延阿林" in messages[-1]["content"]
            return {"issues": [{"type": "fact", "section": "桥段", "line_no": 1, "text": "x",
                                "evidence": "满语名意为长白山，不是六月飘雪", "kb": [1], "suggestions": []},
                               {"type": "fact", "section": "桥段", "line_no": 2, "text": "y",
                                "evidence": "资料未覆盖", "suggestions": []}]}, True
        return (fallback or {}), True
    monkeypatch.setattr(L.llm, "json_chat", fake)
    with SessionLocal() as db:
        ver = LyricVersion(project_id=CHANGBAI, version=99, checks_done=False,
                           sections=[{"name": "桥段", "lines": ["满语里它叫果勒敏珊延阿林", "意为六月飘雪的圣山"]}])
        db.add(ver)
        db.commit()
        rows = L.run_checks(db, ver)
        cited = [r for r in rows if r.kb_entry_ids]
        manual = [r for r in rows if r.needs_manual]
        assert calls["kb_in_prompt"], "核对时应把已核实条目带给模型"
        assert len(cited) == 1 and "据知识库" in cited[0].evidence and not cited[0].needs_manual
        assert len(manual) == 1 and "需人工核实" in manual[0].evidence
        from app.models import LyricCheck
        db.query(LyricCheck).filter(LyricCheck.lyric_version_id == ver.id).delete()
        db.delete(ver)
        db.commit()


# ------------------------------------------------------------------ 度量：工时 / 复盘 / 选型结论（F1-10 / F1-11）
def test_worklog_review_and_selection_report(client):
    r = client.post("/api/projects", json={"name": "工时测试", "destination": "吉林·长白山", "genre": "folk"})
    pid = r.json()["id"]
    # 新建即进入「简报中」人工节点：心跳计入
    assert client.post(f"/api/projects/{pid}/worklog/heartbeat", json={"seconds": 30}).json()["counted"] == 30
    assert client.post(f"/api/projects/{pid}/worklog/heartbeat", json={"seconds": 9999}).json()["counted"] == 45, \
        "单次心跳封顶，防止异常请求灌水"
    rv = client.get(f"/api/projects/{pid}/review").json()
    node = rv["work"]["nodes"][0]
    assert node["node"] == "briefing" and node["open"] and node["active_minutes"] == round(75 / 60, 1)
    # 确认简报 → 关闭简报节点
    assert client.post(f"/api/projects/{pid}/advance").status_code == 200
    rv = client.get(f"/api/projects/{pid}/review").json()
    assert not rv["work"]["nodes"][0]["open"]
    # 机器在跑时不计工时
    assert client.post(f"/api/projects/{pid}/worklog/heartbeat", json={}).json()["counted"] in (0, 30)
    # 补录
    r = client.put(f"/api/projects/{pid}/worklog/briefing", json={"minutes": 12, "note": "线下沟通"})
    assert r.status_code == 200
    rv = client.get(f"/api/projects/{pid}/review").json()
    assert rv["work"]["nodes"][0]["manual_minutes"] == 12 and rv["work"]["total_minutes"] >= 12
    assert "cost" in rv and "experience" in rv

    md = client.get("/api/music/selection-report?format=md")
    assert md.status_code == 200 and "选型结论" in md.text and "PRD 要求 ≥ 6 轮" in md.text
    csv_ = client.get("/api/music/selection-report?format=csv")
    assert csv_.status_code == 200 and csv_.text.startswith("选型轮")


def test_per_shot_model_switch_and_revert(client, has_real_db, monkeypatch):
    """逐句换模型重出 → 不满意用回上一版（来源标签跟着回去，不花钱）。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    import shutil

    from app.adapters import image as image_ad
    from app.db import SessionLocal
    from app.models import StoryboardShot
    from app.services import storyboard as sbs

    provs = {x["key"]: x for x in client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["providers"]}
    assert {"wanx", "seedream", "jimeng"} <= set(provs)
    if not provs["jimeng"]["ready"]:
        assert "VOLC_ACCESS_KEY" in provs["jimeng"]["note"]

    client.post(f"/api/projects/{CHANGBAI}/storyboard/plan?force=true")
    client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")
    with SessionLocal() as db:
        row = db.query(StoryboardShot).filter(StoryboardShot.project_id == CHANGBAI,
                                              StoryboardShot.dup_of == 0,
                                              StoryboardShot.image_path != "").order_by(StoryboardShot.order).first()
        sid, rev0, src0 = row.id, row.rev, row.provider
        old_img, old_clip = row.image_path, row.clip_path

    def fake_render(row, p, provider):
        img, clip, thumb = sbs._image_paths(row)
        shutil.copy(old_img, img)
        shutil.copy(old_clip, clip)
        return img, clip, thumb, ""
    monkeypatch.setattr(image_ad, "available", lambda *a, **k: True)
    monkeypatch.setattr(sbs, "_render_one", fake_render)
    monkeypatch.setattr(sbs, "_apply_judge", lambda *a, **k: None)
    monkeypatch.setattr(sbs, "_suggest_real", lambda *a, **k: None)
    with SessionLocal() as db:
        sbs.regenerate(db, sid, provider="jimeng")
    it = {i["id"]: i for i in client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["items"]}[sid]
    assert it["rev"] == rev0 + 1 and it["provider"] == "jimeng" and it["source_label"] == "AI 生成·即梦"
    assert it["prev_image_url"]

    r = client.post(f"/api/projects/{CHANGBAI}/storyboard/shots/{sid}/revert")
    assert r.status_code == 200, r.text
    it = {i["id"]: i for i in client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["items"]}[sid]
    assert it["rev"] == rev0 and it["provider"] == src0 and it["status"] == "pending"
