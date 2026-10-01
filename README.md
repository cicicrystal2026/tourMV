# 文旅MV生产管线 V1.0

内部生产工作台。对应 PRD《文旅MV生产管线_V1.0_PRD》的 MVP 范围：
**热歌监控 → 创作简报 → AI 歌词 → 多模型生曲与评分选优 → 镜头匹配与卡点粗剪 → 精剪 → 多版本导出**，
全部在一个系统内完成。

## 目录结构

```
wenlv-mv/
├── backend/
│   ├── app/
│   │   ├── main.py              FastAPI 入口（含 /media 静态挂载）
│   │   ├── config.py            .env 读取与数据目录
│   │   ├── db.py                SQLAlchemy 会话（SQLite + WAL）
│   │   ├── models.py            16 张表（PRD §8）
│   │   ├── state_machine.py     10 态状态机（PRD §4.2）
│   │   ├── queue.py             SQLite 任务队列（api 4 / gpu 1）
│   │   ├── serialize.py         模型 → 前端 JSON
│   │   ├── observability.py     AI 调用记账
│   │   ├── seed.py              一键初始化 / 自检
│   │   ├── api/                 路由（实测 52 条，覆盖 PRD §9 的 29 个接口）
│   │   ├── services/            业务编排
│   │   │   ├── prompts.py       全部 LLM 提示词 + 本地兜底载荷
│   │   │   ├── trends.py        monitor.py 接入
│   │   │   ├── lyrics.py music.py edit.py assets.py render.py
│   │   │   ├── dashboard.py projects.py
│   │   ├── adapters/            外部能力适配（统一接口 + 兜底实现）
│   │   │   ├── llm.py vlm.py clip.py align.py media.py
│   │   │   └── music/           base.py mock/synthesis.py mureka.py eleven.py minimax_local.py
│   │   └── workers/tasks.py     后台任务处理器
│   ├── monitor/monitor.py       原有的热歌监控脚本（原样放置，未做任何修改）
│   ├── requirements.txt         核心依赖（装完即可跑通）
│   └── requirements-optional.txt 可选依赖（真模型）
├── frontend/                    React + Vite + TS + Tailwind
│   └── src/{pages,components,lib}
├── data/                        媒体文件与数据库（assets/thumbs/music/renders）
├── docs/PRD-偏差清单.md          与 PRD 的每一处偏差及原因
└── .env.example
```

## 界面预览

4 个页面均已本地实跑并截图（数据取自种子项目「桂林漓江·晨雾」）：

| 页面 | 路由 | 截图 |
|---|---|---|
| 生产看板 | `/` | `docs/screenshots/01-dashboard.png` |
| 词曲工作台 | `/projects/:id/lyrics` | `docs/screenshots/02-lyrics.png` |
| 剪辑工作台 | `/projects/:id/edit` | `docs/screenshots/03-edit.png` |
| 素材库管理 | `/assets` | `docs/screenshots/04-assets.png` |

各页承载的东西：

- **生产看板** —— 4 张汇总卡（进行中 / 待人工 / 本周成片 / 素材库）、5 列流水线（按状态归列，人工节点显橙点）、我的待办、GPU 任务队列（api 4 并发 / gpu 1 并发）、模型战绩（胜出 / 轮次 / 均分）。
- **词曲工作台** —— 简报条（地域·情绪·风格·BPM·时长·节奏·人声·语言 + 引用素材卡）、歌词逐行字数与韵脚、自动检查（字数越界 / 韵脚不齐 / 地标年代词）、三模型配置表、候选卡（直接显示模型名与版本）、咬字/旋律/编曲三项评分、选定与驳回。
- **剪辑工作台** —— 预览播放器（16:9 / 9:16 切换、字幕开关）、5 轨时间线（段落 / 画面 / 音乐波形 / 节拍 / 字幕）+ 播放头 + 拖拽改时长、镜头候选（Top4）、成片列表与投放数据回填、成本与耗时。
- **素材库管理** —— 自然语言检索 + 七维度筛选（地点/季节/时段/天气/情绪/镜头运动/画面主体 + 授权状态）、卡片网格、详情栏（标签增删、授权信息、可用状态、使用记录）、批量上传、授权清单导出。

