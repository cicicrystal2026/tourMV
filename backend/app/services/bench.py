"""模型对比台：同一个输入交给多个模型，并排比较效果、费用与用时。

- 生图对比：同一句歌词 / 同一段画面描述 → 多个生图模型各出一张，记录单价、用时、机器预审分；
- 写词对比：同一份简报 → 多个文字模型各写一首，记录 token、费用、用时、行数与字数越界数；
- 生曲对比已经有了（词曲工作台的选型轮盲听），这里只汇总它的战绩。

人工在每个结果上打 1–5 分并选出最佳；汇总页按模型给出均分、均价、胜出次数与「每元得分」，
用来挑「效果最好且最划算」的组合。
"""
from __future__ import annotations

import csv
import io
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import image as image_ad
from ..adapters import media, vlm
from ..adapters.llm import llm
from ..config import settings
from ..models import BenchResult, BenchRun, MusicModel
from ..queue import enqueue
from . import budget, prompts
from .lyrics import MAX_LEN, MIN_LEN, clean_len
from .projects import ProjectError

DEFAULT_LYRIC_MODELS = "qwen-plus,qwen-max,qwen-turbo,qwen-flash"


def lyric_models() -> list[str]:
    raw = os.environ.get("LLM_BENCH_MODELS", DEFAULT_LYRIC_MODELS)
    out = [m.strip() for m in raw.split(",") if m.strip()]
    cur = settings().llm_model
    return list(dict.fromkeys([cur, *out]))


def options(db: Session) -> dict:
    from .. import pricing

    return {
        "image": [{"key": p["key"], "label": p["label"], "model": p["model"], "size": p["size"],
                   "ready": p["ready"], "price": p["price"], "note": p["note"]}
                  for p in image_ad.list_providers()],
        "lyric": [{"model": m, "price": pricing.lookup(m), "current": m == settings().llm_model}
                  for m in lyric_models()],
        "llm_ready": llm.ready,
        "prices": pricing.describe(),
    }


# ------------------------------------------------------------------ 发起
def start_image(db: Session, *, destination: str, place: str, line: str, prompt: str,
                providers: list[str], title: str = "") -> BenchRun:
    place, line = (place or "").strip(), (line or "").strip()
    prompt = (prompt or "").strip() or "，".join(x for x in (place, destination, line) if x)
    if not prompt:
        raise ProjectError("请填写景点、歌词或画面描述")
    keys = [k for k in dict.fromkeys(providers or []) if k in image_ad.PROVIDERS]
    not_ready = [image_ad.PROVIDERS[k]["label"] for k in keys if not image_ad.available(k)]
    if not_ready:
        raise ProjectError(f"{'、'.join(not_ready)} 未配置密钥，不能参加对比")
    if len(keys) < 1:
        raise ProjectError("至少选 1 个生图模型")
    budget.ensure_budget(db, sum(image_ad.price(k) + 0.004 for k in keys), what=f"{len(keys)} 个模型各出 1 张")
    run = BenchRun(kind="image", title=(title or line or place or prompt)[:200], destination=destination or "",
                   input={"place": place, "line": line, "prompt": prompt})
    db.add(run)
    db.flush()
    for k in keys:
        r = BenchResult(run_id=run.id, kind="image", provider=k, model=image_ad.model_name(k))
        db.add(r)
        db.flush()
        enqueue(db, "bench_image_one", queue="api", ref_id=r.id,
                name=f"对比出图 · {image_ad.PROVIDERS[k]['label']}")
    db.commit()
    return run


def start_lyric(db: Session, *, destination: str, genre: str, mood: list[str], duration_s: int,
                style_prompt: str, models: list[str], title: str = "") -> BenchRun:
    if not llm.ready:
        raise ProjectError("文字模型未配置（LLM_BASE_URL / LLM_API_KEY）")
    if not (destination or "").strip():
        raise ProjectError("请填写目的地")
    names = [m.strip() for m in dict.fromkeys(models or []) if m and m.strip()]
    if not names:
        raise ProjectError("至少选 1 个写词模型")
    budget.ensure_budget(db, 0.02 * len(names), what=f"{len(names)} 个模型各写 1 首")
    run = BenchRun(kind="lyric", title=(title or f"{destination} · {genre or '写词对比'}")[:200],
                   destination=destination,
                   input={"genre": genre or "", "mood": list(mood or []),
                          "duration_s": int(duration_s or settings().song_default_seconds),
                          "style_prompt": style_prompt or ""})
    db.add(run)
    db.flush()
    for m in names:
        r = BenchResult(run_id=run.id, kind="lyric", provider="llm", model=m)
        db.add(r)
        db.flush()
        enqueue(db, "bench_lyric_one", queue="api", ref_id=r.id, name=f"对比写词 · {m}")
    db.commit()
    return run


