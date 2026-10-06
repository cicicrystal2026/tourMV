"""全局配置。所有外部依赖通过 .env 注入，不写死在代码里。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"
ENV_FILE = PROJECT_ROOT / ".env"


def _load_env() -> None:
    """极简 .env 解析：与 monitor.py 的 load_env 行为保持一致（已存在的环境变量优先）。"""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def csv_list(s: str | None) -> list[str]:
    return [x.strip() for x in (s or "").split(",") if x.strip()]


class Settings:
    def __init__(self) -> None:
        _load_env()
        self.project_root = PROJECT_ROOT
        # DATA_DIR 可覆盖：自动化测试指向临时目录，避免污染真实数据
        self.data_dir = Path(os.environ.get("DATA_DIR") or DATA_DIR)
        self.db_path = self.data_dir / "wenlv.db"
        # 前端构建产物。存在时由本进程单端口托管（线上环境沙箱只给一个端口）；
        # 本地开发时不放这个目录，前端仍走 vite dev server，互不影响。
        self.webapp_dir = Path(os.environ.get("WEBAPP_DIR", PROJECT_ROOT / "backend" / "webapp"))
        self.monitor_db = Path(os.environ.get("MONITOR_DB", DATA_DIR / "trends.db"))
        # 成片等大体积媒体的回源地址。发布包不带成片（一条 MV 五个版本上百 MB，
        # 塞进包里上传体量直接顶爆），线上后端本地找不到文件时回源到这里。
        # 留空 = 关闭回源（本地开发不需要）。
        self.media_fallback_base = os.environ.get("MEDIA_FALLBACK_BASE", "").rstrip("/")
        self.monitor_script = Path(
            os.environ.get("MONITOR_SCRIPT", PROJECT_ROOT / "backend" / "monitor" / "monitor.py")
        )
        self.monitor_reports = Path(
            os.environ.get("MONITOR_REPORTS", PROJECT_ROOT / "data" / "trends_reports")
        )

        # 大语言模型（OpenAI 兼容）
        self.llm_base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        self.llm_api_key = os.environ.get("LLM_API_KEY", "")
        self.llm_model = os.environ.get("LLM_MODEL", "qwen-plus")

        # 音乐模型
        self.mureka_api_key = os.environ.get("MUREKA_API_KEY", "")
        self.mureka_base_url = os.environ.get("MUREKA_BASE_URL", "https://api.mureka.ai").rstrip("/")
        self.eleven_api_key = os.environ.get("ELEVEN_API_KEY", "")
        self.eleven_base_url = os.environ.get("ELEVEN_BASE_URL", "https://api.elevenlabs.io").rstrip("/")
        self.eleven_music_model = os.environ.get("ELEVEN_MUSIC_MODEL", "music_v2")
        # Mureka 按首计价（不同模型单价不同，见适配器注释）
        self.mureka_model = os.environ.get("MUREKA_MODEL", "auto")
        # Google Lyria（Gemini API）—— 按首计价，$0.08/首
        self.gemini_api_key = os.environ.get("GEMINI_API_KEY", "")
        self.gemini_base_url = os.environ.get(
            "GEMINI_BASE_URL", "https://generativelanguage.googleapis.com").rstrip("/")
        self.lyria_model = os.environ.get("LYRIA_MODEL", "lyria-3.5")
        # 文生图（逐句配图）。provider 可选 wanx（通义万相，复用 LLM_API_KEY）
        # 或 seedream（火山方舟 Seedream，需 ARK_API_KEY）。
        self.image_provider = os.environ.get("IMAGE_PROVIDER", "wanx")
        self.ark_api_key = os.environ.get("ARK_API_KEY", "")
        # 即梦（火山引擎视觉智能）用账号 AK/SK 签名，不是方舟的 API Key
        self.volc_access_key = os.environ.get("VOLC_ACCESS_KEY", "")
        self.volc_secret_key = os.environ.get("VOLC_SECRET_KEY", "")
        # 逐句出图是长任务（一句一张），并行几张能显著缩短等待
        self.image_concurrency = int(os.environ.get("IMAGE_CONCURRENCY", "3"))
        # 汇率（用于把各家美元单价折成人民币做成本估算）
        self.usd_cny = float(os.environ.get("USD_CNY", "7.2"))
        # MiniMax：官方开放平台（api.minimaxi.com 国内站 / api.minimax.io 海外站）
        self.minimax_api_key = os.environ.get("MINIMAX_API_KEY", "")
        self.minimax_api_host = os.environ.get("MINIMAX_API_HOST", "https://api.minimaxi.com").rstrip("/")
        self.minimax_music_model = os.environ.get("MINIMAX_MUSIC_MODEL", "music-1.5")
        # MiniMax：自建推理服务（本地 4090 部署开源模型）
        self.minimax_local_endpoint = os.environ.get("MINIMAX_LOCAL_ENDPOINT", "")

        # 视觉 / 向量 / 对齐
        self.vlm_base_url = os.environ.get("VLM_BASE_URL", "").rstrip("/")
        self.vlm_api_key = os.environ.get("VLM_API_KEY", "")
        self.vlm_model = os.environ.get("VLM_MODEL", "qwen-vl-plus")
        self.clip_model = os.environ.get("CLIP_MODEL", "OFA-Sys/chinese-clip-vit-base-patch16")
        # 实拍镜头「合格」的匹配分阈值（PRD V1.1 F6-2，标定后的 0–1 分）
        self.match_threshold = float(os.environ.get("MATCH_THRESHOLD", "0.6"))
        # 歌词强制对齐：auto（有 faster-whisper 就用，否则估算）| whisper（强制用）| analytic（关闭）
        # 历史值 mock / whisperx / funasr 一并兼容：mock=关闭，其余=开启。
        self.align_engine = (os.environ.get("ALIGN_ENGINE", "auto") or "auto").strip().lower()
        self.align_model = os.environ.get("ALIGN_MODEL", "small").strip() or "small"
        # 模型下载镜像。Xet 分发通道在代理/内网下极易握手超时，代码里会强制走经典 CDN。
        self.hf_endpoint = (os.environ.get("HF_ENDPOINT", "https://hf-mirror.com")
                            or "https://hf-mirror.com").rstrip("/")

        # 队列并发（PRD §10.1：api 4 / gpu 1）
        self.api_concurrency = int(os.environ.get("API_CONCURRENCY", "4"))
        self.gpu_concurrency = int(os.environ.get("GPU_CONCURRENCY", "1"))

        # 运行模式：mock 时所有外部调用走本地假实现
        self.force_mock = os.environ.get("FORCE_MOCK", "1") == "1"
        # PRD V1.1 §11 数据隔离：是否允许用 mock 占位结果。
        # 生产环境必须关掉 —— 否则未配 Key 的模型会静默产出占位音频，混进候选与统计。
        # 全局 FORCE_MOCK 时自动允许（那本来就是「整站演示」模式）。
        self.allow_mock = self.force_mock or os.environ.get("ALLOW_MOCK", "0") == "1"
        # 只让音乐走 mock（其余真实调用）。测试环境要看真实出图效果、又不想误触发付费生曲时用
        self.music_force_mock = os.environ.get("MUSIC_FORCE_MOCK", "0") == "1"

        # PRD V1.1 §11 安全：访问口令。留空 = 不启用（仅限本机开发）。
        # 线上部署必须配置，否则任何人都能读数据、触发付费生成。
        self.access_password = os.environ.get("ACCESS_PASSWORD", "")
        # 登录 cookie 的签名密钥；不配则由口令派生（换口令即全部失效）
        self.session_secret = os.environ.get("SESSION_SECRET", "") or (
            "wenlv:" + self.access_password if self.access_password else "")
        # 生成类接口的每日费用上限（元）。0 = 不限。按服务器本地日期累计。
        self.daily_cost_limit = float(os.environ.get("DAILY_COST_LIMIT", "30") or 0)

        # PRD V1.1 §7.2 选型轮参赛模型（盲听对比只在这些模型间进行）
        self.selection_model_keys = csv_list(
            os.environ.get("SELECTION_MODEL_KEYS", "mureka,lyria,minimax"))

    @property
    def llm_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key) and not self.force_mock

    @property
    def align_enabled(self) -> bool:
        """是否启用强制对齐。'auto' / 'whisper' / 'whisperx' / 'funasr' 都算开启。"""
        return self.align_engine not in ("analytic", "mock", "off", "none", "")

    @property
    def minimax_mode(self) -> str:
        """MiniMax 取用方式：cloud（官方 API，优先）/ local（自建服务）/ none（未配置）。

        只看凭证是否配置，不看 FORCE_MOCK —— 强制 Mock 是全局开关，
        由适配器的 ready 负责降级，这里要反映「你打算用哪种方式」。
        """
        if self.minimax_api_key:
            return "cloud"
        if self.minimax_local_endpoint:
            return "local"
        return "none"

    def ensure_dirs(self) -> None:
        # storyboard: 逐句配图（审核页要在线上看图，所以不能放进会被移出发布的 images/）
        for sub in ("assets", "assets/storyboard", "storyboard", "thumbs",
                    "music", "renders", "trends_reports"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def settings() -> Settings:
    s = Settings()
    s.ensure_dirs()
    return s