## 快速开始

前置：Python 3.11+、Node 18+、**ffmpeg 在 PATH 中**（音频合成分成片都依赖它）。

### 1. 后端

```bash
cd backend
python -m venv .venv                 # 或直接用你已有的虚拟环境
.venv/Scripts/activate               # Windows；macOS/Linux 用 source .venv/bin/activate
pip install -r requirements.txt
cp ../.env.example ../.env           # 首次运行保持 FORCE_MOCK=1 即可
python -m app.seed                   # 建表 + 示例素材卡 + 占位素材 + 3 个示例项目
uvicorn app.main:app --reload --port 8000
```

首次 `app.seed` 会用 ffmpeg 生成 13 段 1920×1080 的占位镜头（约 15–30 秒），
用于在没有真实素材时跑通「镜头匹配 → 卡点粗剪 → 多版本合成」全链路。
接入真实素材后可在素材库删除它们。

自检（不启动服务，同步跑完整条流水线并产出真实 mp4）：

```bash
cd backend
python -m app.seed --run
```

### 2. 前端

```bash
cd frontend
npm install
npm run dev            # http://localhost:5173
```

Vite 已配置 `/api` 与 `/media` 到 `127.0.0.1:8000` 的代理，前端不需要额外配域名。

### 3. 受限网络（直连 PyPI / npm 极慢）时的安装

若所在网络访问软件源只有几 KB/s，硬等 `pip install` / `npm install` 会卡死。可按下述方式绕开：

**后端**：自己并发抓 wheel，再本地离线安装（`uv`/`pip` 会长时间卡在索引元数据解析上，不如自己抓）。

```bash
pip install --no-index --find-links <wheel目录> -r backend/requirements.txt
```

要点：并发下载（实测 8 路并发能把聚合吞吐从 40 KB/s 提到 ~190 KB/s）；
精确按 `requirements.txt` 的版本约束抓（`fastapi` 对 `starlette`、`pydantic` 对 `pydantic-core` 都有硬性版本绑定）。

**前端**：复用机器上已有的 `node_modules`（Vite / React / Tailwind 都是通用包），
用 Windows junction 挂进来，只把缺的少数包按 tarball 手动解包：

```bash
mklink /J frontend\node_modules\vite  D:\<其他项目>\node_modules\vite
curl -L https://registry.npmmirror.com/clsx/-/clsx-2.1.1.tgz | tar -xz --strip-components=1 -C frontend/node_modules/clsx
```

然后**不要**再跑 `npm install` —— 它会把手工建的 junction 当成冲突目录改名并清空。
直接启动：

```bash
node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 5173
```

本项目刻意去掉了 `@vitejs/plugin-react` 与 `typescript` 编译依赖：Vite 用 esbuild
原生转译 TSX（配 `tsconfig` 的 `"jsx": "react-jsx"`），少一整棵 @babel 依赖树。
代价是改代码时整页刷新而非局部热更新。

> 这种方式下 `node_modules` 是寄生的：junction 指向别的项目。删除本项目不会影响源项目；
> 反之源项目若被删除，需要重新挂接。网络正常的环境请直接用上面第 2 节的 `npm install`。

## 无 Key 也能跑通

这是本实现的一个明确设计目标。所有外部能力都走统一适配器，每个适配器都有结构等价的本地兜底：

