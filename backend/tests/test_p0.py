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
    # 这条合成片没有烧字幕：本用例只核时长/规格/字卡；字幕抽帧 OCR 在真实成片上另行验证
    qc = R.quality_check(job, {**R.VARIANTS["short"], "subs": False}, expect_s=160, short=True,
                         sub_lines=12, need_ai_card=True, ai_card=True)
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


def test_regenerate_is_deduped(client, has_real_db):
    """同一句已有重出任务在排队时，再点不重复入队（E2E：一句被连点 21 次、出了 9 张）。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.db import SessionLocal
    from app.models import Job, StoryboardShot

    with SessionLocal() as db:
        row = db.query(StoryboardShot).filter(StoryboardShot.project_id == CHANGBAI,
                                              StoryboardShot.dup_of == 0).first()
        j = Job(queue="gpu", type="storyboard_generate_one", ref_id=row.id, project_id=CHANGBAI,
                name="重生成一张配图", status="running", payload={})
        db.add(j)
        db.commit()
        sid, jid = row.id, j.id
    try:
        r = client.post(f"/api/projects/{CHANGBAI}/storyboard/shots/{sid}/regenerate?provider=wanx")
        assert r.status_code == 200, r.text
        assert r.json()["duplicate"] is True and r.json()["job_id"] == jid
        it = {i["id"]: i for i in client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["items"]}[sid]
        assert it["regen_active"] is True
    finally:
        with SessionLocal() as db:
            db.query(Job).filter(Job.id == jid).delete()
            db.commit()


def test_intro_trim_shifts_timeline(client, has_real_db, monkeypatch, tmp_path):
    """人声超过 3 秒才出现时裁前奏：时间轴整体前移、音频换成裁过的文件（成片前 3 秒必须有人声）。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.adapters import align
    from app.db import SessionLocal
    from app.services import edit

    def fake_align(path, lines, duration, **k):
        return {"engine": "whisper", "note": "", "vocal_span": [31.5, 180.0],
                "lines": [{"text": t, "start_s": 31.5 + 7 * i, "end_s": 38.0 + 7 * i} for i, t in enumerate(lines)]}
    monkeypatch.setattr(align, "align_lyrics", fake_align)
    monkeypatch.setattr(align, "detect_beats", lambda *a, **k: {"bpm": 84, "beats": [i * 0.714 for i in range(250)],
                                                               "strong_beats": [], "engine": "analytic"})
    monkeypatch.setattr(edit, "_trim_intro", lambda src, s: str(tmp_path / "trimmed.mp3"))
    with SessionLocal() as db:
        tl = edit.analyze(db, CHANGBAI)
        assert tl.align_meta["intro_trim_s"] == 30.0
        assert tl.lyric_lines[0]["start_s"] == 1.5
        assert tl.audio_path.endswith("trimmed.mp3")
        assert tl.beats[0] >= 0


