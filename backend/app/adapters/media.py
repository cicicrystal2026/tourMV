"""媒体基础能力：ffprobe 探测、抽帧缩略图、生成测试素材、下载。"""
from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import requests


def run(cmd: list[str], timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, timeout=timeout, check=True)


def has_ffmpeg() -> bool:
    try:
        run(["ffmpeg", "-version"], timeout=15)
        return True
    except Exception:
        return False


def has_ffprobe() -> bool:
    try:
        run(["ffprobe", "-version"], timeout=15)
        return True
    except Exception:
        return False


def probe(path: str | Path) -> dict:
    """返回 duration_s / width / height / fps。ffprobe 不可用时返回零值，不抛错。"""
    path = Path(path)
    blank = {"duration_s": 0.0, "width": 0, "height": 0, "fps": 0.0}
    if not has_ffprobe() or not path.exists():
        return blank
    try:
        out = run([
            "ffprobe", "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", str(path),
        ], timeout=60).stdout
        data = json.loads(out)
        info = dict(blank)
        info["duration_s"] = float(data.get("format", {}).get("duration") or 0.0)
        info["has_audio"] = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                info["width"] = int(s.get("width") or 0)
                info["height"] = int(s.get("height") or 0)
                rate = s.get("avg_frame_rate") or s.get("r_frame_rate") or "0/1"
                num, _, den = rate.partition("/")
                try:
                    info["fps"] = round(float(num) / float(den or 1), 3)
                except (ValueError, ZeroDivisionError):
                    info["fps"] = 0.0
                if not info["duration_s"]:
                    info["duration_s"] = float(s.get("duration") or 0.0)
                break
        return info
    except Exception:
        return blank


def make_thumb(video: str | Path, out_jpg: str | Path, at_s: float = 1.0) -> str:
    out_jpg = Path(out_jpg)
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    try:
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-ss", f"{at_s:.2f}", "-i", str(video), "-frames:v", "1",
             "-vf", "scale=480:-2", str(out_jpg)], timeout=120)
        return str(out_jpg)
    except Exception:
        return ""


PALETTE = [
    ("#0F5E57", "#1D9E75"), ("#185FA5", "#85B7EB"), ("#993C1D", "#D85A30"),
    ("#3C3489", "#7F77DD"), ("#633806", "#EF9F27"), ("#72243E", "#D4537E"),
]

# drawtext 若不指定字体文件，在 Windows 上会因 fontconfig 缺省配置失败而「静默不画字」，
# 视频仍然生成成功但看不到标签，因此这里显式给出中文字体候选。
CJK_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def cjk_fontfile() -> str:
    """返回可用的中文字体绝对路径；未找到返回空串。"""
    for p in CJK_FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return ""


def esc_filter_path(p: str | Path) -> str:
    """ffmpeg 滤镜参数里的路径写法：正斜杠 + 转义盘符冒号（C: -> C\\:）。"""
    return Path(p).as_posix().replace(":", r"\:")


def generate_test_clip(out_mp4: str | Path, *, seconds: float = 6.0,
                       width: int = 1920, height: int = 1080, label: str = "clip") -> str:
    """生成一段带渐变底与文字的占位实拍素材。仅用于实验室没有真实素材时跑通流程。"""
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    h = int(hashlib.md5(label.encode()).hexdigest()[:6], 16)
    c1, c2 = PALETTE[h % len(PALETTE)]
    # drawtext 把 : ' % \ 当作语法字符，标签里必须先剔除
    safe = label.replace(":", " ").replace("'", "").replace("%", "").replace("\\", "")[:28]
    font = cjk_fontfile()
    fontarg = f"fontfile='{esc_filter_path(font)}':" if font else ""
    vf = (
        f"drawbox=x=0:y=0:w={width}:h={height}:color={c1}:t=fill,"
        f"drawbox=x=0:y={int(height*0.55)}:w={width}:h={int(height*0.45)}:color={c2}:t=fill,"
        f"drawbox=x=40:y=40:w={width-80}:h={height-80}:color=white@0.18:t=6,"
        f"drawtext={fontarg}text='{safe}':fontcolor=white:fontsize={max(28, width // 28)}:"
        f"x=(w-text_w)/2:y=(h-text_h)/2"
    )
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"color=c=0x222222:s={width}x{height}:r=25:d={seconds:.2f}",
         "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
         str(out_mp4)], timeout=300)
    return str(out_mp4)