| 能力 | 真实实现 | 无 Key 时的兜底 |
|---|---|---|
| 歌词 / 检查 / 画面描述 / 匹配分 / 字幕翻译 | OpenAI 兼容 LLM | 模板 + 本地规则（字数/韵脚/地标词命中），界面标「模拟」 |
| 音乐生成 | Mureka / Eleven / MiniMax 本地 | ffmpeg 合成真实可播放、BPM 与时长正确的音轨 |
| 节拍检测 | librosa | 按 BPM 生成节拍网格 |
| 歌词逐句对齐 | WhisperX / FunASR | 按字数加权分配并吸附强拍 |
| 素材打标 | Qwen-VL | 文件名词表命中 + 置信度推算（会真实触发「待纠错」） |
| 素材画面 | **通义万相文生图**（按目的地生成真实景点） | 程序生成的占位色块 |
| 画面匹配 | **LLM 语义分配 + 相邻不撞图轮换** | 字符 n-gram 哈希向量 + numpy 余弦（无语义，等同随机） |
| 多版本合成 | — | **ffmpeg 真实合成，产出可播放的 mp4** |

换句话说：把 `.env` 全部留空，你依然能拿到 4 个页面全部可用、能走完 10 个状态、
能把真实 mp4 拉到本地播放的系统。差别只是内容质量。

## 配置真实模型

编辑仓库根目录的 `.env`，把 `FORCE_MOCK` 改成 `0`，然后填对应的 Key：

```ini
FORCE_MOCK=0
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=qwen-plus

# 参与「同轮多模型对比」的名单（留空 = mureka,eleven,lyria）
MUSIC_MODEL_KEYS=
USD_CNY=7.2

# Mureka —— $0.045/首，国内可直连，最长 5m30s，付费调用含商用授权
MUREKA_API_KEY=...
MUREKA_MODEL=auto
MUREKA_PRICE_PER_SONG_USD=0.045
MUREKA_CONCURRENCY=1                             # $10 档 = 1；充到 $1000 档填 5

# Google Lyria 3.5（Gemini API）—— $0.08/首，官方 API 里最便宜
GEMINI_API_KEY=...
LYRIA_MODEL=lyria-3.5

# ElevenLabs Music —— $0.15/分钟，授权最清晰但最贵（需国际信用卡）
ELEVEN_API_KEY=...
ELEVEN_MUSIC_MODEL=music_v2

# MiniMax：官方托管 API 已对新用户关闭（2026-08-20），默认不进对比名单。
# 要重新启用它，把 MUSIC_MODEL_KEYS 里加上 minimax 即可。
MINIMAX_LOCAL_ENDPOINT=http://127.0.0.1:9000     # 新用户走开源自部署
MINIMAX_API_KEY=                                 # ⚠️ 仅存量付费账号有效
MINIMAX_API_HOST=https://api.minimaxi.com
MINIMAX_MUSIC_MODEL=music-3.0
```

三家分别怎么申请、门槛与单价、以及验证 Key 是否生效的命令，见
**[docs/AI-API-申请指引.md](docs/AI-API-申请指引.md)**。

重启后端即生效。某个模型凭证缺失时，系统**只降级那一个模型**（自动走兜底），
不影响其他模型和整体流程——词曲工作台的模型表会显示「降级到 mock」。

**对比名单是可配的**（`MUSIC_MODEL_KEYS`）。默认三家 `mureka,eleven,lyria` 的取舍依据：
Mureka 最便宜且国内最稳、Lyria 单价最低、ElevenLabs 授权链条最干净。
跌出名单的模型会被自动停用（否则它会挤进每一轮对比，无 Key 时降级成 Mock 白占一个候选位）。

每个模型适配器都公开 `capabilities()`（中文支持、最长时长、并发额度）与实时 `note()`
（当前配置下的用法与单价）。`note` **刻意不入库** —— 它描述的是「当前配置下怎么用」，
会随 `.env` 变化，入库必然过期。库里的 note 只存界面上的手填备注。

Mureka 适配器按 `MUREKA_CONCURRENCY` 把实际并发压到账号额度以内，并对 `429` 做
1/2/4/8 秒退避重试 —— 因为 Mureka 的并发额度是**按单笔充值额**给的（$10 → 1 并发），
而队列默认并发是 4，不收口会打出一半 429、重试耗尽后整条候选挂掉。

