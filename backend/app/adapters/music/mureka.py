"""Mureka 音乐生成适配器。

⚠️ 请求/响应字段名按公开文档编写，W1 选型测试时必须对照官方文档核对一次。
任何调用失败都由上层捕获并通过 base.resolve() 降级到 Mock，不会中断流程。

官方 API 定价（platform.mureka.ai/pricing，2026-09 核价，**按首计价**）：
- 歌词转歌：V7.6 $0.03/首；V8 / V9 / O2 $0.045/首；9.5 $0.15/首
- 歌词生成：$0.009/首（≤2000 字）；$0.002/行
- 最低充值 **$10**（对应 1 并发）；$1000 → 5 并发；$3000 → 15 并发
  （并发按「单笔充值额」计，充 5 次 $1000 仍是 5 并发，不是 25）
- 官方明示：付费 API 调用产出自带完整商用授权
- 单曲最长 5m30s；一次调用最多出 3 首（默认 2 首）
"""
from __future__ import annotations

import os
import threading
import time

import requests

from ...config import settings
from ..media import download
from .base import GenRequest, GenResult, MusicAdapter

# 各模型单价（美元/首）。键名必须与 API 的 model 取值一致（官方文档 valid values）：
#   auto | mureka-7.6 | mureka-8 | mureka-9 | mureka-9.5
# ⚠️ auto 的含义是「自动选择最新的 regular 模型」，实测落到 9.5 —— 也就是最贵那档。
#    2026-09-27 实测账单：一次歌词生曲扣 $0.15，印证 auto → mureka-9.5。
#    所以 auto 绝不能按便宜档估价，否则成本显示会低 3 倍多。
_DEFAULT_PRICE_USD = {
    "AUTO": 0.15,
    "MUREKA-7.6": 0.03,
    "MUREKA-8": 0.045,
    "MUREKA-9": 0.045,
    "MUREKA-9.5": 0.15,
}


def _price_for(model: str) -> float:
    """按档位取单价（美元/首）。auto 按最贵估 —— 它实际会选最新的 9.5。"""
    return _DEFAULT_PRICE_USD.get((model or "").upper(), 0.15)


def _concurrency() -> int:
    """账号实际并发额度。$10 起充档只有 1 并发，按充值额递增（$1000 → 5）。"""
    try:
        return max(1, int(os.environ.get("MUREKA_CONCURRENCY", "1")))
    except ValueError:
        return 1


class MurekaAdapter(MusicAdapter):
    key = "mureka"
    name = "Mureka"
    run_mode = "api"
    price_per_min = 0.0  # 按首计价，见 estimate_cost

    def __init__(self, key: str = "mureka", model: str | None = None,
                 label: str | None = None) -> None:
        """一个实例固定一个 model 档位，可注册多个形成「版本可选」。

        这样界面上就能像切换音乐模型一样切换 Mureka 的版本（7.6 / 9 / 9.5），
        试听完一档直接换另一档，不必改 .env 再重启。
        model=None 表示跟随 .env 的 MUREKA_MODEL。
        """
        super().__init__()
        self.key = key
        self.name = label or "Mureka"
        self._model = model
        # Mureka 的并发额度是**按单笔充值额**给的（$10 → 1，$1000 → 5）。
        # 本项目 api 队列默认并发 4，若不在这里收口，4 个生曲任务同时打过去会有一半吃 429，
        # 重试 3 次仍失败就整条候选挂掉。用信号量把实际并发压到账号额度以内。
        self._gate = threading.Semaphore(_concurrency())

    @property
    def model_name(self) -> str:
        """实际发给 API 的 model 值。"""
        return self._model or settings().mureka_model or "auto"

    @property
    def ready(self) -> bool:
        s = settings()
        return bool(s.mureka_api_key) and not s.force_mock

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {settings().mureka_api_key}"}

    def capabilities(self) -> dict:
        return {
            "supports_chinese": True,       # 待 W1 实听确认咬字质量
            "max_duration_s": 330,          # 官方上限 5m30s
            "supports_custom_lyrics": True,
            "concurrency": _concurrency(),
            "max_songs_per_call": 3,
        }

    def estimate_cost(self, req: GenRequest) -> float:
        # 按首计价，与时长无关；单价取决于该实例固定的版本档位
        return round(_price_for(self.model_name) * settings().usd_cny, 4)

    def _request(self, method: str, url: str, **kw) -> requests.Response:
        """受并发额度约束 + 429 退避重试（最多 4 次，1/2/4/8 秒）。"""
        r: requests.Response | None = None
        for attempt in range(4):
            with self._gate:
                r = requests.request(method, url, **kw)
            if r.status_code != 429:
                return r
            time.sleep(2 ** attempt)  # 429 = 超出并发额度，退避后再试
        return r  # type: ignore[return-value]

    def generate(self, req: GenRequest) -> str:
        s = settings()
        body = {
            "lyrics": req.lyrics,
            "prompt": req.style_prompt,
            "model": self.model_name,
            "n": 1,
        }
        if req.seed is not None:
            body["seed"] = req.seed
        r = self._request("POST", f"{s.mureka_base_url}/v1/song/generate",
                          headers=self._headers(), json=body, timeout=120)
        r.raise_for_status()
        job_id = str(r.json().get("id") or r.json().get("job_id") or "")
        if not job_id:
            raise RuntimeError(f"Mureka 未返回任务标识：{r.text[:300]}")
        self._jobs[job_id] = req
        return job_id

    def get_result(self, job_id: str) -> GenResult:
        s = settings()
        t0 = time.time()
        r = self._request("GET", f"{s.mureka_base_url}/v1/song/query/{job_id}",
                          headers=self._headers(), timeout=60)
        r.raise_for_status()
        data = r.json()
        status = str(data.get("status", "")).lower()
        if status in ("running", "queued", "pending", "preparing"):
            return GenResult(status="running", elapsed_s=time.time() - t0, raw_meta=data)
        if status in ("failed", "error"):
            return GenResult(status="failed", error=str(data)[:500], raw_meta=data)
        choices = data.get("choices") or []
        if not choices:
            return GenResult(status="running", elapsed_s=time.time() - t0, raw_meta=data)
        url = choices[0].get("url") or choices[0].get("audio_url") or ""
        req = self._jobs.get(job_id)
        dest = settings().data_dir / "music" / f"mureka-{job_id}.mp3"
        download(url, dest)
        # ⚠️ Mureka 返回的 duration 是**毫秒**（实测 169430 对应 169.43 秒）。
        # 直接当秒用会让时间轴整体放大 1000 倍（歌词被摊到 21 小时），
        # 表现为「音画完全对不上、成片只有开头一小段有内容」。
        raw_dur = float(choices[0].get("duration") or 0)
        if raw_dur > 1000:          # 一首歌不可能超过 1000 秒，超过即判定为毫秒
            raw_dur = raw_dur / 1000.0
        fallback = float(req.duration_s) if req else 0.0
        return GenResult(
            status="done", audio_path=str(dest),
            duration_s=raw_dur or fallback,
            cost=self.estimate_cost(req) if req else 0.0,
            elapsed_s=time.time() - t0, raw_meta=data,
        )

    def note(self) -> str:
        s = settings()
        price = _price_for(self.model_name) * s.usd_cny
        if not s.mureka_api_key:
            return "未配置：填 MUREKA_API_KEY（platform.mureka.ai，最低充值 $10）。"
        return (f"官方 API（{self.model_name}）：约 ¥{price:.2f}/首，最长 5m30s，"
                f"官方明示付费调用含商用授权；充值 $10 起（对应 1 并发）。")