def is_valid_image(path: str | Path) -> bool:
    """校验图片是否完整可读。

    为什么不能只看文件大小：下载被中断会留下「大小看着正常、结构其实损坏」的 PNG，
    ffmpeg 读它会直接卡死（实测卡满 300 秒超时）。
    必须让 ffprobe 真正解析一遍，能读出宽高才算有效。
    """
    p = Path(path)
    if not p.exists() or p.stat().st_size < 4096:
        return False
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
            capture_output=True, timeout=30, text=True,
        )
    except Exception:  # noqa: BLE001
        return False
    return r.returncode == 0 and bool(r.stdout.strip()) and "," in r.stdout


def to_jpeg(src: str | Path, dst: str | Path, *, quality: int = 90,
            max_width: int = 2048) -> str:
    """把任意可读图片统一转成 JPEG。

    为什么必须转：文生图给的是 PNG，一张 1.5MB 起 —— 一句一张的话 18 句就是 30MB，
    白白撑大发布包（而发布体量直接决定能不能发出去）。转成 JPEG 后大约 400–700KB，
    审核页预览和片段源用它肉眼无差。

    用 ffmpeg 而不是 Pillow：项目本来就依赖 ffmpeg 做全部媒体处理，
    而运行环境里**没有装 Pillow**（踩过：ModuleNotFoundError: No module named 'PIL'）。
    """
    p, out = Path(src), Path(dst)
    if not is_valid_image(p):
        raise RuntimeError(f"图片不可读：{p.name}")
    out.parent.mkdir(parents=True, exist_ok=True)
    # -q:v 2 ≈ JPEG 质量 90（2 最好、31 最差）
    q = max(2, min(31, int(round(31 * (1 - quality / 100)))))
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(p),
         "-vf", f"scale='min({max_width},iw)':-2",
         "-q:v", str(q), str(out)], timeout=180)
    if not is_valid_image(out):
        raise RuntimeError("转 JPEG 失败")
    return str(out)


def image_to_clip(img: str | Path, out_mp4: str | Path, *, seconds: float = 8.0,                  width: int = 1920, height: int = 1080, fps: int = 25,
                  zoom: bool = True) -> str:
    """把静态图片转成带缓慢推拉的视频片段。

    为什么需要这一步：合成管线是按「视频片段」剪切素材的（render.py 用 -ss/-t 截取），
    而文生图的产物是静态图。静图直接进合成会是一张不动的画面，成片显得死板；
    加一点缓慢缩放（kenburns）就有实拍运镜的感觉。

    片段要尽量长（默认 20 秒）：一句歌词最长可能十几秒，素材比它短就得靠放慢补齐。
    """
    src = Path(img)
    out_mp4 = Path(out_mp4)
    out_mp4.parent.mkdir(parents=True, exist_ok=True)
    frames = max(2, int(seconds * fps))
    if zoom:
        # zoompan 的 1920x1080 输出直接就是目标尺寸。
        # 注意不要在它之前再放大 —— 曾经写成 scale 到 2 倍再 zoompan，
        # 每帧要处理 3840x2160，6 张图要跑好几分钟。zoom 是往上放大，
        # 不会露边，根本不需要预留余量。
        #
        # 缩放步长按总帧数摊算：写死步长会让长片段在头几秒就推到头、
        # 剩下十几秒完全静止。
        amount = 0.10
        rate = amount / frames
        vf = (
            f"scale={width}:{height}:force_original_aspect_ratio=increase,"
            f"crop={width}:{height},"
            f"zoompan=z='min(zoom+{rate:.8f},{1.0 + amount:.4f})':d={frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps},"
            # 万相最大只给 1440×810，落到 1920×1080 是放大 —— 轻微锐化补偿一下柔化，
            # 观感上更接近实拍（不锐化会明显发糊）
            f"unsharp=5:5:0.6:5:5:0.0"
        )
    else:
        vf = (f"scale={width}:{height}:force_original_aspect_ratio=increase,"
              f"crop={width}:{height},unsharp=5:5:0.6:5:5:0.0")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-loop", "1", "-i", str(src),
         "-vf", vf, "-t", f"{seconds:.2f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
         "-r", str(fps), str(out_mp4)], timeout=300)
    return str(out_mp4)


def make_thumb_from_image(img: str | Path, out_jpg: str | Path) -> str:
    """从静态图直接生成缩略图（不走 ffmpeg 的 -ss 截帧）。"""
    out_jpg = Path(out_jpg)
    out_jpg.parent.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(img), "-vf", "scale=480:-2", str(out_jpg)], timeout=120)
    return str(out_jpg)


def download(url: str, dest: str | Path, timeout: int = 300) -> str:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with dest.open("wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
    return str(dest)


def write_bytes(content: bytes, dest: str | Path) -> str:
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    return str(dest)