MiniMax 适配器是**双模**的（`backend/app/adapters/music/minimax.py`）：只配本地端点则进
gpu 队列（串行 1）；配了官方 Key 才走官方 API、进 api 队列（并发 4）。本地服务需满足的最小
契约见该文件头部注释。模型卡片上的名称与运行方式会跟着 `.env` 实时变化。

> ⚠️ **MiniMax 官方托管 API 自 2026-08-20 起不再对新用户开放**（免费版 `music-3.0-free`
> 等已停止服务；Token Plan 也不含音乐，只覆盖 M3 / M2.7 / image / speech）。
> 新用户请走开源自部署：Hugging Face 权重 `MiniMaxAI/MiniMax-Music3`，
> Community License **允许商用**（需在产品中显著标注 "MiniMax-Music3"）。

## 接入 monitor.py

系统**只读** monitor.py 的 SQLite 数据库，不修改原脚本一行。

```ini
MONITOR_DB=D:/WPS_Storage/WPS Cloud/WPSDrive/460887772/WPS云盘/AIGA-CODE/04-tourMV/trends.db
```

留空则使用 `data/trends.db`（不存在时系统使用内置示例素材卡）。
同步入口：

- `POST /api/trends/sync?sync=true`（或界面上的同步按钮）
- 定时任务建议：`collect` 每天一次、`report + analyze` 每周一次，即 PRD F5-1/F5-3

同步时直接复用 monitor.py 自己的 `rank()` 与 `analysis` 结果，不重复实现排序与要素拆解逻辑。
素材卡的"周"维度由系统按 `analyzed_at` 的 ISO 周计算（monitor.py 本身没有周的概念）。

## 关键设计说明

**状态机驱动一切。** 项目状态与看板列、待办类型、下游任务派发全部由 `state_machine.py`
单一来源定义；新增状态只需改这一个文件。

**队列可直接替换。** `queue.py` 暴露 `enqueue / handler / requeue`，形状对齐 RQ。
将来迁到 Redis + RQ 或 Celery，业务代码不用动。

**成本可查。** 每次 LLM / 音乐 / VLM 调用都写入 `ai_call` 表（模型、参数摘要、耗时、费用、状态），
剪辑工作台右侧「成本与耗时」按类型汇总，满足 PRD §12.1 的验收要求。

## 线上部署

已上线：**http://tourmv.migucomi.com/**（腾讯云 EdgeOne Makers）

架构是「静态资源全部走 EdgeOne CDN + 只有 `/api/*` 走边缘函数反代到后端」——
后端依赖 SQLite 写库、本地媒体文件、ffmpeg 子进程和常驻队列 worker，Serverless 环境跑不了。

⚠️ **HTTPS 证书尚未配置**，当前 443 上挂的是 CDN 共享证书（`*.cdn.myqcloud.com`），
浏览器会报证书错误，需在控制台申请免费证书。详见验收报告。

- 📋 **[线上部署验收报告](docs/线上部署验收.md)** —— 逐项验收结果、证书申请步骤、504 问题定位与修复
- 🔧 **`../wenlv-mv-edgeone/verify.mjs`** —— 一键验收（DNS / 证书 / 路由 / 静态 / 数据），退出码 0/1
- 📊 **`../wenlv-mv-edgeone/bench.mjs`** —— 源站直连 vs 经边缘的成功率与 P50/P95 对比

## 素材从哪来

**默认的 13 条素材是程序生成的占位色块**（`ffmpeg` 画的纯色块 + 把文件名烧成文字），
它们只用于打通链路，**不是能用的画面**。

要让成片真的好看，走「素材库 → **AI 生成景点素材**」：

1. 填目的地（如 `北京·中轴线`）和主题方向
2. 后端先用 LLM 把目的地拆成 **真实可考的景点**（北京 → 万宁桥 / 景山万春亭 /
   南锣鼓巷雨儿胡同 / 天坛圜丘坛 / 钟楼湾银杏 / 永定门），再逐条拼成摄影化 prompt