# ------------------------------------------------------------------ 执行（由后台任务调用）
def run_image_one(db: Session, result_id: str) -> None:
    import time

    r = db.get(BenchResult, result_id)
    run = db.get(BenchRun, r.run_id) if r else None
    if r is None or run is None:
        return
    r.status = "running"
    db.commit()
    d = settings().data_dir / "storyboard"
    raw = d / "_raw" / f"bench-{r.id}.png"
    img = d / f"bench-{r.id}.jpg"
    raw.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        image_ad.text_to_image(run.input.get("prompt", ""), raw, provider=r.provider, attempts=2)
        media.to_jpeg(raw, img)
        info = _image_size(img)
        r.elapsed_s = round(time.time() - t0, 1)
        r.cost = image_ad.price(r.provider)
        r.output = {"image_path": str(img), **info}
        j = vlm.judge_image(str(img), run.input.get("line", ""), run.input.get("place", ""), run.destination)
        if j:
            r.ai_score, r.ai_reason = int(j["score"]), j.get("reason", "")
            r.cost = round(r.cost + vlm.last_usage().get("cost", 0.0), 6)
        r.status = "done"
    except Exception as e:  # noqa: BLE001
        r.elapsed_s = round(time.time() - t0, 1)
        r.status, r.error = "failed", str(e)[:600]
    db.commit()


def _image_size(path: Path) -> dict:
    try:
        from PIL import Image

        with Image.open(path) as im:
            return {"width": im.size[0], "height": im.size[1]}
    except Exception:  # noqa: BLE001
        return {}


def run_lyric_one(db: Session, result_id: str) -> None:
    import json

    from ..adapters.llm import _strip_fence

    r = db.get(BenchResult, result_id)
    run = db.get(BenchRun, r.run_id) if r else None
    if r is None or run is None:
        return
    r.status = "running"
    db.commit()
    inp = run.input or {}
    try:
        out = llm.chat_raw(
            prompts.lyrics_messages(run.destination, inp.get("genre", ""), inp.get("mood") or [],
                                    int(inp.get("duration_s") or 120), inp.get("style_prompt", "")),
            json_mode=True, purpose="bench_lyrics", model=r.model)
        sections = (json.loads(_strip_fence(out["content"])) or {}).get("sections") or []
        lines = [ln for s in sections for ln in (s.get("lines") or [])]
        if not lines:
            raise RuntimeError("模型没有返回歌词")
        want = prompts.song_structure(int(inp.get("duration_s") or 120))[1]
        r.output = {
            "sections": sections, "lines": len(lines), "want_lines": want,
            # 规则可算的客观指标：字数越界的行数、与要求行数的偏差、有没有把「地名·地名」整串塞进歌词
            "meter_bad": sum(1 for ln in lines if not MIN_LEN <= clean_len(ln) <= MAX_LEN),
            "has_dot_name": any("·" in ln for ln in lines),
        }
        r.input_tokens, r.output_tokens = out["input_tokens"], out["output_tokens"]
        r.cost, r.price_known, r.elapsed_s = out["cost"], out["price_known"], out["elapsed_s"]
        r.status = "done"
    except Exception as e:  # noqa: BLE001
        r.status, r.error = "failed", str(e)[:600]
    db.commit()


# ------------------------------------------------------------------ 查询与评分
def _result(r: BenchResult) -> dict:
    from ..serialize import dt, media_url

    out = dict(r.output or {})
    if out.get("image_path"):
        out["image_url"] = media_url("storyboard", out.pop("image_path"))
    label = image_ad.PROVIDERS.get(r.provider, {}).get("label") if r.kind == "image" else None
    return {"id": r.id, "run_id": r.run_id, "kind": r.kind, "provider": r.provider, "model": r.model,
            "label": label or r.model, "status": r.status, "error": r.error, "output": out,
            "cost": r.cost, "price_known": bool(r.price_known), "elapsed_s": r.elapsed_s,
            "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
            "ai_score": None if r.ai_score < 0 else r.ai_score, "ai_reason": r.ai_reason,
            "human_score": r.human_score, "picked": bool(r.picked), "note": r.note,
            "created_at": dt(r.created_at)}


def run_payload(db: Session, run: BenchRun) -> dict:
    from ..serialize import dt

    rows = list(db.execute(select(BenchResult).where(BenchResult.run_id == run.id)
                           .order_by(BenchResult.created_at)).scalars())
    return {"id": run.id, "kind": run.kind, "title": run.title, "destination": run.destination,
            "input": run.input or {}, "created_at": dt(run.created_at),
            "busy": any(r.status in ("queued", "running") for r in rows),
            "results": [_result(r) for r in rows]}


