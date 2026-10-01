"""Mock 音频合成 —— 用 ffmpeg 生成真实可播放、时长/速度符合需求的音轨。

目的不是"好听"，而是让上下游（波形、节拍检测、字幕对齐、成片合成）拿到
真实的、参数正确的媒体文件，从而在没有一个 API Key 的情况下验证整条管线。
ffmpeg 不可用时退化为纯 Python 写 WAV，保证绝对不断链。
"""
from __future__ import annotations

import math
import random
import struct
import subprocess
import wave
from pathlib import Path

# 常用和弦根音（Hz），避免刺耳、便于听出差异
ROOTS = [174.61, 196.00, 220.00, 233.08, 261.63, 293.66]
MAJOR = [1.0, 1.26, 1.5, 2.0]
MINOR = [1.0, 1.19, 1.5, 2.0]


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


def synth_track(out_path: Path, *, duration_s: int, seed: int = 1,
                bpm: float | None = None, hint: str = "") -> dict:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rnd = random.Random(f"{seed}-{hint}")
    root = rnd.choice(ROOTS)
    minor = any(w in hint.lower() for w in ("minor", "sad", "忧郁", "伤感", "chill"))
    chord = (MINOR if minor else MAJOR)
    if bpm is None:
        bpm = float(rnd.choice([72, 78, 84, 90, 96]))

    if _ffmpeg_available() and out_path.suffix.lower() != ".wav":
        try:
            _ffmpeg_chord(out_path, duration_s, root, chord, bpm)
            return {"bpm": bpm, "root_hz": root, "engine": "ffmpeg", "mock": True}
        except Exception:
            pass
    _py_wav(out_path.with_suffix(".wav"), duration_s, root, chord, bpm)
    return {"bpm": bpm, "root_hz": root, "engine": "python-wav", "mock": True,
            "actual_path": str(out_path.with_suffix(".wav"))}


def _ffmpeg_chord(out_path: Path, duration_s: int, root: float, chord: list[float], bpm: float) -> None:
    inputs: list[str] = []
    for f in chord:
        inputs += ["-f", "lavfi", "-i",
                   f"sine=frequency={root * f:.2f}:duration={duration_s}:sample_rate=44100"]
    n = len(chord)
    tremolo_hz = max(0.5, bpm / 60.0)
    filter_complex = (
        f"[0:a][1:a][2:a]{''.join(f'[{i}:a]' for i in range(3, n))}"
        f"amix=inputs={n}:duration=longest,"
        f"tremolo=f={tremolo_hz:.3f}:d=0.6,"
        f"aecho=0.8:0.7:120|240:0.25|0.15,"
        f"volume=0.22[out]"
    )
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *inputs,
           "-filter_complex", filter_complex, "-map", "[out]",
           "-t", str(duration_s), "-c:a", "libmp3lame", "-b:a", "160k", str(out_path)]
    subprocess.run(cmd, capture_output=True, timeout=300, check=True)


def _py_wav(out_path: Path, duration_s: int, root: float, chord: list[float], bpm: float) -> None:
    """无 ffmpeg 时的兜底：直接写一个带和弦与脉冲包络的 PCM WAV。"""
    sr, amp = 22050, 0.22
    beat = 60.0 / bpm
    with wave.open(str(out_path), "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        frames = bytearray()
        for i in range(int(sr * duration_s)):
            t = i / sr
            env = math.exp(-6 * ((t % beat) / beat))
            v = sum(math.sin(2 * math.pi * root * f * t) for f in chord) / len(chord)
            frames += struct.pack("<h", int(max(-1.0, min(1.0, v * amp * (0.35 + 0.65 * env))) * 32767))
        w.writeframes(bytes(frames))