3. 调通义万相文生图出图（实测 **60–90 秒/张**），转成带缓慢推拉的视频片段入库
4. 之后重新卡点时，**只要有 ≥4 条 AI 真实素材，画面匹配就只在真实素材里选**
   （占位色块会被排除），并做相邻不撞图轮换

素材按「AI 生成」登记授权（`license_no = AI-YYYYMMDD`，scope 含海外投放），无第三方权利问题。

> 缓存按**景点名**做键，所以同一个目的地重复生成不会重复花钱；
> 只有出现新景点才会真正调文生图。

## 歌词与歌声怎么对齐（音画同步）

这是**最容易翻车、也最影响观感**的一环，所以单独说明。

时间轴来自**强制对齐**，不是估算：

1. 用 faster-whisper 把整首歌转写一遍，拿到「真实人声的字符流 + 每个字的时间戳」；
2. 把我们**已知的歌词**与识别结果做 **DTW 单调对齐**（字符级），
   于是每句歌词的起止时间来自歌声本身；
3. 只信「最连续的那一簇」匹配，避免副歌重复段把某句的跨度拉长；
4. 质量不达标（匹配率过低、或多数句子挂不上识别结果）时**回退到按字数估算**，
   并在时间轴上标明 `align_meta.engine`，界面显示「歌词对齐：按字数估算」。

三个实测踩出来的关键点（改动前请先读 `backend/app/adapters/align.py` 顶部注释）：

- **必须关掉 VAD**。Silero VAD 是按「说话」训的，对带伴奏的歌唱几乎全判成静音 ——
  实测阈值 0.5 时 169 秒里只剩 2.7 秒，转写为空，强制对齐直接失效。
- **不要传 `initial_prompt`（哪怕把歌词喂进去）**。传了之后 whisper 会
  在前奏里幻觉出歌词，把首句拉到错误的起点（实测 9.6s vs 真实 20.2s），
  而且慢一倍。识别率只从 35% 提到 38%，不值。
- **繁体要转简体**。whisper 的中文输出经常是繁体，`門≠门` 会让字符匹配大面积落空。
  依赖 `opencc-python-reimplemented`。

模型：`faster-whisper`（默认 `small`，CPU int8，约 480MB）。
首次使用自动下载到 `data/models/`，**发布时该目录会被移出**（见 `_probe/holdout.sh`），
线上首次对齐时重新下载。装不上或下不动时会自动回退估算，不影响流程跑通。

界面上「剪辑工作台」会显示当前是哪种来源，并提供**「重做歌词对齐」**按钮。

## 已知限制

- **AI 素材数量决定画面重复度**：一条 170 秒的 MV 约 17–18 个镜头，
  6 条素材意味着每条平均出现 3 次。想要更丰富就多生成几条（生成张数可填到 12），
  代价是每张 60–90 秒。

- **竖版「主体居中裁切」未实现主体检测**，当前为居中裁切（见偏差清单）。
- **短版切片**以第一段副歌起点为基准截 170 秒，不是智能选段。
- 打标为同步执行（上传即完成），PRD F4-1 允许的"后台打标"路径预留了 `asset_tag` 任务类型但未默认启用。
- 素材抽帧策略为固定时间点（0.5s / 2s / 4s），未做镜头切换检测——这直接影响 VLM 打标成本，接真 VLM 前需要先定策略。
- 预览播放器为「按当前片段切换视频源 + 音频对齐」的实现，不是逐帧合成预览；最终效果以合成后的成片为准。
- 合成过程的中间片段放在系统临时目录（`%TEMP%/wenlvmv_work_*`）且**不做自动清理**：
  一是文件名固定、重复渲染即覆盖，本质幂等；二是批量删除容易触发宿主机的批量删除保护
  而导致合成中断。需要时可手动清理系统临时目录。
- 无 Key 兜底模式下，中英字幕的英文译行来自本地词表直译（如 `Night walk over`），
  只用于验证双语字幕链路是否贯通，不代表真实翻译质量。