def test_revision_reuses_music_when_lyrics_unchanged(client, has_real_db, monkeypatch):
    """修订版沿用原曲：歌词没改就跳过生曲直接到待选曲；歌词一改就不再沿用。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app import gates
    from app.db import SessionLocal
    from app.models import MusicCandidate, Project
    from app.services import projects as P

    with SessionLocal() as db:
        src = db.get(Project, LIJIANG)
        if not src.selected_candidate_id:
            pytest.skip("原项目没有选定曲目")
        src_audio = db.get(MusicCandidate, src.selected_candidate_id).audio_path
    rev = client.post(f"/api/projects/{LIJIANG}/revise").json()
    monkeypatch.setattr(gates, "check", lambda db, p: gates.Gate())
    with SessionLocal() as db:
        p = db.get(Project, rev["id"])
        c = db.get(MusicCandidate, p.selected_candidate_id)
        assert c is not None and c.audio_path == src_audio and c.cost == 0
        out = P.advance(db, p.id)
        assert out["status"] == "music_review" and out["dispatched"] == "reuse_music"


def test_rhyme_rule_and_llm_error_text():
    """规则押韵按韵母比（儿化取前一字），上游欠费等错误要翻成人话。"""
    from app.adapters.llm import explain_error
    from app.services.prompts import check_fallback, rhyme_key

    assert rhyme_key("云在塔上 停了一会儿") == rhyme_key("天边的云 慢慢地飞")      # 会/飞 都是 ei
    assert rhyme_key("山脊上的光 落在肩上") == rhyme_key("檐角的铜铃 还在响")       # 上/响 都是 ang
    assert rhyme_key("灯火把夜晚 烫出一个洞") == rhyme_key("风吹过 山顶")            # ong/ing 同辙
    secs = [{"name": "主歌1", "lines": ["一行", "山脊上的光 落在肩上", "三行", "檐角的铜铃 还在响"]},
            {"name": "副歌", "lines": ["月光照在 河面上", "我们一起 慢慢唱"]}]
    assert not [i for i in check_fallback(secs, "x")["issues"] if i["type"] == "rhyme"]

    msg = explain_error(400, '{"error":{"code":"Arrearage","message":"Access denied"}}')
    assert "欠费" in msg and "Arrearage" in msg


def test_mock_lyrics_blocked_and_repeats_merged(client, has_real_db, monkeypatch):
    """LLM 已配置却失败 → 模板歌词标 mock、闸门拦住；副歌复唱同一句只出一条检查。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app import gates
    from app.config import settings
    from app.db import SessionLocal
    from app.models import LyricCheck, LyricVersion, Project
    from app.services import lyrics as L

    with SessionLocal() as db:
        p = Project(name="测试·模板歌词", destination="杭州·西湖", status="lyric_review")
        db.add(p)
        db.flush()
        ver = LyricVersion(project_id=p.id, version=1, source="mock", checks_done=True, sections=[
            {"name": "副歌", "lines": ["把杭州西湖的风 装进我的口袋还要更长一点", "第二行是正常长度的歌词"]},
            {"name": "尾副歌", "lines": ["把杭州西湖的风 装进我的口袋还要更长一点"]}])
        db.add(ver)
        db.commit()
        monkeypatch.setattr(type(settings()), "llm_ready", property(lambda s: True))
        g = gates.check(db, p)
        assert not g.ok and any("模板占位" in r for r in g.reasons)

        monkeypatch.setattr(L.llm, "json_chat", lambda *a, **k: ({"issues": [], "items": []}, True))
        L.run_checks(db, ver)
        meter = [c for c in db.query(LyricCheck).filter(LyricCheck.lyric_version_id == ver.id) if c.type == "meter"]
        assert len(meter) == 1 and "处理一次全部生效" in meter[0].evidence
        db.query(LyricCheck).filter(LyricCheck.lyric_version_id == ver.id).delete()
        db.delete(ver)
        db.delete(p)
        db.commit()


def test_align_failure_is_not_silent(monkeypatch):
    """F3-1：对齐失败抛错、不重试；只有人明确选择时才按字数估算，并如实标注。"""
    from app.adapters import align
    from app.config import settings

    monkeypatch.setattr(type(settings()), "align_enabled", property(lambda s: True))

    def boom(*a, **k):
        raise RuntimeError("模型文件缺失")
    monkeypatch.setattr(align, "_whisper_align", boom)
    with pytest.raises(align.AlignError) as ei:
        align.align_lyrics("x.mp3", ["第一句歌词", "第二句歌词"], 120.0)
    assert ei.value.no_retry and "模型文件缺失" in str(ei.value)

    out = align.align_lyrics("x.mp3", ["第一句歌词", "第二句歌词"], 120.0, allow_estimate=True)
    assert out["engine"] == "analytic" and "人工选择" in out["note"] and len(out["lines"]) == 2


