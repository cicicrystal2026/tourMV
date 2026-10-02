"""多版本合成（ffmpeg）。产出真实可播放的 mp4，供内部评审与投放。

两阶段：先把每个片段规整为统一编码的中间文件，再用 concat 拼接并叠加音频与字幕。
这样比单条巨型 filter_complex 更稳、更易定位问题，也方便按阶段回报进度。
"""
from __future__ import annotations

import hashlib
import subprocess
import tempfile
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..adapters import media
from ..config import settings
from ..models import Asset, Project, RenderJob, Timeline, TimelineClip
from .projects import ProjectError, get as get_project

VARIANTS = {
    # ⚠️ 主交付物必须带歌词字幕。文旅 MV 是「歌 + 词」，无字幕的横版几乎是半成品：
    # 用户打开第一条片子发现没字幕，会直接认为产品没做完（这个反馈真收到过）。
    # 所以 landscape / portrait 默认带中文字幕，「无字幕」单独作为 clean 版留给二剪。
    "landscape": {"label": "横版 1920×1080 · 中文字幕", "w": 1920, "h": 1080, "subs": "zh"},
    "portrait": {"label": "竖版 1080×1920 · 中文字幕", "w": 1080, "h": 1920, "subs": "zh"},
    "sub_zh_en": {"label": "横版 1920×1080 · 中英字幕", "w": 1920, "h": 1080, "subs": "zh_en"},
    "short": {"label": "短版竖屏 2:30–2:50 · 中英字幕", "w": 1080, "h": 1920,
              "subs": "zh_en", "short": True},
    "clean": {"label": "横版 1920×1080 · 无字幕（供二次剪辑）", "w": 1920, "h": 1080, "subs": None},
}

# 短版规格（PRD F3-8）：2:30–2:50
SHORT_MIN_S, SHORT_MAX_S = 150.0, 170.0
# 含 AI 画面的成片，片尾字卡（PRD V1.1 §7.3）
AI_CARD_TEXT = "含 AI 生成画面 · Contains AI-generated visuals"
AI_CARD_SECONDS = 3.5

# 历史版本号 → 现在的等价版本。
# sub_zh 曾经是「横版 + 中文字幕」，与现在的 landscape 完全等价 ——
# 老项目里还留着这个 key 的成片记录，展示时按 landscape 的标签显示，不要出现裸 key。
LEGACY_VARIANTS = {"sub_zh": "landscape"}

CJK_FONTS = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def _font_name() -> str:
    for p in CJK_FONTS:
        if Path(p).exists():
            return Path(p).stem.replace("msyhbd", "Microsoft YaHei").replace("msyh", "Microsoft YaHei")
    return "Arial"


def esc_filter_path(p: str | Path) -> str:
    """把路径转成 ffmpeg 滤镜参数里安全的写法。

    Windows 盘符的冒号（C:）会被滤镜解析器当成选项分隔符，导致
    「Unable to parse original_size option value」之类的报错。
    实测必须写成正斜杠 + 转义冒号，并用 filename= 传参：
        subtitles=filename='C\\:/path/to/subs.ass'
    """
    return Path(p).as_posix().replace(":", r"\:")


# ------------------------------------------------------------------ 字幕
def _ts(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def build_ass(lines: list[dict], english: list[str] | None, w: int, h: int,
              offset_s: float = 0.0) -> str:
    """lines: [{text, start_s, end_s}]；english 与之等长。"""
    base = min(w, h)
    fs_zh = max(24, int(base * 0.045))
    fs_en = max(18, int(base * 0.030))
    margin_zh = int(h * 0.10) if w >= h else int(h * 0.22)
    margin_en = margin_zh - int(fs_zh * 1.35)
    font = _font_name()
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: ZH,{font},{fs_zh},&H00FFFFFF,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,{int(w*0.06)},{int(w*0.06)},{margin_zh},1
Style: EN,{font},{fs_en},&H00E8E8E8,&H000000FF,&H00101010,&H80000000,0,0,0,0,100,100,0,0,1,2,1,2,{int(w*0.06)},{int(w*0.06)},{margin_en},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows = []
    for i, ln in enumerate(lines):
        s = max(0.0, float(ln.get("start_s", 0)) - offset_s)
        e = max(s + 0.3, float(ln.get("end_s", 0)) - offset_s)
        text = (ln.get("text") or "").replace("\n", " ").replace(",", "，")
        rows.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},ZH,,0,0,0,,{text}")
        if english and i < len(english):
            en = (english[i] or "").replace("\n", " ").replace(",", " ")
            if en:
                rows.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},EN,,0,0,0,,{en}")
    return head + "\n".join(rows) + "\n"


