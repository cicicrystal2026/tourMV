"""文生图适配器 —— 多 provider。

用途：把「一句歌词对应的景区」变成一张**写实摄影质感**的配图。

支持的 provider：

| key | 厂商 | Key 环境变量 | 说明 |
|---|---|---|---|
| `wanx` | 通义万相（DashScope） | `LLM_API_KEY` | 和 LLM 同一个 Key，无需另申请 |
| `seedream` | 豆包 Seedream 5.0（火山方舟） | `ARK_API_KEY` | 写实感与中文场景理解更强，支持 2K |
| `jimeng` | 即梦 AI 图片生成（火山引擎视觉智能） | `VOLC_ACCESS_KEY` + `VOLC_SECRET_KEY` | 即梦同源模型，AK/SK 签名调用 |

⚠️ 分辨率上限（实测）：万相 **宽或高必须落在 512–1440**，所以 16:9 最大只能
`1440*810`；`media.image_to_clip` 会把它放大到 1920×1080 并做一次锐化补偿。
Seedream 直接给 2560×1440（16:9 的 2K），落到 1920×1080 是**缩小**而非放大，
不会再有放大带来的柔化 —— 这是接它的主要收益。

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
        "label": "豆包 Seedream",
        # 5.0 flash：快、便宜、2K（pro 是 doubao-seedream-5-0-pro-260628，画质更高）。
        # ⚠️ 5.0 的 size 只有两种写法，**不可混用**：
        #     档位词 1K / 1.5K / 2K（默认 2K），或像素值 `宽x高`
        #     （总像素须落在 921600~4624220、宽高比须落在 1/16~16）。
        #   并且**没有 aspect_ratio 参数** —— 所以想要 16:9 横版配图，
        #   必须传像素值；只传 `2K` 会拿到模型自己的默认比例（偏方）。
        # 模型 ID 以方舟控制台「开通管理」为准，可用 SEEDREAM_IMAGE_MODEL 覆盖。
        "model": "doubao-seedream-5-0-flash-260915",
        "size": "2560x1440",     # 16:9，3,686,400 像素，落在官方允许区间内
        "key_attr": "ark_api_key",
        "env": "ARK_API_KEY",
        "price_cny": 0.21,       # flash 官方 $0.03/张（约 ¥0.21）；以控制台刊例价为准
    },
    "jimeng": {
        "label": "即梦",
        # 火山引擎视觉智能「即梦AI-图片生成」：model 字段即 req_key，
        # 可用 JIMENG_IMAGE_MODEL 覆盖（如 jimeng_t2i_v31）
        "model": "jimeng_t2i_v40",
        "size": "2560x1440",
        "key_attr": "volc_access_key",
        "env": "VOLC_ACCESS_KEY + VOLC_SECRET_KEY",
        "price_cny": 0.20,
    },
}
PROVIDERS["wanx"]["env"] = "LLM_API_KEY"


def _env_hint(key: str) -> str:
    return _cfg(key).get("env", "")


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
    v = getattr(settings(), _cfg(key)["key_attr"], "") or ""
    if key == "jimeng" and not settings().volc_secret_key:
        return ""     # 即梦要 AK、SK 两个都有
    return v


def available(key: str | None = None) -> bool:
    """该 provider 是否能真实出图。"""
    return bool(_key(key)) and not settings().force_mock


def note(key: str | None = None) -> str:
    key = key or default_provider()
    cfg = _cfg(key)
    if not _key(key):
        return f"未配置：填 {_env_hint(key)} 后才能用于出图。"
    return f"{cfg['label']}（{model_name(key)}）：{image_size(key)}，按张计费。"


def list_providers() -> list[dict]:
    """给前端做「生图模型可选对比」用。"""
    return [
        {
            "key": key, "label": cfg["label"], "model": model_name(key),
            "size": image_size(key), "ready": available(key), "note": note(key),
            "price": price(key),
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
        raise RuntimeError(f"{_cfg(key)['label']} 未就绪：需要 {_env_hint(key)} 且 FORCE_MOCK=0")

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
            elif key == "jimeng":
                _jimeng(full, out_path, size=size, timeout_s=timeout_s)
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
def _seedream_gen(model: str) -> int:
    """从模型 ID 取主版本号：doubao-seedream-5-0-flash-260915 → 5。

    用来决定「哪些参数能传」——各家版本支持面不一样，多传一个就 400。
    """
    import re

    m = re.search(r"seedream-(\d+)", model or "")
    return int(m.group(1)) if m else 4


def _seedream(prompt: str, out_path: Path, *, size: str, timeout_s: int) -> None:
    """火山方舟 Seedream，同步返回（url 或 b64_json）。

    版本差异（官方文档口径，很容易踩）：
    - **`sequential_image_generation`（组图）**：仅 **4.x** 支持。3.x 没有，
      **5.0 pro / flash 官方明确不支持，传了直接 400**。所以按主版本号精确传。
    - **`stream`（流式）**：5.0 同样不支持，不要带。
    - **`watermark`**：接口默认 **true**，必须显式设 false 才有干净图。
    - **`size`**：5.0 只有「档位词 1K/1.5K/2K」或「像素值 宽x高」两种写法且不可混用；
      **没有 aspect_ratio 参数**，要 16:9 就得传像素值。
    """
    base = os.environ.get(
        "ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")
    url = f"{base}/images/generations"
    headers = {
        "Authorization": f"Bearer {_key('seedream')}",
        "Content-Type": "application/json",
    }
    model = model_name("seedream")
    body: dict = {
        "model": model,
        "prompt": prompt[:1500],
        "size": size,
        "response_format": "url",
        "watermark": False,          # 默认是 true，不关会带水印
    }
    if _seedream_gen(model) == 4:
        body["sequential_image_generation"] = "disabled"   # 只要一张

    timeout = max(120, min(timeout_s, 600))
    r = requests.post(url, headers=headers, json=body, timeout=timeout)

    # 参数类 400：逐个剥掉可选参数再试 —— 「多带一个参数就整张图出不来」
    # 在跨版本时太常见，不值得让人手工排查。
    if r.status_code == 400:
        first_err = r.text[:300]
        retried = False
        for drop in ("sequential_image_generation", "watermark"):
            if drop not in body:
                continue
            slim = {k: v for k, v in body.items() if k != drop}
            r2 = requests.post(url, headers=headers, json=slim, timeout=timeout)
            if r2.status_code < 400:
                r, retried = r2, True
                break
        if not retried:
            raise RuntimeError(f"Seedream HTTP 400（参数不被接受）：{first_err}")

    if r.status_code >= 400:
        raise RuntimeError(f"Seedream 失败 HTTP {r.status_code}: {r.text[:300]}")
    data = (r.json() or {}).get("data") or []
    if not data:
        raise RuntimeError(f"Seedream 未返回图片：{r.text[:300]}")
    item = data[0]
    if item.get("b64_json"):
        out_path.write_bytes(base64.b64decode(item["b64_json"]))
        return
    url_img = item.get("url")
    if not url_img:
        raise RuntimeError(f"Seedream 未返回 url：{str(item)[:200]}")
    _fetch(url_img, out_path)


# ------------------------------------------------------------ 即梦（火山引擎视觉智能，异步）
_VOLC_HOST = "visual.volcengineapi.com"
_VOLC_REGION = "cn-north-1"
_VOLC_SERVICE = "cv"
_VOLC_VERSION = "2022-08-31"


def _volc_sign(ak: str, sk: str, host: str, query: dict, payload: bytes, x_date: str) -> dict:
    """火山引擎 OpenAPI 的 HMAC-SHA256 V4 签名（与官方 SDK 算法一致），返回请求头。"""
    import hashlib
    import hmac
    from urllib.parse import quote

    short_date = x_date[:8]
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(str(v), safe='-_.~')}"
                               for k, v in sorted(query.items()))
    body_hash = hashlib.sha256(payload).hexdigest()
    signed_headers = "content-type;host;x-content-sha256;x-date"
    canonical_headers = (f"content-type:application/json\nhost:{host}\n"
                         f"x-content-sha256:{body_hash}\nx-date:{x_date}\n")
    canonical_request = "\n".join(["POST", "/", canonical_query, canonical_headers,
                                   signed_headers, body_hash])
    scope = f"{short_date}/{_VOLC_REGION}/{_VOLC_SERVICE}/request"
    string_to_sign = "\n".join(["HMAC-SHA256", x_date, scope,
                                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()])

    def _h(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()

    k = _h(sk.encode("utf-8"), short_date)
    for part in (_VOLC_REGION, _VOLC_SERVICE, "request"):
        k = _h(k, part)
    signature = hmac.new(k, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "Host": host,
        "X-Date": x_date,
        "X-Content-Sha256": body_hash,
        "Authorization": (f"HMAC-SHA256 Credential={ak}/{scope}, "
                          f"SignedHeaders={signed_headers}, Signature={signature}"),
        "_query": canonical_query,
    }


def _volc_call(action: str, body: dict, timeout: int = 60) -> dict:
    import datetime
    import json as _json

    s = settings()
    host = os.environ.get("VOLC_VISUAL_HOST", _VOLC_HOST)
    payload = _json.dumps(body, ensure_ascii=False).encode("utf-8")
    x_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers = _volc_sign(s.volc_access_key, s.volc_secret_key, host,
                         {"Action": action, "Version": _VOLC_VERSION}, payload, x_date)
    query = headers.pop("_query")
    r = requests.post(f"https://{host}/?{query}", headers=headers, data=payload, timeout=timeout)
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"即梦 HTTP {r.status_code}: {r.text[:300]}") from None
    err = (j.get("ResponseMetadata") or {}).get("Error") or {}
    if r.status_code >= 400 or err or j.get("code") not in (None, 10000):
        msg = err.get("Message") or j.get("message") or r.text[:300]
        raise RuntimeError(f"即梦 HTTP {r.status_code}: {j.get('code', '')} {msg}"[:400])
    return j.get("data") or {}


def _jimeng(prompt: str, out_path: Path, *, size: str, timeout_s: int) -> None:
    """即梦 AI 图片生成：提交任务 → 轮询结果（图片 URL 或 base64）。

    注：**未拿到 AK/SK 前无法实测**，字段按官方文档写；报参数错误时先核对
    `JIMENG_IMAGE_MODEL`（即 req_key，须是账号已开通的能力）与尺寸。
    """
    req_key = model_name("jimeng")
    w, _, h = size.lower().partition("x")
    task = _volc_call("CVSync2AsyncSubmitTask", {
        "req_key": req_key, "prompt": prompt[:800],
        "width": int(w or 2560), "height": int(h or 1440),
    })
    task_id = task.get("task_id")
    if not task_id:
        raise RuntimeError(f"即梦未返回 task_id：{str(task)[:200]}")
    deadline = time.time() + max(60, timeout_s)
    while time.time() < deadline:
        time.sleep(POLL_INTERVAL)
        d = _volc_call("CVSync2AsyncGetResult", {
            "req_key": req_key, "task_id": task_id,
            "req_json": '{"return_url":true,"logo_info":{"add_logo":false}}',
        })
        st = d.get("status")
        if st == "done":
            urls = d.get("image_urls") or []
            if urls:
                _fetch(urls[0], out_path)
                return
            b64 = d.get("binary_data_base64") or []
            if b64:
                out_path.write_bytes(base64.b64decode(b64[0]))
                return
            raise RuntimeError("即梦任务完成但没有返回图片（可能被内容审核拦截）")
        if st in ("not_found", "expired"):
            raise RuntimeError(f"即梦任务 {st}")
    raise RuntimeError(f"即梦出图超时（>{timeout_s}s）")


def _fetch(url: str, dest: Path) -> None:
    with requests.get(url, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
    if dest.stat().st_size < 4096:
        raise RuntimeError(f"下载到的图片过小（{dest.stat().st_size}B），可能不是有效图片")