def test_static_ai_shot_split_and_beat_snap():
    """F3-2：AI 静态画面单段 ≤ MAX_STATIC_S（拆段取不同景别），切点吸附最近强拍。"""
    from types import SimpleNamespace

    from app.adapters import media
    from app.services import edit

    # 运镜参数必须成对：预放大要盖得住最大放大倍率，否则 zoom 越大画面越糊
    assert media.PRE_SCALE >= 1 + media.PUSH_AMOUNT, "PRE_SCALE 必须 >= 1 + PUSH_AMOUNT"
    # 幅度要够大，否则不同时间窗之间看不出景别差（旧值 0.10 就是这个 bug）
    assert media.PUSH_AMOUNT >= 0.30

    ai = SimpleNamespace(source="ai", duration_s=20.0)
    beats = [i * 1.0 for i in range(20)]           # 每 1 秒一个强拍（拆分点离强拍都 ≤ 0.5 秒）
    parts = edit._split_static({"asset": ai, "start": 0.0, "end": 19.0}, (beats, beats))
    assert len(parts) == 5
    assert all(p["end"] - p["start"] <= edit.MAX_STATIC_S + 0.5 for p in parts)
    assert all(p["start"] in beats for p in parts[1:])        # 拆分点落在强拍上
    assert parts[0]["window"] == 0.0 and parts[-1]["window"] == 1.0

    shot = SimpleNamespace(source="shot", duration_s=20.0)
    assert len(edit._split_static({"asset": shot, "start": 0.0, "end": 19.0}, (beats, beats))) == 1
    # 短句不拆：4 秒以内保持单镜，拆了会变成 1 秒一闪
    assert len(edit._split_static({"asset": ai, "start": 0.0, "end": 3.2}, (beats, beats))) == 1
    # 切点被吸附到同一强拍时不能产出 0 秒片段
    flat = [0.0, 4.0]                              # 强拍只有两个，5 个切点会挤在一起
    safe = edit._split_static({"asset": ai, "start": 0.0, "end": 19.0}, (flat, flat))
    assert all(p["end"] - p["start"] > 0 for p in safe)
    assert sum(p["end"] - p["start"] for p in safe) == 19.0
    # 强拍太远时退到普通拍
    assert edit._snap(([0.0, 4.0], [0.0, 1.0, 2.0, 3.0, 4.0]), 2.2) == 2.0
    assert edit._snap(([0.0, 4.0], [0.0, 1.0, 2.0, 3.0, 4.0]), 3.7) == 4.0
    assert edit._nearest([0.0, 2.0, 4.0], 2.7, 0.5) is None and edit._nearest([0.0, 2.0, 4.0], 2.4, 0.5) == 2.0


def test_clip_score_calibration_and_comparability(monkeypatch):
    """向量：不同编码器的向量不比较；匹配分按实测区间标定。"""
    from app.adapters import clip as C

    monkeypatch.setattr(type(C.encoder), "is_real", property(lambda s: True))
    monkeypatch.setattr(type(C.encoder), "model_id", property(lambda s: "clip:x"))
    assert C.match_score(0.47) > 0.9 and C.match_score(0.31) < 0.1 and 0.55 < C.match_score(0.41) < 0.65
    assert C.comparable("clip:x") and not C.comparable("hash") and not C.comparable("")


