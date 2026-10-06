"""抽帧 OCR —— 出片自检（PRD V1.1 F3-10）用来核对字幕是否真的烧进画面、出现时间对不对。

用 RapidOCR（ONNX 推理，CPU 上一帧约 2 秒，不需要 torch）。只识别画面下方字幕区，
既快又不会把画面里的招牌、碑文当成字幕。
"""
from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path

_ENGINE = None
_TRIED = False
_PUNCT = re.compile(r"[\s，。、；：！？,.!?;:·…—\-“”\"'（）()《》]")


def available() -> bool:
    return _engine() is not None


def _engine():
    global _ENGINE, _TRIED
    if not _TRIED:
        _TRIED = True
        try:
            import logging

            from rapidocr import RapidOCR

            logging.getLogger("RapidOCR").setLevel(logging.WARNING)
            _ENGINE = RapidOCR()
        except Exception:  # noqa: BLE001
            _ENGINE = None
    return _ENGINE


def grab_frame(video: str | Path, at_s: float, out_jpg: str | Path) -> Path:
    out = Path(out_jpg)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, at_s):.3f}", "-i", str(video),
                    "-frames:v", "1", "-q:v", "3", str(out)], capture_output=True, check=True, timeout=60)
    return out


def read_subtitle_zone(image: str | Path, bottom_frac: float = 0.4) -> str:
    """识别画面下方 bottom_frac 区域的文字，返回拼接后的文本。"""
    eng = _engine()
    if eng is None:
        raise RuntimeError("未安装 OCR（pip install rapidocr）")
    from PIL import Image

    im = Image.open(image).convert("RGB")
    w, h = im.size
    crop = im.crop((0, int(h * (1 - bottom_frac)), w, h))
    tmp = Path(image).with_suffix(".sub.jpg")
    crop.save(tmp, quality=92)
    try:
        r = eng(str(tmp))
        return "".join(r.txts or []) if r is not None and r.txts else ""
    finally:
        tmp.unlink(missing_ok=True)


def zh_only(text: str) -> str:
    return "".join(c for c in _PUNCT.sub("", text or "") if "一" <= c <= "鿿")


def similarity(expected: str, got: str) -> float:
    """期望字幕（中文部分）与识别结果的相似度 0–1。"""
    a, b = zh_only(expected), zh_only(got)
    if not a:
        return 0.0
    return difflib.SequenceMatcher(None, a, b).ratio()