def _translate(lines: list[str], project_id: str) -> list[str]:
    from . import prompts
    from ..adapters import llm

    payload, _real = llm.json_chat(
        prompts.translate_messages(lines),
        fallback=prompts.translate_fallback(lines),
        purpose="subtitle_translate", project_id=project_id,
    )
    en = payload.get("lines") or prompts.translate_fallback(lines)["lines"]
    if len(en) != len(lines):
        en = (en + [""] * len(lines))[: len(lines)]
    return en


# ------------------------------------------------------------------ 片段规整
def _normalize(src: Path, dst: Path, in_s: float, out_s: float, w: int, h: int,
               want_s: float | None = None) -> None:
    """把源素材的一段归一化成目标尺寸的中间片段。

    ⚠️ 关键：**产物时长必须严格等于 want_s**（这一段在成片里该占多久），
    否则会一路短下去。源素材（尤其 AI 生成的缓动片段，固定 20 秒）
    经常比一个镜头需要的时长短，截断就会让成片比音频短几十秒
    （这个 bug 犯过两次：时间轴 188 秒、成片 136 秒；后来又复现成 152/157 秒）。

    实现上有三个必须一起满足的点（少一个就失效）：

    1. **截取写在 filter 里**（`trim=start=0:end=take`），不要用 `-t`。
       `-t` 放在 `-i` 之后是**输出时长上限**，会把下面 `setpts` 拉长的部分
       重新砍回 take —— 实测拉长完全无效，成片时长纹丝不动。
    2. 拉伸后必须 `-fps_mode cfr`：让编码器按 25fps 复制帧填满新时长。
       否则时间戳被拉开而帧数不变，容器时长仍按原样收尾。
    3. 源比 want_s 长时靠**输出 `-t want_s`** 裁掉多余部分（这一处用 `-t` 才对）。

    对静图转出来的缓动片段来说，放慢在观感上和原速没有区别，
    但能保证每段时长精确、镜头总长等于音频长度。
    """
    take = max(0.2, out_s - in_s)          # 源内实际能取多长
    dur = max(0.2, want_s or take)         # 这一段应该有多长
    pts = "setpts=(PTS-STARTPTS)"
    if dur > take + 0.01:
        pts += f"*{dur / take:.6f}"        # 不够长 → 按比例放慢拉满
    chain = (f"trim=start=0:end={take:.3f},{pts},"
             f"scale={w}:{h}:force_original_aspect_ratio=increase,"
             f"crop={w}:{h},fps=25,setsar=1")
    media.run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{max(0.0, in_s):.3f}", "-i", str(src),
        "-filter_complex", f"[0:v]{chain}[v]", "-map", "[v]",
        "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
        "-t", f"{dur:.3f}", str(dst),
    ], timeout=600)


def _clip_sources(db: Session, tl: Timeline) -> list[tuple[Path, float, float, float]]:
    """返回 [(源文件, 源内起点, 源内终点, 这一段在成片里应有的时长), …]。

    第四项必须单独给：源文件可能比这一段短（AI 生成的缓动片段默认 20 秒，
    而一句歌词可能更长），此时源内区间会被夹到文件末尾，
    光看 (in_s, out_s) 就丢失了「本来需要多长」这个信息，
    结果就是每个镜头都短一点、成片比音频短一截。
    """
    clips = list(db.execute(
        select(TimelineClip).where(TimelineClip.timeline_id == tl.id).order_by(TimelineClip.order)
    ).scalars())
    amap = {a.id: a for a in db.execute(select(Asset)).scalars()}
    out: list[tuple[Path, float, float, float]] = []
    for c in clips:
        a = amap.get(c.asset_id or "")
        if a is None:
            continue
        p = Path(a.file_path)
        if not p.exists():
            continue
        want = max(0.4, float(c.end_s or 0) - float(c.start_s or 0))
        out.append((p, c.in_s or 0.0, c.out_s or 0.0, want))
    return out