def list_runs(db: Session, kind: str = "", limit: int = 30) -> list[dict]:
    q = select(BenchRun).order_by(BenchRun.created_at.desc()).limit(limit)
    if kind:
        q = q.where(BenchRun.kind == kind)
    return [run_payload(db, r) for r in db.execute(q).scalars()]


def rate(db: Session, result_id: str, *, score: int | None = None, picked: bool | None = None,
         note: str | None = None) -> dict:
    r = db.get(BenchResult, result_id)
    if r is None:
        raise ProjectError("对比结果不存在")
    if score is not None:
        if not 0 <= int(score) <= 5:
            raise ProjectError("评分为 1–5（0 表示清除）")
        r.human_score = int(score)
    if picked is not None:
        if picked:      # 一轮只能选一个最佳
            for o in db.execute(select(BenchResult).where(BenchResult.run_id == r.run_id)).scalars():
                o.picked = False
        r.picked = bool(picked)
    if note is not None:
        r.note = note[:500]
    db.commit()
    return _result(r)


def summary(db: Session) -> dict:
    """按模型汇总：参加轮数、均价、平均用时、人工均分、机器均分、被选为最佳的次数、每元得分。"""
    rows = [r for r in db.execute(select(BenchResult)).scalars() if r.status == "done"]
    out: dict[str, list[dict]] = {"image": [], "lyric": []}
    groups: dict[tuple, list[BenchResult]] = {}
    for r in rows:
        groups.setdefault((r.kind, r.provider if r.kind == "image" else r.model, r.model), []).append(r)
    for (kind, key, model), rs in groups.items():
        rated = [r.human_score for r in rs if r.human_score]
        ai = [r.ai_score for r in rs if r.ai_score >= 0]
        avg_cost = sum(r.cost for r in rs) / len(rs)
        avg_h = (sum(rated) / len(rated)) if rated else None
        item = {
            "key": key, "model": model,
            "label": image_ad.PROVIDERS.get(key, {}).get("label", model) if kind == "image" else model,
            "runs": len(rs), "avg_cost": round(avg_cost, 4),
            "avg_elapsed_s": round(sum(r.elapsed_s for r in rs) / len(rs), 1),
            "avg_human": round(avg_h, 2) if avg_h is not None else None, "rated": len(rated),
            "avg_ai": round(sum(ai) / len(ai), 1) if ai else None,
            "picked": sum(1 for r in rs if r.picked),
            # 每元得分：人工均分 ÷ 均价。分数相同选它高的，就是性价比
            "score_per_yuan": round(avg_h / avg_cost, 1) if (avg_h and avg_cost > 0) else None,
            "price_known": all(r.price_known for r in rs),
        }
        if kind == "lyric":
            item["avg_meter_bad"] = round(sum((r.output or {}).get("meter_bad", 0) for r in rs) / len(rs), 1)
        out.setdefault(kind, []).append(item)
    for k in out:
        out[k].sort(key=lambda x: (-(x["avg_human"] or 0), x["avg_cost"]))
    # 生曲：直接取选型轮盲听的战绩（词曲工作台）
    out["music"] = [{"key": m.key, "label": m.name, "wins": m.wins, "rounds": m.rounds,
                     "avg_score": m.avg_score} for m in db.execute(select(MusicModel)).scalars()
                    if (m.rounds or 0) > 0]
    return out


def export_csv(db: Session) -> str:
    """导出全部对比明细（填造价分析文档的测试记录表用）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "类型", "目的地", "输入", "模型", "状态", "费用(元)", "用时(秒)", "输入token", "输出token",
                "机器预审分", "人工评分", "是否最佳", "行数/尺寸", "字数越界行", "备注"])
    runs = {r.id: r for r in db.execute(select(BenchRun)).scalars()}
    for r in db.execute(select(BenchResult).order_by(BenchResult.created_at)).scalars():
        run = runs.get(r.run_id)
        o = r.output or {}
        w.writerow([
            r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else "",
            "生图" if r.kind == "image" else "写词", run.destination if run else "",
            (run.title if run else "")[:60], r.model, r.status, f"{r.cost:.4f}", r.elapsed_s,
            r.input_tokens, r.output_tokens, "" if r.ai_score < 0 else r.ai_score,
            r.human_score or "", "是" if r.picked else "",
            f"{o.get('width')}×{o.get('height')}" if o.get("width") else o.get("lines", ""),
            o.get("meter_bad", "") if r.kind == "lyric" else "", r.note or r.error[:80],
        ])
    return "﻿" + buf.getvalue()