def test_revision_carries_keep_decisions(client, has_real_db):
    """修订版沿用原项目「保留原文」的决定：按类型 + 原句匹配，段落/行号变了也能对上。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app.db import SessionLocal
    from app.models import LyricCheck, LyricVersion, Project
    from app.services import lyrics as L

    with SessionLocal() as db:
        src = Project(name="测试·原项目", destination="x", status="delivered")
        rev = Project(name="测试·原项目（修订1）", destination="x", status="lyric_review")
        db.add_all([src, rev])
        db.flush()
        v0 = LyricVersion(project_id=src.id, version=3, sections=[], source="ai", checks_done=True)
        v1 = LyricVersion(project_id=rev.id, version=1, sections=[], source="manual", checks_done=True)
        db.add_all([v0, v1])
        db.flush()
        db.add_all([
            LyricCheck(lyric_version_id=v0.id, project_id=src.id, type="fact", section="主歌1", line_no=2,
                       text="千年火口沉静卧，", status="kept", keep_reason="已查证属实"),
            LyricCheck(lyric_version_id=v1.id, project_id=rev.id, type="fact", section="主歌2", line_no=4,
                       text="千年火口沉静卧", status="pending"),
            LyricCheck(lyric_version_id=v1.id, project_id=rev.id, type="rhyme", section="主歌2", line_no=4,
                       text="千年火口沉静卧", status="pending"),
        ])
        db.commit()
        assert L._carry_decisions(db, v0, v1, prefix="沿用原项目的决定：") == 1
        rows = {c.type: c for c in L.checks_of(db, v1.id)}
        assert rows["fact"].status == "kept" and rows["fact"].keep_reason == "沿用原项目的决定：已查证属实"
        assert rows["rhyme"].status == "pending"        # 原项目没处理过押韵：不能替人拍板
        for t in (LyricCheck, LyricVersion):
            db.query(t).filter(t.project_id.in_([src.id, rev.id])).delete()
        db.query(Project).filter(Project.id.in_([src.id, rev.id])).delete()
        db.commit()


def test_event_fingerprint_changes_on_state_change(client):
    """F1-2 SSE：项目状态一变，变更指纹就变（看板据此 1 秒内刷新）。"""
    import time

    from app.api.events import fingerprint
    from app.db import SessionLocal
    from app.models import Project

    before = fingerprint()
    time.sleep(0.01)
    with SessionLocal() as db:
        p = Project(name="测试·推送", destination="x", status="briefing")
        db.add(p)
        db.commit()
        pid = p.id
    after = fingerprint()
    assert after != before
    with SessionLocal() as db:
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()


def test_audit_fixes(client, has_real_db, monkeypatch):
    """自查修复：素材地址保留子目录、已交付项目不能重跑歌词检查、登录防爆破、驳回目标有中文名。"""
    from app import auth
    from app.serialize import asset_url

    assert asset_url(r"C:\data\assets\storyboard\sb-x-0.mp4") == "/media/assets/storyboard/sb-x-0.mp4"
    assert asset_url("/srv/data/assets/a.mp4") == "/media/assets/a.mp4" and asset_url("") == ""

    if has_real_db:
        assert client.post(f"/api/projects/{BEIJING}/lyrics/check").status_code == 400
        assert client.post(f"/api/projects/{BEIJING}/music/regenerate", json={}).status_code == 400
        p = client.get(f"/api/projects/{CHANGBAI}").json()["project"]
        assert all(p["reject_target_labels"].get(k) for k in p["reject_targets"])

    monkeypatch.setattr(auth.settings(), "access_password", "s3cret")
    monkeypatch.setattr(auth.settings(), "session_secret", "wenlv:s3cret")
    auth._FAILS.clear()
    codes = [client.post("/api/auth/login", json={"password": "bad"}).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5] == 429, codes
    auth._FAILS.clear()
    assert client.post("/api/auth/login", json={"password": "s3cret"}).status_code == 200
    auth._FAILS.clear()


def test_controlled_vocabulary(client, has_real_db):
    """F4-12 受控词表：近义词归并、整句丢弃、景点截成短名；人工加标签只能选词表；存量清洗把提示词搬走。"""
    from app import vocab
    from app.db import SessionLocal
    from app.models import Asset, AssetTag
    from app.services import assets as A

    assert vocab.normalize("time", "傍晚") == "黄昏" and vocab.normalize("mood", "静谧") == "宁静"
    assert vocab.normalize("mood", "写实纪实摄影，雪原延时") is None
    assert vocab.normalize("season", "秋") == "秋" and vocab.normalize("weather", "龙卷风") is None
    assert vocab.clean_place("长白山天池（北坡天文峰观景台）") == "长白山天池"
    assert vocab.normalize_many("camera", ["航拍", "鸟瞰", "乱写"]) == ["航拍"]
    with pytest.raises(ValueError):
        vocab.add_value("mood", "这是一整句话不是词")

    with SessionLocal() as db:
        a = Asset(name="测试·词表", file_path="", destination="测试", source="ai", licensor="AI 生成（通义万相）")
        db.add(a)
        db.flush()
        db.add_all([
            AssetTag(asset_id=a.id, dim="subject", value="航拍雪线以上裸露浮石山脊，强风卷雪掠过尖锐玄武岩棱角", source="ai"),
            AssetTag(asset_id=a.id, dim="mood", value="静谧", source="ai"),
            AssetTag(asset_id=a.id, dim="mood", value="宁静", source="ai"),
            AssetTag(asset_id=a.id, dim="mood", value="cinematic", source="ai"),
            AssetTag(asset_id=a.id, dim="place", value="天池（北坡）", source="ai"),
        ])
        db.commit()
        aid = a.id
        A.clean_tags(db)
        db.refresh(a)
        tags = sorted((t.dim, t.value) for t in db.query(AssetTag).filter(AssetTag.asset_id == aid))
        assert tags == [("mood", "宁静"), ("place", "天池")]
        assert a.gen_prompt.startswith("航拍雪线") and a.gen_model == "通义万相"
        assert A.clean_tags(db) == {"normalized": 0, "dropped": 0, "prompts_moved": 0}     # 幂等

    r = client.put(f"/api/assets/{aid}", json={"add_tags": [{"dim": "weather", "value": "乱七八糟的天气"}]})
    assert r.status_code == 400 and "不在词表" in r.json()["detail"]
    assert client.put(f"/api/assets/{aid}", json={"add_tags": [{"dim": "weather", "value": "下雪"}]}).status_code == 200
    f = client.get("/api/assets/facets").json()
    assert all(v in f["vocab"][d] for d in vocab.CONTROLLED for v in f["facets"]["tags"][d])
    assert all(len(v) <= vocab.PLACE_MAX for v in f["facets"]["tags"]["place"])
    with SessionLocal() as db:
        db.query(AssetTag).filter(AssetTag.asset_id == aid).delete()
        db.query(Asset).filter(Asset.id == aid).delete()
        db.commit()


def test_only_fact_checks_block(client, has_real_db):
    """只有史实疑点拦确认；押韵 / 字数是建议。自相矛盾的史实疑点丢弃；史实可一键全部保留。"""
    from app import gates
    from app.db import SessionLocal
    from app.models import LyricCheck, LyricVersion, Project
    from app.services import lyrics as L

    assert L._self_contradictory("曲院风荷旁冷饮摊沁出凉香", "名称错误，应为‘曲院风荷’，属字形错误")
    assert not L._self_contradictory("雷锋塔影斜斜印在裙边", "应为‘雷峰塔’，‘锋’字错误")

    with SessionLocal() as db:
        p = Project(name="测试·检查闸门", destination="x", status="lyric_review")
        db.add(p)
        db.flush()
        v = LyricVersion(project_id=p.id, version=1, sections=[], source="ai", checks_done=True)
        db.add(v)
        db.flush()
        mk = lambda t, i: LyricCheck(lyric_version_id=v.id, project_id=p.id, type=t, section="主歌1",  # noqa: E731
                                     line_no=i, text=f"句子{i}", status="pending")
        db.add_all([mk("rhyme", 1), mk("meter", 2)])
        db.commit()
        pid = p.id
        assert gates.check(db, p).ok                      # 只有建议：不拦
        db.add_all([mk("fact", 3), mk("fact", 4)])
        db.commit()
        g = gates.check(db, p)
        assert not g.ok and "2 条史实疑点" in g.reasons[0]
        s = L.check_summary(L.checks_of(db, v.id), v)
        assert s["blocking"] == 2 and s["pending"] == 4 and s["by_type"]["rhyme"]["blocking"] is False

    assert client.post(f"/api/projects/{pid}/lyrics/checks/keep-all", json={"reason": ""}).status_code == 400
    assert client.post(f"/api/projects/{pid}/lyrics/checks/keep-all", json={"reason": "已逐条查证"}).json() == {"kept": 2}
    with SessionLocal() as db:
        assert gates.check(db, db.get(Project, pid)).ok
        for t in (LyricCheck, LyricVersion):
            db.query(t).filter(t.project_id == pid).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()


def test_incremental_checks_no_whack_a_mole(client, has_real_db, monkeypatch):
    """改一句只查这一句：没动的句子沿用上一版结果，采纳的建议不回头复查（修复「处理一条冒一条」）。"""
    from app.db import SessionLocal
    from app.models import LyricCheck, LyricVersion, Project
    from app.services import lyrics as L

    calls = {"n": 0}

    def noisy(messages, **kw):
        """模拟不稳定的模型：每次都对所有句子报史实疑点。"""
        calls["n"] += 1
        body = messages[-1]["content"]
        issues = [{"type": "fact", "section": "主歌1", "line_no": i, "text": "", "evidence": f"第{calls['n']}次觉得可疑",
                   "suggestions": ["这是系统给出的改写句子"]} for i in (1, 2, 3) if f"{i}. " in body]
        return {"issues": issues, "items": []}, True
    monkeypatch.setattr(L.llm, "json_chat", noisy)

    with SessionLocal() as db:
        p = Project(name="测试·增量检查", destination="x", status="lyric_review")
        db.add(p)
        db.commit()
        pid = p.id
        v1 = L.save_version(db, pid, [{"name": "主歌1", "lines": ["第一句歌词写在这里", "第二句歌词写在这里", "第三句歌词写在这里"]}])
        rows = L.checks_of(db, v1.id)
        assert len(rows) == 3 and calls["n"] == 1
        L.keep_original(db, rows[0].id, "已查证")
        L.keep_original(db, rows[1].id, "已查证")

        # 采纳第三条的建议：不再调模型，前两条的保留决定原样带过来，新句子不被复查
        v2 = L.adopt_suggestion(db, rows[2].id, 0)
        r2 = L.checks_of(db, v2.id)
        assert calls["n"] == 1, "采纳建议后不应再整首重查"
        assert sorted((c.text, c.status) for c in r2) == [("第一句歌词写在这里", "kept"), ("第二句歌词写在这里", "kept")]

        # 手动改第二句：只查这一句（模型虽然对三句都报，但只留改动句的）
        v3 = L.save_version(db, pid, [{"name": "主歌1", "lines": ["第一句歌词写在这里", "手动改过的第二句话", "这是系统给出的改写句子"]}],
                            carry_from=v2)
        r3 = {c.text: c.status for c in L.checks_of(db, v3.id)}
        assert r3 == {"第一句歌词写在这里": "kept", "手动改过的第二句话": "pending"} and calls["n"] == 2

        # 同一版本「重新检查」：已保留的决定不被冲掉
        L.run_checks(db, v3)
        assert {c.text: c.status for c in L.checks_of(db, v3.id)}["第一句歌词写在这里"] == "kept"

        for t in (LyricCheck, LyricVersion):
            db.query(t).filter(t.project_id == pid).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()


def test_short_video_cut_windows():
    """高光 30 秒从副歌起、前 3 秒有人声；精华 60 秒以副歌结束为终点；结尾都落在句末。"""
    from types import SimpleNamespace

    from app.services import render as R

    def L(sec, s, e):
        return {"section": sec, "start_s": s, "end_s": e, "text": "x"}
    lines = ([L("主歌1", 1.5 + 7 * i, 8.0 + 7 * i) for i in range(4)]            # 1.5–29
             + [L("预副歌", 30 + 5 * i, 34.5 + 5 * i) for i in range(2)]         # 30–39.5
             + [L("副歌", 41 + 7 * i, 47.5 + 7 * i) for i in range(4)]           # 41–68.5
             + [L("主歌2", 72 + 7 * i, 78.5 + 7 * i) for i in range(4)]
             + [L("尾副歌", 110 + 7 * i, 116.5 + 7 * i) for i in range(4)])
    tl = SimpleNamespace(duration_s=160.0, lyric_lines=lines)

    off, span = R.cut_window(tl, 30)
    assert abs(off - (41 - R.CUT_LEAD_S)) < 0.01                    # 从第一段副歌起，人声 0.8 秒处进
    assert abs(span - 30) <= R.CUT_TOLERANCE_S + 0.5
    assert any(abs(off + span - 0.4 - l["end_s"]) < 0.01 for l in lines)      # 结尾落在句末

    off, span = R.cut_window(tl, 60)
    assert off < 41 and abs(off + span - 0.4 - 68.5) < 0.01          # 以第一段副歌结束（68.5）为终点
    assert abs(span - 60) <= R.CUT_TOLERANCE_S + 0.5
    assert any(abs(off + R.CUT_LEAD_S - l["start_s"]) < 0.01 for l in lines) or off == 0.0

    short_song = SimpleNamespace(duration_s=25.0, lyric_lines=lines[:3])
    assert R.cut_window(short_song, 30) == (0.0, 25.0)               # 歌比目标短：用整首
    assert set(R.VARIANTS) >= {"highlight30", "essence60"}


def test_song_default_and_structure(client):
    """默认时长 120 秒；歌词段落与行数随时长变化（V1.0 一律写满六段，所以每首都近 3 分钟）。"""
    from app.config import settings
    from app.services.prompts import lyrics_messages, song_structure

    assert settings().song_default_seconds == 120
    assert song_structure(120) == ([("主歌1", 4), ("副歌", 4), ("主歌2", 4), ("尾副歌", 2)], 14)
    assert song_structure(90)[1] == 10 and song_structure(180)[1] == 19
    assert [n for n, _ in song_structure(180)[0]] == ["主歌1", "预副歌", "副歌", "主歌2", "桥段", "尾副歌"]
    sys = lyrics_messages("杭州·西湖", "流行", ["治愈"], 120, "")[0]["content"]
    assert "共 14 行" in sys and "桥段" not in sys

    r = client.post("/api/projects", json={"name": "测试·默认时长", "destination": "测试"})
    assert r.status_code == 200
    assert client.get(f"/api/projects/{r.json()['id']}").json()["brief"]["duration_s"] == 120


def test_token_pricing_and_cost_totals(client, has_real_db):
    """记账：按真实 token × 价格表计费；未知模型标明单价未配置；项目累计费用含全部环节。"""
    from app import pricing
    from app.db import SessionLocal
    from app.models import AiCall, MusicCandidate, Project
    from app.observability import record

    cost, known = pricing.token_cost("qwen-plus", 4000, 4800)
    assert known and abs(cost - 0.0128) < 1e-6
    assert pricing.token_cost("qwen-plus-2025-07-28", 1_000_000, 0) == (0.8, True)     # 快照名回落到主名
    assert pricing.token_cost("没见过的模型", 1000, 1000) == (0.0, False)

    with SessionLocal() as db:
        p = Project(name="测试·记账", destination="x", status="edit_review")
        db.add(p)
        db.flush()
        db.add(MusicCandidate(project_id=p.id, model_key="mureka", model_name="Mureka", status="done",
                              cost=0.324, is_mock=False, label="A"))
        db.commit()
        pid = p.id
    record(kind="llm", provider="x", model="qwen-plus", purpose="t", cost=0.0128, input_tokens=4000,
           output_tokens=4800, project_id=pid)
    record(kind="image", provider="seedream", model="m", purpose="t", cost=0.21, project_id=pid)
    d = client.get(f"/api/projects/{pid}/calls").json()
    assert abs(d["project_total_cost"] - (0.0128 + 0.21 + 0.324)) < 1e-6     # 以前只有生曲的 0.324
    assert d["by_kind"]["llm"]["input_tokens"] == 4000 and d["by_kind"]["music"]["cost"] == 0.324
    assert {m["kind"] for m in d["by_model"]} == {"llm", "image", "music"}
    with SessionLocal() as db:
        db.query(AiCall).filter(AiCall.project_id == pid).delete()
        db.query(MusicCandidate).filter(MusicCandidate.project_id == pid).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()


def test_bench_rating_summary_and_export(client):
    """对比台：一轮只能有一个最佳；汇总给出均价、均分与每元得分；可导出。"""
    from app.db import SessionLocal
    from app.models import BenchResult, BenchRun
    from app.services import bench as B

    with SessionLocal() as db:
        run = BenchRun(kind="image", title="断桥柳影", destination="杭州·西湖", input={"prompt": "断桥"})
        db.add(run)
        db.flush()
        a = BenchResult(run_id=run.id, kind="image", provider="wanx", model="wanx2.1-t2i-plus", status="done",
                        cost=0.20, elapsed_s=30, ai_score=80)
        b = BenchResult(run_id=run.id, kind="image", provider="seedream", model="seedream-5", status="done",
                        cost=0.21, elapsed_s=12, ai_score=90)
        db.add_all([a, b])
        db.commit()
        rid, aid, bid = run.id, a.id, b.id
    assert client.put(f"/api/bench/results/{aid}", json={"score": 3, "picked": True}).status_code == 200
    r = client.put(f"/api/bench/results/{bid}", json={"score": 5, "picked": True}).json()
    assert r["picked"] and r["human_score"] == 5
    d = client.get(f"/api/bench/runs/{rid}").json()
    assert [x["picked"] for x in d["results"]] == [False, True]          # 改选后上一个取消
    assert client.put(f"/api/bench/results/{aid}", json={"score": 9}).status_code == 400
    s = {x["key"]: x for x in client.get("/api/bench/summary").json()["image"]}
    assert s["seedream"]["avg_human"] == 5 and s["seedream"]["picked"] == 1
    assert s["seedream"]["score_per_yuan"] == round(5 / 0.21, 1) and s["wanx"]["score_per_yuan"] == 15.0
    csv_text = client.get("/api/bench/export").text
    assert "断桥柳影" in csv_text and "seedream-5" in csv_text
    assert "image" in client.get("/api/bench/options").json()
    with SessionLocal() as db:
        db.query(BenchResult).filter(BenchResult.run_id == rid).delete()
        db.query(BenchRun).filter(BenchRun.id == rid).delete()
        db.commit()


def test_distribute_plan_and_copy(client, tmp_path, monkeypatch):
    """一键分发：按渠道规则复制、规范命名、附说明；未交付不分发；自检未过的版本不分发。"""
    from datetime import datetime, timezone

    from app.adapters import media
    from app.db import SessionLocal
    from app.models import DistributeLog, Project, RenderJob
    from app.services import distribute as D

    root = tmp_path / "数字资产"
    root.mkdir()
    monkeypatch.setenv("DISTRIBUTE_ROOT", str(root))
    monkeypatch.setattr(D, "_cfg_path", lambda: tmp_path / "distribute.json")
    clip = tmp_path / "a.mp4"
    media.generate_test_clip(clip, seconds=3, width=320, height=180, label="x")

    with SessionLocal() as db:
        p = Project(name="测试·分发", destination="杭州·西湖", status="rendering")
        db.add(p)
        db.flush()
        for v, qc in (("sub_zh_en", True), ("highlight30", True), ("essence60", False)):
            db.add(RenderJob(project_id=p.id, variant=v, status="done", output_path=str(clip),
                             qc={"ok": qc, "checks": []}))
        db.commit()
        pid = p.id

    assert client.post(f"/api/distribute/projects/{pid}", json={}).status_code == 400      # 未交付
    with SessionLocal() as db:
        db.get(Project, pid).locked_at = datetime.now(timezone.utc)
        db.commit()
    plan = client.get(f"/api/distribute/projects/{pid}").json()
    yt = next(c for c in plan["channels"] if c["key"] == "youtube")
    assert {f["variant"] for f in yt["files"]} == {"sub_zh_en", "highlight30", "essence60"}
    assert "portrait" in plan["missing_variants"] and yt["blocked"] == ["精华版"]

    out = client.post(f"/api/distribute/projects/{pid}", json={"channels": ["youtube", "douyin"]}).json()
    assert {c["key"] for c in out["channels"]} == {"youtube", "douyin"}
    day = datetime.now().strftime("%Y%m%d")
    folder = root / "youtube" / f"杭州西湖_{day}"
    names = sorted(x.name for x in folder.iterdir())
    assert f"杭州西湖_YouTube_横版中英字幕_3s_{day}.mp4" in names and "说明.md" in names and "分发清单.json" in names
    assert not any("精华版" in n for n in names)                    # 自检没过的不分发
    assert "YouTube" in (folder / "说明.md").read_text(encoding="utf-8")
    assert (root / "抖音" / f"杭州西湖_{day}").is_dir() and not (root / "小红书").exists()
    with SessionLocal() as db:
        db.query(DistributeLog).filter(DistributeLog.project_id == pid).delete()
        db.query(RenderJob).filter(RenderJob.project_id == pid).delete()
        db.query(Project).filter(Project.id == pid).delete()
        db.commit()


def test_repeat_line_follows_master_status(client, has_real_db):
    """副歌复唱句以母句当前状态为准：被过期状态写回「待审核」也会自动校正，闸门按实际要审的句数算。"""
    if not has_real_db:
        pytest.skip("没有真实库备份")
    from app import gates
    from app.db import SessionLocal
    from app.models import Project, StoryboardShot

    client.post(f"/api/projects/{CHANGBAI}/storyboard/plan?force=true")
    client.post(f"/api/projects/{CHANGBAI}/storyboard/generate")
    client.post(f"/api/projects/{CHANGBAI}/storyboard/approve-all")
    with SessionLocal() as db:
        rows = db.query(StoryboardShot).filter(StoryboardShot.project_id == CHANGBAI).order_by(StoryboardShot.order).all()
        dups = [r for r in rows if r.dup_of]
        if not dups:                        # 这份歌词没有复唱句：把最后一句造成第一句的复唱
            rows[-1].dup_of = rows[0].order
            dups = [rows[-1]]
        dups[0].status = "pending"          # 模拟预审任务用过期状态把复唱句写回待审核
        db.commit()
        did = dups[0].id
    prog = client.get(f"/api/projects/{CHANGBAI}/storyboard").json()["progress"]
    assert prog["unique_approved"] == prog["unique"] and prog["pending"] == 0
    with SessionLocal() as db:
        assert db.get(StoryboardShot, did).status == "approved"       # 已被校正
        p = db.get(Project, CHANGBAI)
        p.status = "storyboard_review"
        db.commit()
        assert not any("未通过审核" in r for r in gates.check(db, p).reasons)