def short_window(tl: Timeline) -> tuple[float, float]:
    """短版截取窗口 (起点, 时长)。

    V1.0 的问题：音频从副歌起点开始，**画面却从第一个镜头开始**，音画错位；
    时长又按「副歌起点到结尾」算，169 秒的歌只剩 2:03，达不到 2:30–2:50。
    现在：时长取 150–170 秒（歌本身不够长就整首），尽量从第一段副歌起，
    放不下就往前挪，保证窗口落在整首歌之内。
    """
    dur = float(tl.duration_s or 0)
    span = min(SHORT_MAX_S, dur) if dur >= SHORT_MIN_S else dur
    chorus = next((l for l in (tl.lyric_lines or []) if "副歌" in (l.get("section") or "")), None)
    start = float(chorus["start_s"]) if chorus else 0.0
    start = max(0.0, min(start, dur - span))
    return round(start, 3), round(span, 3)


def _has_ai_shots(db: Session, tl: Timeline) -> bool:
    ids = {c.asset_id for c in db.execute(
        select(TimelineClip).where(TimelineClip.timeline_id == tl.id)).scalars() if c.asset_id}
    if not ids:
        return False
    return any(a.source == "ai" or (a.licensor or "").startswith("AI 生成")
               for a in db.execute(select(Asset).where(Asset.id.in_(ids))).scalars())


def _ai_card_filter(span: float) -> str:
    font = media.cjk_fontfile()
    fontarg = f"fontfile='{esc_filter_path(font)}':" if font else ""
    t0 = max(0.0, span - AI_CARD_SECONDS)
    return (f"drawbox=y=ih*0.42:w=iw:h=ih*0.16:color=black@0.55:t=fill:enable='gte(t,{t0:.2f})',"
            f"drawtext={fontarg}text='{AI_CARD_TEXT}':fontcolor=white:fontsize=h*0.032:"
            f"x=(w-text_w)/2:y=(h-text_h)/2:enable='gte(t,{t0:.2f})'")


