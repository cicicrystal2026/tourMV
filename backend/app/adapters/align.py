"""音频分析适配器：节拍检测（librosa）+ 歌词逐句对齐（faster-whisper）。

对齐这件事的演进，值得写清楚，因为踩过真实的坑：

- **v1 解析式兜底（已废弃的默认路径）**：按「字数加权」把歌词铺满整首歌，
  再把每句起点吸附到节拍上。问题是它**完全不知道歌声在哪里** ——
  前奏有多长、哪句被拖腔、副歌是否重复，全靠猜。
  实测用户直接反馈「从 58 秒开始才同步」，就是这个原因。
- **v2 DTW 强制对齐（现在的默认路径）**：用 faster-whisper 识别出**真实的人声
  字符流及其时间戳**，再把「我们已知的歌词」与识别结果做单调对齐（DTW），
  于是每句歌词的起止时间来自**歌声本身**。
  额外用 `initial_prompt` 把歌词喂给模型做上下文，能显著提升识别率 ——
  歌唱的识别本来就比说话难，这个提示很重要。
- 识别质量太差时（匹配率低于阈值）自动回退到解析式，绝不产出明显错乱的时间轴。

节拍检测仍走 librosa；没有 librosa 时用解析网格，但**不再用它吸附歌词起点**
（假网格上吸附等于在错误估计上再叠一层随机偏移，纯有害）。
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path

TARGET_BPM_RANGE = (60, 140)

# 匹配率低于这个值就认为识别不可信，回退解析式
MIN_MATCH_RATIO = 0.30


# ---------------------------------------------------------------- 节拍检测
def detect_beats(audio_path: str, fallback_bpm: float = 84.0) -> dict:
    """返回 {bpm, beats: [秒], strong_beats: [秒], engine}。"""
    try:
        import librosa  # type: ignore
        import numpy as np  # type: ignore

        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        tempo, frames = librosa.beat.beat_track(y=y, sr=sr)
        bpm = float(np.atleast_1d(tempo)[0])
        beats = [float(t) for t in librosa.frames_to_time(frames, sr=sr)]
        if len(beats) < 4:
            raise RuntimeError("节拍点过少")
        beats = _dedupe(beats)
        strong = _pick_strong(beats, y, sr, librosa)
        return {"bpm": round(bpm, 2), "beats": beats, "strong_beats": strong, "engine": "librosa"}
    except Exception:
        return _analytic_beats(fallback_bpm)


def _dedupe(beats: list[float], min_gap: float = 0.08) -> list[float]:
    out: list[float] = []
    for b in sorted(beats):
        if not out or b - out[-1] >= min_gap:
            out.append(b)
    return out


def _pick_strong(beats: list[float], y, sr: int, librosa) -> list[float]:
    """按每拍能量取每 4 拍一组的最强拍作为强拍（近似 downbeat）。"""
    import numpy as np  # type: ignore

    rms = librosa.feature.rms(y=y)[0]
    times = librosa.times_like(rms, sr=sr)
    energy = []
    for b in beats:
        idx = np.searchsorted(times, b)
        lo, hi = max(0, idx - 2), min(len(rms), idx + 3)
        energy.append(float(rms[lo:hi].mean()) if hi > lo else 0.0)
    strong: list[float] = []
    for i in range(0, len(beats), 4):
        window = energy[i:i + 4]
        if not window:
            continue
        strong.append(beats[i + window.index(max(window))])
    return strong


def _analytic_beats(bpm: float) -> dict:
    bpm = min(max(bpm, TARGET_BPM_RANGE[0]), TARGET_BPM_RANGE[1])
    period = 60.0 / bpm
    beats = [round(i * period, 4) for i in range(int(600 / period))]
    return {
        "bpm": round(bpm, 2),
        "beats": beats,
        "strong_beats": beats[::4],
        "engine": "analytic",  # ⚠️ 这是**假网格**，不可用于吸附真实歌词起点
    }


# ---------------------------------------------------------------- 模型加载
_MODELS: dict[str, object] = {}

_PUNCT = re.compile(r"[\s，。、！？；：\"'“”‘’（）()【】《》…—\-·,.!?;:~`]+")

# faster-whisper 只需要这几个文件；用 requests 自己拉，
# 不走 huggingface_hub —— 后者默认走 Xet 分发通道，
# 在代理/内网环境下会一直 `handshake operation timed out`（实测卡死）。
_MODEL_REPO = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}
_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")
_APPROX_MB = {"tiny": 75, "base": 145, "small": 480, "medium": 1500, "large-v3": 3000}


def _model_dir(name: str) -> Path:
    base = os.environ.get("ALIGN_MODEL_DIR", "").strip()
    if base:
        return Path(base) / name
    from ..config import settings

    return settings().data_dir / "models" / name


def _fetch(url: str, dest: Path, timeout: int = 120) -> bool:
    """带断点续传的单文件下载。404 返回 False（该文件可选）。"""
    import requests

    part = dest.with_suffix(dest.suffix + ".part")
    got = part.stat().st_size if part.exists() else 0
    headers = {"Range": f"bytes={got}-"} if got else {}
    with requests.get(url, headers=headers, stream=True, timeout=timeout) as r:
        if r.status_code == 404:
            return False
        if r.status_code == 416:      # 已下完
            part.rename(dest)
            return True
        r.raise_for_status()
        mode = "ab" if (got and r.status_code == 206) else "wb"
        with part.open(mode) as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    part.rename(dest)
    return True


def _model_ok(dest: Path, name: str) -> bool:
    """model.bin 存在 **且体积合理** 才算就绪。

    只判存在是不够的：下载中断会留下一个残缺的 model.bin，
    直接喂给 CTranslate2 会以「模型损坏」之类的报错炸掉，
    而且下次启动还会认为它已经就绪、永不重下。
    """
    f = dest / "model.bin"
    if not f.exists():
        return False
    need = _APPROX_MB.get(name, 480) * 0.6 * 1048576
    return f.stat().st_size >= need


def ensure_model(name: str, log=None) -> Path | None:
    """确保模型文件就位；缺失则从镜像/官方两个源依次尝试下载。"""
    dest = _model_dir(name)
    if _model_ok(dest, name):
        return dest
    repo = _MODEL_REPO.get(name, name)
    dest.mkdir(parents=True, exist_ok=True)

    from ..config import settings

    endpoints = []
    for ep in (settings().hf_endpoint, "https://huggingface.co"):
        if ep and ep not in endpoints:
            endpoints.append(ep)

    for ep in endpoints:
        try:
            if log:
                log(f"从 {ep} 下载模型 {repo}（约 {_APPROX_MB.get(name, '数百')}MB）")
            for fn in _MODEL_FILES:
                _fetch(f"{ep}/{repo}/resolve/main/{fn}", dest / fn)
            if _model_ok(dest, name):
                if log:
                    log(f"模型就绪：{dest}")
                return dest
        except Exception as e:  # noqa: BLE001
            if log:
                log(f"{ep} 下载失败：{type(e).__name__}: {str(e)[:100]}")
    return None


def load_align_model(name: str | None = None, log=None):
    """加载 faster-whisper 模型；失败抛异常（调用方负责回退）。"""
    from ..config import settings

    name = name or settings().align_model
    if name in _MODELS:
        return _MODELS[name]

    # 兜底再设一次（huggingface_hub 若被其它路径用到也受益）
    os.environ.setdefault("HF_ENDPOINT", settings().hf_endpoint)
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")

    from faster_whisper import WhisperModel

    local = ensure_model(name, log=log)
    target = str(local) if local else name
    m = WhisperModel(target, device="cpu", compute_type="int8",
                     cpu_threads=max(2, (os.cpu_count() or 4) - 1))
    _MODELS[name] = m
    return m


def model_ready(name: str | None = None) -> bool:
    from ..config import settings

    name = name or settings().align_model
    if name in _MODELS:
        return True
    return _model_ok(_model_dir(name), name)


def status() -> dict:
    """给接口/界面用的对齐能力状态。"""
    from ..config import settings

    s = settings()
    has_lib = True
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        has_lib = False
    return {
        "engine": s.align_engine,
        "enabled": s.align_enabled,
        "model": s.align_model,
        "has_library": has_lib,
        "model_ready": model_ready(),
        "note": model_note(),
    }


def model_note() -> str:
    from ..config import settings

    s = settings()
    if not s.align_enabled:
        return "已关闭强制对齐（ALIGN_ENGINE=analytic），歌词时间轴为估算值 —— 音画可能不同步"
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        return ("未装 faster-whisper，歌词按字数估算 —— 音画会有偏差。"
                "装法：pip install faster-whisper")
    if model_ready():
        return f"强制对齐已就绪（{s.align_model}，本地模型）"
    return (f"强制对齐待下载模型（{s.align_model}，首次约 "
            f"{_APPROX_MB.get(s.align_model, 480)}MB）")


# ---------------------------------------------------------------- 歌词对齐
def align_lyrics(audio_path: str, lines: list[str], duration_s: float,
                 strong_beats: list[float] | None = None, bpm: float = 84.0,
                 section_of: list[str] | None = None) -> dict:
    """返回 {lines: [{text, start_s, end_s}], engine, note}。"""
    lines = [ln for ln in (lines or []) if ln and ln.strip()]
    if not lines:
        return {"lines": [], "engine": "analytic", "note": "没有歌词"}

    from ..config import settings

    s = settings()
    if s.align_enabled:
        try:
            out = _whisper_align(audio_path, lines, duration_s)
            return out
        except Exception as e:  # noqa: BLE001
            fallback = _analytic_align(lines, duration_s, strong_beats, bpm, snap=False)
            fallback["note"] = f"强制对齐不可用（{type(e).__name__}: {str(e)[:120]}），已回退估算"
            return fallback

    out = _analytic_align(lines, duration_s, strong_beats, bpm, snap=False)
    out["note"] = "未启用强制对齐，按字数估算"
    return out


# -------------------------------------------------------- DTW 强制对齐实现
_CC = None


def _t2s(text: str) -> str:
    """繁体转简体。

    Whisper 的中文输出**经常是繁体**（实测「永定门下风卷旧旗影」被转成
    「原定門下風卷就起影」），不归一化的话「門≠门、風≠风」会让字符匹配
    大面积落空，DTW 直接失去锚点。opencc 不可用时退化为原样。
    """
    global _CC
    if _CC is None:
        try:
            from opencc import OpenCC

            _CC = OpenCC("t2s")
        except Exception:  # noqa: BLE001
            _CC = False
    return _CC.convert(text) if _CC else text


def _norm(text: str) -> list[str]:
    """只保留有意义的字符（去标点空白），并统一成简体。"""
    return list(_PUNCT.sub("", _t2s(text or "")))


def _cjk_ratio(text: str) -> float:
    cs = _PUNCT.sub("", text or "")
    if not cs:
        return 0.0
    return sum(1 for c in cs if "\u4e00" <= c <= "\u9fff") / len(cs)


# Whisper 在器乐段常幻觉出「制作人员表」——实测见过
# 「詞曲 李宗盛」「字幕by索兰娅」。它们是纯中文，CJK 过滤拦不住，
# 而一旦被当成歌词锚点，就会把某句的跨度拉到几十秒。
_CREDIT_WORDS = ("词曲", "作词", "作曲", "编曲", "字幕", "混音", "母带",
                 "演唱", "制作人", "出品", "录音", "和声", "吉他", "钢琴",
                 "监制", "统筹", "鸣谢", "特别感谢")


def _is_credit(text: str) -> bool:
    t = _t2s(text or "")
    if any(w in t for w in _CREDIT_WORDS):
        return True
    # 「字幕by…」被转成中文时不带「字幕」两字的情况
    return "by" in t.lower() and len(t) <= 12


def _dtw_align(lyric_chars: list[str], asr_chars: list[str],
               band: int = 28) -> list[int]:
    """对「歌词字符序列」与「识别字符序列」做单调对齐（DTW）。

    返回长度为 len(lyric_chars) 的列表，第 i 项是与之对齐的 asr 下标
    （-1 表示没找到对应，多半这个字没被唱到）。

    代价：相同字符 0、不同 1；三种走法 ——
      对角(sub) 匹配/替换；上(del) 歌词多出一个字（漏唱/识别缺失）；
      左(ins) 识别多出一个字（幻觉/重复）。
    只在带宽内计算：识别结果的幻觉可能很长，全矩阵容易把路径带偏。
    """
    m, n = len(lyric_chars), len(asr_chars)
    if m == 0 or n == 0:
        return [-1] * m

    INF = float("inf")
    W_INS, W_DEL = 0.9, 1.1

    # 第 0 行 = 「歌词一个字都还没消费，先跳过 j 个识别字符」，
    # 也就是允许 ASR 在开头多出若干字（前奏里的幻觉/气声）。
    # ⚠️ 这一行必须真的存在：早期版本用 prev[j-1] 递推，
    # 而 i=0 行只有 j=0 是有限的，结果开头最多只能跳 1 个字，
    # 首句很容易被误判成「没唱到」。
    prev = [W_INS * j for j in range(n + 1)]
    choices: list[list[str]] = [["ins" if j else "" for j in range(n + 1)]]

    for i in range(1, m + 1):
        cur = [INF] * (n + 1)
        ch = [""] * (n + 1)
        cur[0] = W_DEL * i
        ch[0] = "del"
        lo, hi = max(1, i - band), min(n, i + band)
        for j in range(lo, hi + 1):
            opts = (
                (prev[j - 1] + (0.0 if lyric_chars[i - 1] == asr_chars[j - 1] else 1.0), "sub"),
                (prev[j] + W_DEL, "del"),
                (cur[j - 1] + W_INS, "ins"),
            )
            best, tag = min(opts, key=lambda x: x[0])
            cur[j] = best
            ch[j] = tag
        choices.append(ch)
        prev = cur

    # 终点取全局最优（允许尾部有多余的识别字符）
    j_end = min(range(n + 1), key=lambda x: prev[x])
    return _backtrack(choices, m, j_end)


def _backtrack(choices: list[list[str]], m: int, j: int) -> list[int]:
    """从 (m, j) 回溯到 (0, 0)。

    ⚠️ choices 的第 0 项是 DP 的第 0 行（全是 ins），所以第 i 行的表在 choices[i]。
    早期写成 choices[i-1] 会让整条路径错位一格，首字永远对不上。
    """
    mapping = [-1] * m
    i = m
    while i > 0 and j > 0:
        tag = choices[i][j]
        if tag == "sub":
            mapping[i - 1] = j - 1
            i -= 1
            j -= 1
        elif tag == "del":
            i -= 1
        elif tag == "ins":
            j -= 1
        else:
            break
    return mapping


def _cluster_span(js: list[int], asr_times: list[tuple[float, float]],
                  max_gap: float = 2.5) -> tuple[float, float] | None:
    """从匹配到的字符下标里取「时间上最连续的一簇」，返回 (起, 止)。

    为什么不直接取 min/max：副歌会重复，某句的少量字符可能匹配到远处的
    那一次重复（或被幻觉段吸走），于是跨度被拉到几十秒 ——
    实测永州第 8 句就这样变成了 76.87→97.70s（真实只有 7 秒）。
    取最密的一簇能同时解决「重复段」和「孤立误匹配」。
    """
    if not js:
        return None
    js = sorted(set(js))
    clusters: list[list[int]] = [[js[0]]]
    for x in js[1:]:
        if asr_times[x][0] - asr_times[clusters[-1][-1]][1] > max_gap:
            clusters.append([x])
        else:
            clusters[-1].append(x)
    best = max(clusters, key=lambda c: (len(c), -(asr_times[c[0]][0])))
    return asr_times[best[0]][0], asr_times[best[-1]][1]


def _whisper_align(audio_path: str, lines: list[str], duration_s: float) -> dict:
    model = load_align_model()

    # ⚠️ 刻意**不传 initial_prompt**。试过三种策略（北京·中轴线实测）：
    #
    #   | 策略            | 首句起点        | 识别区间        | 匹配率 | 耗时 |
    #   | 无 prompt       | 20.20s（正确）  | 20.20-153.08   | 38%    | 38s  |
    #   | 歌词作 prompt   | 19.54s          | 3.24-153.08 ⚠  | 37%    | 71s  |
    #   | 中性 prompt     | 9.64s（错）     | 7.84-154.66 ⚠  | 35%    | 49s  |
    #
    # 传了 prompt 之后，whisper 会**在前奏里幻觉出歌词**（把首句拉到 9~19s，
    # 而真实人声从 20.2s 才开始），而且因为要处理更长上下文而慢一倍。
    # 识别率的提升（35%→38%）远不值这个代价 —— DTW 在字符级本来就容忍错字，
    # 真正重要的是「哪里有人在唱」这个信息，而这恰恰会被幻觉破坏。
    segs, info = model.transcribe(
        audio_path, language="zh", beam_size=5,
        # ⚠️ **必须关掉 VAD**。Silero VAD 是按「说话」训的，对带伴奏的歌唱
        # 几乎全判成静音 —— 实测 threshold=0.5 时 169 秒里只剩 2.7 秒，
        # 转写结果为空，整个强制对齐直接失效。用 CJK 占比过滤幻觉段即可。
        vad_filter=False,
        word_timestamps=True,
        condition_on_previous_text=False,
    )
    segs = list(segs)

    # 逐字时间轴：把每个词的时长按字数均摊到字符上
    asr_chars: list[str] = []
    asr_times: list[tuple[float, float]] = []
    dropped = 0
    for sg in segs:
        txt = getattr(sg, "text", "") or ""
        if _cjk_ratio(txt) < 0.5 or _is_credit(txt):
            dropped += 1      # 纯拉丁段 / 制作人员表：都不是歌词
            continue
        words = getattr(sg, "words", None) or []
        for w in (words or [sg]):
            txt = getattr(w, "word", None)
            if txt is None:
                txt = getattr(w, "text", "") or ""
            st = float(getattr(w, "start", 0.0) or 0.0)
            en = float(getattr(w, "end", st) or st)
            cs = _norm(txt)
            if not cs:
                continue
            span = max(0.02, en - st)
            for k, ch in enumerate(cs):
                asr_chars.append(ch)
                asr_times.append((st + span * k / len(cs), st + span * (k + 1) / len(cs)))

    if not asr_chars:
        raise RuntimeError("ASR 未识别到任何内容（可能是纯器乐或无歌词）")

    lyric_chars: list[str] = []
    bounds: list[tuple[int, int]] = []
    for ln in lines:
        cs = _norm(ln)
        bounds.append((len(lyric_chars), len(lyric_chars) + len(cs)))
        lyric_chars.extend(cs)

    mapping = _dtw_align(lyric_chars, asr_chars)
    hit = sum(1 for i, j in enumerate(mapping)
              if j >= 0 and lyric_chars[i] == asr_chars[j])
    ratio = hit / max(1, len(lyric_chars))

    if ratio < MIN_MATCH_RATIO:
        raise RuntimeError(f"识别与歌词匹配率过低（{ratio:.0%}），判定识别不可信")

    out: list[dict] = []
    used_times: list[tuple[float, float]] = []
    for idx, ln in enumerate(lines):
        a, b = bounds[idx]
        js = [mapping[i] for i in range(a, b) if 0 <= mapping[i] < len(asr_chars)]
        span = _cluster_span(js, asr_times)
        if span:
            out.append({"text": ln.strip(), "start_s": span[0],
                        "end_s": span[1], "src": "asr"})
            used_times.append(span)
        else:
            out.append({"text": ln.strip(), "start_s": None, "end_s": None, "src": "interp"})

    _fill_and_smooth(out, lines, duration_s, used_times or asr_times)

    # 质量闸门：如果绝大多数句子都没能挂到识别结果上（全靠插值），
    # 那这条时间轴并不比解析式估计更可信 —— 宁可回退，也不要产出
    # 「看起来对齐了、其实全是猜的」的时间轴。
    anchored = sum(1 for o in out if o.get("src") == "asr")
    if anchored < max(2, len(out) * 0.6):
        raise RuntimeError(f"仅 {anchored}/{len(out)} 句挂上识别结果，判定对齐不可信")

    lo = min((o["start_s"] for o in out if o.get("src") == "asr"), default=0.0)
    hi = max((o["end_s"] for o in out if o.get("src") == "asr"), default=duration_s)
    return {
        "lines": out,
        "engine": "whisper",
        "match_ratio": round(ratio, 3),
        "anchored": anchored,
        "dropped_segments": dropped,
        "vocal_span": [round(lo, 2), round(hi, 2)],
        "note": (f"强制对齐：{anchored}/{len(out)} 句时间来自歌声"
                 f"（人声区间 {lo:.1f}-{hi:.1f}s，识别匹配率 {ratio:.0%}）"),
    }


def _fill_and_smooth(out: list[dict], lines: list[str], duration_s: float,
                     asr_times: list[tuple[float, float]]) -> None:
    """补全没对齐上的句子，并保证单调、不重叠、落在音频范围内。"""
    n = len(out)
    # 已知锚点
    known = [i for i, o in enumerate(out) if o["start_s"] is not None]
    if not known:
        # 全没对上 → 交给解析式（调用方会因 ratio 低而回退，这里兜底）
        for o in out:
            o["src"] = "interp"
        _spread(out, 0.0, duration_s)
        return

    audio_end = max((t[1] for t in asr_times), default=duration_s)
    audio_start = min((t[0] for t in asr_times), default=0.0)

    # 头部：第一句之前没对上的句子，往前铺但不超过人声起点
    first = known[0]
    if first > 0:
        _spread(out[:first], audio_start, out[first]["start_s"])
    # 中间：两个锚点之间
    for k in range(len(known) - 1):
        i, j = known[k], known[k + 1]
        if j - i > 1:
            _spread(out[i + 1:j], out[i]["end_s"], out[j]["start_s"])
    # 尾部
    last = known[-1]
    if last < n - 1:
        _spread(out[last + 1:], out[last]["end_s"], min(duration_s, audio_end + 4.0))

    # 单调 + 不重叠 + 最小显示时长
    for o in out:
        o["start_s"] = max(0.0, min(float(o["start_s"]), duration_s))
        o["end_s"] = max(o["start_s"] + 0.3, min(float(o["end_s"]), duration_s))
    for i in range(1, n):
        if out[i]["start_s"] < out[i - 1]["end_s"] + 0.05:
            out[i]["start_s"] = round(out[i - 1]["end_s"] + 0.05, 3)
            out[i]["end_s"] = max(out[i]["end_s"], out[i]["start_s"] + 0.3)
    # 相邻很紧时把前一句延到下一句起点（避免字幕闪现）
    for i in range(n - 1):
        gap = out[i + 1]["start_s"] - out[i]["end_s"]
        if 0 < gap < 0.5:
            out[i]["end_s"] = round(out[i + 1]["start_s"] - 0.05, 3)
    for o in out:
        o["start_s"] = round(o["start_s"], 3)
        o["end_s"] = round(o["end_s"], 3)
        o.pop("src_dummy", None)


def _spread(rows: list[dict], start: float, end: float) -> None:
    """把 rows 在 [start, end] 内按字数权重匀开。"""
    if not rows:
        return
    span = max(0.3, end - start)
    weights = [max(4, len(_norm(r["text"]))) for r in rows]
    total = sum(weights) or 1
    cursor = start
    for r, w in zip(rows, weights):
        seg = span * w / total
        r["start_s"] = cursor
        r["end_s"] = cursor + seg * 0.88
        cursor += seg


# ---------------------------------------------------------------- 解析式兜底
def _analytic_align(lines: list[str], duration_s: float,
                    strong_beats: list[float] | None, bpm: float,
                    *, snap: bool = False) -> dict:
    """按字数加权分配时长。

    `snap` 默认为 False：**只有节拍来自真实引擎时才允许吸附**。
    之前无条件吸附，而 librosa 缺席时 strong_beats 是「84BPM 假网格」，
    把已经不准的估计再挪到任意位置，纯属雪上加霜。
    """
    lines = [ln for ln in lines if ln and ln.strip()]
    if not lines:
        return {"lines": [], "engine": "analytic"}
    weights = [max(4, len(ln.strip())) for ln in lines]
    total = sum(weights)
    lead = min(6.0, duration_s * 0.06)
    usable = max(duration_s - lead, 1.0)
    raw = []
    cursor = lead
    for ln, w in zip(lines, weights):
        span = usable * w / total
        gap = min(0.35, span * 0.12)
        raw.append([ln.strip(), cursor, cursor + span - gap])
        cursor += span

    period = 60.0 / max(bpm, 1)
    strong = strong_beats or []
    out = []
    for text, s, e in raw:
        if snap and strong:
            s = min(strong, key=lambda b: abs(b - s))
            end_candidates = [b for b in strong if b > s + period * 0.5]
            if end_candidates:
                e = min(end_candidates, key=lambda b: abs(b - (s + (e - s))))
        out.append({"text": text, "start_s": round(float(s), 3), "end_s": round(float(e), 3)})
    for i in range(1, len(out)):
        if out[i]["start_s"] < out[i - 1]["end_s"]:
            mid = (out[i - 1]["end_s"] + out[i]["start_s"]) / 2
            out[i - 1]["end_s"] = round(mid, 3)
            out[i]["start_s"] = round(mid, 3)
    return {"lines": out, "engine": "analytic"}


def nearest_beat(t: float, beats: list[float]) -> float:
    if not beats:
        return t
    return min(beats, key=lambda b: abs(b - t))


def snap_start(t: float, beats: list[float], tolerance: float = 0.35) -> float:
    if not beats:
        return t
    b = nearest_beat(t, beats)
    return b if abs(b - t) <= tolerance else t


def beats_in(start_s: float, end_s: float, beats: list[float]) -> int:
    return sum(1 for b in beats if start_s <= b < end_s)


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def rms_energy(path: str) -> float:
    try:
        import librosa  # type: ignore
        import numpy as np  # type: ignore

        y, sr = librosa.load(path, sr=22050, mono=True)
        return float(np.sqrt(np.mean(y ** 2)))
    except Exception:
        return 0.0


def bpm_or_default(v: float) -> float:
    return v if math.isfinite(v) and v > 0 else 84.0
