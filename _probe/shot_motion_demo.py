"""镜头运动对比样片：验证「PPT 感」的根因与零成本改法的效果。

现状问题（已在代码里量到）：
    image_to_clip 把 10% 的推近摊在**整条 20 秒素材**上（rate = amount/500 帧），
    而实际每个镜头只用到 4~6 秒 → 真正落在画面里的推近只有 2.8%，
    肉眼几乎看不出运动 → 「一张图挂着不动」的 PPT 感。

本脚本用同一张景区图产出三段做对比：
    ① 现状      ：一镜 6 秒，推近 2.8%
    ② 方案A 镜1 ：远景 3 秒，推近 12.6%（幅度按 20 秒摊，但速度是现状的 4.5 倍）
    ③ 方案A 镜2 ：近景 3 秒，从素材靠后的时间窗起拍 → 景别明显跳进

只跑 ffmpeg，不调用任何付费 API，成本 ¥0。
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.adapters import media  # noqa: E402

OUT = ROOT / "data" / "_probe_demo"
W, H, FPS = 1920, 1080, 25
CLIP_S = 20.0                 # 与 storyboard.CLIP_SECONDS 一致
IMG = ROOT / "data" / "storyboard" / "篁岭晒秋观景台-9fe3b.jpg"


def make_clip(img: Path, out: Path, amount: float) -> Path:
    """复刻 image_to_clip 的滤镜链，只把推近幅度作为参数暴露出来。"""
    frames = int(CLIP_S * FPS)
    rate = amount / frames
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
        f"zoompan=z='min(zoom+{rate:.8f},{1.0 + amount:.4f})':d={frames}:"
        f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={FPS},"
        f"unsharp=5:5:0.6:5:5:0.0"
    )
    media.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-loop", "1", "-i", str(img), "-vf", vf, "-t", f"{CLIP_S:.2f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-r", str(FPS), str(out)], timeout=300)
    return out


def make_part(src: Path, out: Path, in_s: float, dur: float, label: str) -> Path:
    """从素材截一段并烧上说明文字。"""
    font = media.esc_filter_path(media.cjk_fontfile())
    text = label.replace("\\", "").replace("'", "").replace(":", "\\:")
    dt = (f"drawtext=fontfile='{font}':text='{text}':fontsize=46:fontcolor=white:"
          f"box=1:boxcolor=black@0.55:boxborderw=20:x=(w-text_w)/2:y=h-160")
    media.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-ss", f"{in_s:.3f}", "-i", str(src),
               "-vf", dt, "-t", f"{dur:.3f}",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-r", str(FPS), "-an", str(out)], timeout=300)
    return out


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    if not IMG.exists():
        print(f"缺少底图：{IMG}")
        return 1

    print("① 生成现状片段（推近 10% / 20s）…")
    now = make_clip(IMG, OUT / "clip_now.mp4", 0.10)
    print("② 生成强化片段（推近 42% / 20s）…")
    new = make_clip(IMG, OUT / "clip_new.mp4", 0.42)

    seg = int(5.7 * FPS)                      # 现状里一个镜头的典型帧数
    parts = [
        (now, 0.0, 6.0, "现状：6 秒一镜到底 · 画面只推近 2.8%（几乎静止）"),
        (new, 0.0, 3.0, "方案A 镜1：远景 3 秒 · 推近 12.6%（速度 4.5 倍）"),
        (new, 17.0, 3.0, "方案A 镜2：近景 3 秒 · 景别跳进 36%（同一景点，非同一张构图）"),
    ]
    files = []
    for i, (src, a, b, lab) in enumerate(parts, 1):
        p = make_part(src, OUT / f"part{i}.mp4", a, b, lab)
        files.append(p)
        print(f"   段落{i} 完成  {b:.1f}s  {lab[:22]}…")

    lst = OUT / "concat.txt"
    lst.write_text("".join(f"file '{f.name}'\n" for f in files), encoding="utf-8")
    final = OUT / "镜头运动对比_现状vs方案A.mp4"
    media.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-f", "concat", "-safe", "0", "-i", str(lst),
               "-c", "copy", str(final)], timeout=300)

    dur = media.probe(final).get("duration_s")
    print(f"\n完成：{final}")
    print(f"  时长 {dur}s  体积 {final.stat().st_size / 1048576:.1f}MB")
    print(f"  素材单段 {seg} 帧（约 5.7s）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