def quality_check(job: RenderJob, cfg: dict, *, expect_s: float, short: bool,
                  sub_lines: int, need_ai_card: bool, ai_card: bool) -> dict:
    """出片自检（PRD V1.1 F3-10）：时长与规格、音轨、字幕、AI 字卡。

    字幕检查目前核的是「字幕文件里确有逐句歌词且已压制」，没有做抽帧 OCR ——
    OCR 依赖较重，留到真实能力（W3）一起接；这一点在界面上如实说明。
    """
    info = media.probe(job.output_path) if job.output_path else {}
    checks: list[dict] = []

    def add(name: str, ok: bool, detail: str) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    d = float(info.get("duration_s") or 0)
    if short:
        lo, hi = (SHORT_MIN_S, SHORT_MAX_S) if expect_s >= SHORT_MIN_S else (expect_s - 1.5, expect_s + 1.5)
        add("时长", lo - 0.5 <= d <= hi + 0.5,
            f"{d:.1f} 秒（短版要求 {int(SHORT_MIN_S)}–{int(SHORT_MAX_S)} 秒）"
            + ("；整首歌不足 150 秒，无法满足短版规格" if expect_s < SHORT_MIN_S else ""))
    else:
        add("时长", abs(d - expect_s) <= 1.5, f"{d:.1f} 秒（音频 {expect_s:.1f} 秒）")
    w, h = int(info.get("width") or 0), int(info.get("height") or 0)
    add("分辨率", (w, h) == (cfg["w"], cfg["h"]), f"{w}×{h}（要求 {cfg['w']}×{cfg['h']}）")
    add("音轨", bool(info.get("has_audio")), "有" if info.get("has_audio") else "成片没有音轨")
    if cfg.get("subs"):
        add("字幕", sub_lines > 0, f"已压制 {sub_lines} 句歌词字幕" if sub_lines else "没有可压制的歌词字幕")
    if need_ai_card:
        add("AI 字卡", ai_card, "片尾已加「含 AI 生成画面」" if ai_card else "含 AI 画面但缺片尾字卡")
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def render_variant(db: Session, project_id: str, variant: str, progress=None) -> RenderJob:
    cfg = VARIANTS.get(variant)
    if cfg is None:
        raise ProjectError(f"未知的成片版本：{variant}")
    p = get_project(db, project_id)
    tl = db.execute(select(Timeline).where(Timeline.project_id == p.id)).scalar_one_or_none()
    if tl is None:
        raise ProjectError("还没有时间线，先做自动粗剪")

    job = db.execute(
        select(RenderJob).where(RenderJob.project_id == p.id, RenderJob.variant == variant)
    ).scalar_one_or_none()
    if job is None:
        job = RenderJob(project_id=p.id, variant=variant)
        db.add(job)
    job.status = "running"
    job.progress = 2
    job.error = ""
    job.qc = {}
    db.commit()

    # 规整片段放在系统临时目录，按 (项目, 版本) 固定命名：
    # 反复渲染会覆盖同名文件，天然幂等，因此不需要先清空目录。
    work = Path(tempfile.gettempdir()) / f"wenlvmv_work_{p.id[:8]}_{variant}"
    work.mkdir(parents=True, exist_ok=True)

    try:
        sources = _clip_sources(db, tl)
        if not sources:
            raise ProjectError("时间线里没有可用的镜头")

        offset = 0.0
        if cfg.get("short"):
            offset, span = short_window(tl)
        else:
            span = float(tl.duration_s or 0)
        has_ai = _has_ai_shots(db, tl)
        # 二剪用的无字幕版不加字卡（二剪成片由剪辑方自行加注），其余版本含 AI 画面就加
        want_card = has_ai and variant != "clean"

        # 阶段一：规整片段
        segs: list[Path] = []
        cursor = 0.0
        for i, (src, in_s, out_s, want_s) in enumerate(sources):
            if cfg.get("short") and cursor >= offset + span:
                break
            seg_out = work / f"seg_{i:03d}.mp4"
            _normalize(src, seg_out, in_s, out_s, cfg["w"], cfg["h"], want_s)
            segs.append(seg_out)
            cursor += want_s
            if progress:
                progress(5 + int(45 * (i + 1) / max(1, len(sources))))
        if not segs:
            raise ProjectError("没有成功规整出任何片段")

        # 阶段二：拼接 + 音频 + 字幕
        concat_list = work / "list.txt"
        concat_list.write_text(
            "\n".join(f"file '{s.as_posix()}'" for s in segs), encoding="utf-8"
        )
        # 文件名里带 timeline 版本号 —— 每次重新卡点都会产生新名字。
        # 成片是「不可变内容」，而 CDN / 网关会按 URL 缓存它：
        # 如果沿用固定文件名，重新合成后线上仍会取到旧片（这个坑真踩过：
        # 桂林重渲染后，沙箱回源拿到的还是上一版 1.9MB，CDN 直连才是新的 15.6MB）。
        stem = f"{p.name[:20]}_{variant}_v{tl.auto_version}"
        out = settings().data_dir / "renders" / f"{stem}.mp4"
        # 画面与音频必须从同一个起点截：短版两路输入都 -ss 到窗口起点（V1.0 只截了音频）
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        if offset > 0:
            cmd += ["-ss", f"{offset:.3f}"]
        cmd += ["-f", "concat", "-safe", "0", "-i", str(concat_list)]

        audio = Path(tl.audio_path) if tl.audio_path else None
        has_audio = audio is not None and audio.exists()
        if has_audio:
            cmd += ["-ss", f"{offset:.3f}", "-i", str(audio)]

        lines = [l for l in (tl.lyric_lines or [])
                 if float(l.get("end_s", 0)) > offset and float(l.get("start_s", 0)) < offset + span]
        vf = None
        sub_lines = 0
        if cfg.get("subs") and lines:
            en = _translate([l["text"] for l in lines], p.id) if cfg["subs"] == "zh_en" else None
            ass = work / "subs.ass"
            ass.write_text(build_ass(lines, en, cfg["w"], cfg["h"], offset), encoding="utf-8")
            vf = f"subtitles=filename='{esc_filter_path(ass)}'"
            sub_lines = len(lines)
        if want_card:
            vf = (vf + "," if vf else "") + _ai_card_filter(span)

        if vf:
            cmd += ["-vf", vf]
        cmd += ["-map", "0:v"]
        if has_audio:
            cmd += ["-map", "1:a", "-c:a", "aac", "-b:a", "192k"]
        # CRF 26 是发布档：实测 1080p25 的缓动镜头约 1.1 Mbps，
        # 与 CRF 22（1.8 Mbps）肉眼无差，但体积小 40% ——
        # 一条 3 分钟的片子 5 个版本，差额是 80MB 级，直接决定能不能发布出去。
        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart"]
        cmd += ["-t", f"{span:.3f}"] if cfg.get("short") else ["-shortest"]
        cmd += [str(out)]
        subprocess.run(cmd, capture_output=True, timeout=3600, check=True)

        # 再挂一段内容哈希：即使没重新卡点（版本号没变），换了编码参数重渲染
        # 也会得到新文件名。名字 = 内容，缓存永远不会张冠李戴。
        digest = hashlib.md5(out.read_bytes()).hexdigest()[:8]
        final = out.with_name(f"{stem}_{digest}.mp4")
        if final != out:
            if final.exists():
                final.unlink()
            out.rename(final)
            out = final
        # 清掉同一项目同一版本的旧产物，避免 renders 目录无限膨胀（会顶爆发布体量）
        for stale in out.parent.glob(f"{stem}_*.mp4"):
            if stale.name != out.name:
                stale.unlink(missing_ok=True)

        job.output_path = str(out)
        job.size_mb = round(out.stat().st_size / 1024 / 1024, 2) if out.exists() else 0.0
        audio_s = float(media.probe(audio).get("duration_s") or 0) if has_audio else 0.0
        expect = span if cfg.get("short") else (min(span, audio_s) if audio_s else span)
        job.qc = quality_check(job, cfg, expect_s=expect, short=bool(cfg.get("short")),
                               sub_lines=sub_lines, need_ai_card=want_card, ai_card=want_card)
        job.status = "done"
        job.progress = 100
        db.commit()
    except subprocess.CalledProcessError as e:
        job.status = "failed"
        job.error = (e.stderr or b"").decode("utf-8", "ignore")[-1500:]
        db.commit()
    except Exception as e:
        job.status = "failed"
        job.error = str(e)[:1500]
        db.commit()
    # 刻意不做批量删除：中间文件在系统临时目录里固定命名、重复渲染即覆盖，
    # 主动清理既无必要，又会触发宿主机的大批量删除保护而使合成中断。
    # 临时目录交由操作系统自行回收。

    db.refresh(job)
    return job


def preview_command(project_id: str) -> str:
    """F3-7 导出预览：直接复用横版渲染（720p 低清可加 -vf scale=1280:-2）。"""
    return f"POST /api/projects/{project_id}/render  variant=landscape_720p"


def variant_list() -> list[dict]:
    """给前端渲染「要合成哪些版本」的勾选项。

    `subs` 一并返回：界面上要能一眼看出哪几版有字幕 ——
    之前只给个「横版 1920×1080」，用户下载完才发现没歌词。
    """
    return [{"key": k, "label": v["label"], "subs": v.get("subs"),
             "short": bool(v.get("short")), "w": v["w"], "h": v["h"]}
            for k, v in VARIANTS.items()]
