"""文生图适配器 —— 多 provider。

用途：把「一句歌词对应的景区」变成一张**写实摄影质感**的配图。

支持的 provider：

| key | 厂商 | Key 环境变量 | 说明 |
|---|---|---|---|
| `wanx` | 通义万相（DashScope） | `LLM_API_KEY` | 和 LLM 同一个 Key，无需另申请 |
| `seedream` | Seedream / 即梦（火山方舟） | `ARK_API_KEY` | 写实感与中文场景理解更强，需自行申请 |

⚠️ 分辨率上限（实测）：万相 **宽或高必须落在 512–1440**，所以 16:9 最大只能
`1440*810`；出图后由 `media.image_to_clip` 放大到 1920×1080（并做一次锐化补偿）。
Seedream 支持 2K+，接上后画质会有明显提升，这也是接它的主要收益。

两个 provider 都统一在 `text_to_image()` 里按 `provider` 分派，
失败带退避重试（实测万相 5 张会挂 1–2 张）。
"""
from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import requests

from ..config import settings

POLL_INTERVAL = 3
POLL_MAX_S = 300

# 写实摄影风格后缀：不加这段，模型默认会偏「插画 / 概念图」，
# 而这正是用户反馈「配图很幼稚」的主因之一。
REALISM_SUFFIX = (
    "。写实纪实摄影，真实照片，实拍感，专业单反拍摄，35mm 定焦，"
    "自然光，真实色彩，层次丰富，细节清晰锐利，高分辨率，无文字无水印"
)
DEFAULT_NEGATIVE = (
    "插画,卡通,动漫,绘画,油画,水彩,3D渲染,CG,塑料感,过度磨皮,"
    "模糊,低清,噪点,畸变,文字,汉字,书法,题字,碑文,石刻文字,牌匾,招牌,对联,字母,水印,logo,"
    "多人,人脸特写,畸形"
)

PROVIDERS: dict[str, dict] = {
    "wanx": {
        "label": "通义万相",
        "model": "wanx2.1-t2i-plus",
        "size": "1440*810",
        "key_attr": "llm_api_key",
        "price_cny": 0.20,     # 元/张（官方刊例价），可用 WANX_IMAGE_PRICE 覆盖
    },
    "seedream": {
        "label": "Seedream 即梦",
        "model": "doubao-seedream-3-0-t2i-250415",
        "size": "2048x1152",
        "key_attr": "ark_api_key",
        "price_cny": 0.259,
    },
}


def price(key: str | None = None) -> float:
    """单张出图费用（元）。V1.0 出图不记费用，导致成本统计与每日上限都漏掉生图。"""
    key = key or default_provider()
    env = os.environ.get(f"{key.upper()}_IMAGE_PRICE", "").strip()
    try:
        return float(env) if env else float(_cfg(key)["price_cny"])
    except ValueError:
        return float(_cfg(key)["price_cny"])


def _cfg(key: str) -> dict:
    return PROVIDERS.get(key) or PROVIDERS["wanx"]


def default_provider() -> str:
    p = (os.environ.get("IMAGE_PROVIDER") or "").strip().lower()
    return p if p in PROVIDERS else "wanx"


def model_name(key: str | None = None) -> str:
    key = key or default_provider()
    env = os.environ.get(f"{key.upper()}_IMAGE_MODEL", "").strip()
    return env or _cfg(key)["model"]


def image_size(key: str | None = None) -> str:
    key = key or default_provider()
    env = os.environ.get(f"{key.upper()}_IMAGE_SIZE", "").strip()
    return env or _cfg(key)["size"]


def _key(key: str | None = None) -> str:
    key = key or default_provider()
    return getattr(settings(), _cfg(key)["key_attr"], "") or ""


def available(key: str | None = None) -> bool:
    """该 provider 是否能真实出图。"""
    return bool(_key(key)) and not settings().force_mock


def note(key: str | None = None) -> str:
    key = key or default_provider()
    cfg = _cfg(key)
    if not _key(key):
        env = "LLM_API_KEY" if key == "wanx" else "ARK_API_KEY"
        return f"未配置：填 {env} 后才能用于出图。"
    return f"{cfg['label']}（{model_name(key)}）：{image_size(key)}，按张计费。"


def list_providers() -> list[dict]:
    """给前端做「生图模型可选对比」用。"""
    return [
        {
            "key": key, "label": cfg["label"], "model": model_name(key),
            "size": image_size(key), "ready": available(key), "note": note(key),
        }
        for key, cfg in PROVIDERS.items()
    ]


