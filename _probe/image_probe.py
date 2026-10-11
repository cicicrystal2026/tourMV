"""文生图 provider 自检 / 诊断。

用途：刚申请到火山引擎 / 方舟的 Key 时先跑这个，避免在「逐句配图」流程里
跑到一半才发现 401（鉴权）/ 404（模型未开通）/ 400（参数或尺寸不被接受）。

对 seedream 是**先走项目代码链路**（`adapters/image.text_to_image` → `_seedream`），
成功只花一次出图的钱；**只有失败时**才补一发直连最小请求做二分定位，
把「凭证 / 模型 ID」与「参数」两类问题分开。

用法：
    python _probe/image_probe.py                # 测 .env 里 IMAGE_PROVIDER 指定的那个
    python _probe/image_probe.py seedream       # 指定 provider：wanx / seedream / jimeng
    python _probe/image_probe.py seedream 2K    # 再指定尺寸（默认用 provider 配置里的）

⚠️ 会真实调用上游接口出图，产生费用（约 ¥0.2/张量级）。
"""
from __future__ import annotations

import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import requests                                # noqa: E402

from app.adapters import image as img          # noqa: E402

PROMPT = ("吉林省长白山天池，冬季雪原，火山口湖面结冰，远处十六峰环抱，"
          "清晨冷蓝色调，云雾缭绕")
OUT = ROOT / "data" / "_probe_t2i"

HINT = {
    401: "鉴权失败。确认用的是方舟「API Key 管理」创建的 API Key（Bearer 认证），"
         "不是 IAM 的 AccessKey/SecretKey。",
    403: "无权限。检查是否已完成实名认证，以及该模型是否已在「开通管理」里开通。",
    404: "模型不存在。把控制台里的 Model ID 原样填进 SEEDREAM_IMAGE_MODEL。",
    429: "触发限流。调低 IMAGE_CONCURRENCY，或稍后重试。",
}


def _report(code: int, text: str, tag: str) -> bool:
    mark = "✅" if code < 400 else "❌"
    print(f"  {mark} [{tag}] HTTP {code}  {text[:260]}")
    if code >= 400:
        tip = HINT.get(code)
        if tip:
            print(f"     → {tip}")
    return code < 400


def diagnose_seedream(prov: str, size: str, dest: pathlib.Path) -> int:
    """验证项目代码链路；失败时才直连二分，避免把「参数问题」误判成「Key 问题」。"""
    import os

    key = img._key(prov)
    print("\n【步骤 1】检查凭证")
    if not key:
        print("  ❌ 没读到 API Key。请在 wenlv-mv/.env 里填 ARK_API_KEY")
        return 1
    print(f"  ✅ 已读到 Key：{key[:8]}…{key[-4:]}（长度 {len(key)}）")
    if len(key) < 20:
        print("  ⚠️  Key 偏短，确认没有被截断。")

    print(f"\n【步骤 2】走项目代码出图"
          f"（模型 {img.model_name(prov)}，size={size}）")
    t0 = time.time()
    try:
        img.text_to_image(PROMPT, dest, provider=prov, size=size)
    except Exception as e:                       # noqa: BLE001
        print(f"  ❌ 失败：{str(e)[:300]}")
        base = os.environ.get(
            "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
        print("\n  【二分定位】直连发最小请求（只带 model + prompt）…")
        r2 = requests.post(
            f"{base}/images/generations",
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json"},
            timeout=180,
            json={"model": img.model_name(prov), "prompt": PROMPT,
                  "response_format": "url"})
        if r2.status_code < 400:
            print("  → 最小请求**成功**：Key 与模型 ID 都没问题，问题出在可选参数。")
            print("     5.0 的 size 只接受 1K/1.5K/2K 或「宽x高」像素值，不可混用；")
            print("     且 5.0 不支持 sequential_image_generation / stream。")
        else:
            _report(r2.status_code, r2.text, "最小请求")
        return 1

    print("  ✅ 项目链路出图成功")
    print(f"\n✅ 通过  尺寸={size}  耗时={time.time() - t0:.1f}s  "
          f"体积={dest.stat().st_size / 1024:.0f}KB")
    print(f"   样图：{dest}")
    return 0


def main() -> int:
    prov = (sys.argv[1] if len(sys.argv) > 1 else img.default_provider()).strip()
    if prov not in img.PROVIDERS:
        print(f"未知 provider：{prov}（可选：{' / '.join(img.PROVIDERS)}）")
        return 2
    size = sys.argv[2].strip() if len(sys.argv) > 2 else img.image_size(prov)

    cfg = img._cfg(prov)
    print("=" * 70)
    print(f"provider : {prov}  ({cfg['label']})")
    print(f"模型 ID  : {img.model_name(prov)}")
    print(f"尺寸     : {size}")
    print(f"凭证字段 : {img._env_hint(prov)}")
    print(f"就绪     : {'是' if img.available(prov) else '否'}")
    print("=" * 70)

    if not img.available(prov):
        print(f"\n❌ 未就绪 —— 请在 wenlv-mv/.env 里填好 {img._env_hint(prov)}，"
              "确认 FORCE_MOCK=0，然后重启。")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    dest = OUT / f"{prov}_{time.strftime('%m%d_%H%M%S')}.jpg"

    if prov == "seedream":
        return diagnose_seedream(prov, size, dest)

    t0 = time.time()
    try:
        img.text_to_image(PROMPT, dest, provider=prov, size=size)
    except Exception as e:                       # noqa: BLE001
        print(f"\n❌ 失败：{str(e)[:400]}")
        return 1
    print(f"\n✅ 出图成功  尺寸={size}  耗时={time.time() - t0:.1f}s  "
          f"体积={dest.stat().st_size / 1024:.0f}KB")
    print(f"   样图：{dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