def text_to_image(prompt: str, out_path: str | Path, *,
                  provider: str | None = None, size: str | None = None,
                  negative: str = DEFAULT_NEGATIVE,
                  timeout_s: int = POLL_MAX_S, attempts: int = 3,
                  project_id: str = "") -> Path:
    """生成一张图并保存到 out_path。失败抛 RuntimeError（带上游原文，便于定位）。

    `prompt` 传「画面内容」，写实风格后缀由本函数自动补 —— 调用方不用操心。
    """
    from ..observability import record

    key = provider or default_provider()
    if not available(key):
        env = "LLM_API_KEY" if key == "wanx" else "ARK_API_KEY"
        raise RuntimeError(f"{_cfg(key)['label']} 未就绪：需要 {env} 且 FORCE_MOCK=0")

    full = prompt if "写实纪实摄影" in prompt else (prompt.rstrip("。") + REALISM_SUFFIX)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    size = size or image_size(key)

    last: Exception | None = None
    for k in range(max(1, attempts)):
        t0 = time.time()
        try:
            if key == "seedream":
                _seedream(full, out_path, size=size, timeout_s=timeout_s)
            else:
                _wanx(full, out_path, size=size, negative=negative, timeout_s=timeout_s)
            # 只有成功出图才计费（失败的任务上游不收费）
            record(kind="image", provider=key, model=model_name(key), purpose="text_to_image",
                   params={"size": size}, status="ok", elapsed_s=time.time() - t0,
                   cost=price(key), project_id=project_id)
            return out_path
        except Exception as e:  # noqa: BLE001
            last = e
            msg = str(e)
            # 参数/鉴权类错误重试没意义
            if "HTTP 4" in msg and "429" not in msg:
                raise
            if k < attempts - 1:
                time.sleep(2 * (k + 1))
    raise RuntimeError(f"文生图连续 {attempts} 次失败：{last}")


# ------------------------------------------------------------ 通义万相（异步）
def _wanx(prompt: str, out_path: Path, *, size: str, negative: str,
          timeout_s: int) -> None:
    base = os.environ.get(
        "IMAGE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1").rstrip("/")
    headers = {
        "Authorization": f"Bearer {_key('wanx')}",
        "Content-Type": "application/json",
        "X-DashScope-Async": "enable",
    }
    body: dict = {
        "model": model_name("wanx"),
        "input": {"prompt": prompt[:1500]},
        "parameters": {"size": size, "n": 1},
    }
    if negative:
        body["input"]["negative_prompt"] = negative[:500]

    r = requests.post(f"{base}/services/aigc/text2image/image-synthesis",
                      headers=headers, json=body, timeout=90)
    if r.status_code >= 400:
        raise RuntimeError(f"万相提交失败 HTTP {r.status_code}: {r.text[:300]}")
    task_id = (r.json().get("output") or {}).get("task_id")
    if not task_id:
        raise RuntimeError(f"万相未返回 task_id：{r.text[:300]}")

    poll = {"Authorization": f"Bearer {_key('wanx')}"}
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        try:
            q = requests.get(f"{base}/tasks/{task_id}", headers=poll, timeout=45)
        except requests.RequestException:
            continue  # 轮询抖动不算失败，下一轮再问
        out = (q.json() or {}).get("output") or {}
        st = out.get("task_status")
        if st == "SUCCEEDED":
            results = out.get("results") or []
            url = (results[0].get("url") if results else "") or ""
            if not url:
                raise RuntimeError("万相成功但未返回图片 URL")
            _fetch(url, out_path)
            return
        if st in ("FAILED", "CANCELED", "UNKNOWN"):
            raise RuntimeError(f"万相失败（{st}）：{str(out)[:300]}")
    raise RuntimeError(f"万相超时（>{timeout_s}s）")


# ------------------------------------------------------------ Seedream（同步）
def _seedream(prompt: str, out_path: Path, *, size: str, timeout_s: int) -> None:
    """火山方舟 Seedream，同步返回（url 或 b64_json）。

    注：**未拿到 Key 前无法实测**，字段按官方文档写。
    真接上后若报参数错误，优先核对 `SEEDREAM_IMAGE_MODEL` 与尺寸格式
    （Seedream 用 `1024x1024` 这种 x 分隔，且 3.0 只支持固定几档尺寸）。
    """
    base = os.environ.get(
        "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    headers = {
        "Authorization": f"Bearer {_key('seedream')}",
        "Content-Type": "application/json",
    }
    body = {
        "model": model_name("seedream"),
        "prompt": prompt[:1500],
        "size": size,
        "response_format": "url",
        "watermark": False,
    }
    r = requests.post(f"{base}/images/generations", headers=headers, json=body,
                      timeout=max(120, min(timeout_s, 600)))
    if r.status_code >= 400:
        raise RuntimeError(f"Seedream 失败 HTTP {r.status_code}: {r.text[:300]}")
    data = (r.json() or {}).get("data") or []
    if not data:
        raise RuntimeError(f"Seedream 未返回图片：{r.text[:300]}")
    item = data[0]
    if item.get("b64_json"):
        out_path.write_bytes(base64.b64decode(item["b64_json"]))
        return
    url = item.get("url")
    if not url:
        raise RuntimeError(f"Seedream 未返回 url：{str(item)[:200]}")
    _fetch(url, out_path)


def _fetch(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
    if dest.stat().st_size < 4096:
        raise RuntimeError(f"下载到的图片过小（{dest.stat().st_size}B），可能不是有效图片")
