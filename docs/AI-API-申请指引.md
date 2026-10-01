# 音乐模型 API 选型与申请指引

面向本项目 `wenlv-mv` 的实际配置：每个模型对应 `.env` 里哪几个变量、申请要过哪些关卡、
大概花多少钱。价格与准入政策来自各家官方页面（**2026-09-27 核对**），这类信息变动频繁，
**下单前请再核一次官方报价页**。

---

## 0. 先看结论

| 模型 | 官方入口 | 单价（官方公开价） | 最长时长 | 支付 | 大陆直连 | 一句话判断 |
|---|---|---|---|---|---|---|
| **Google Lyria 3.5** | aistudio.google.com | **$0.08 / 首** ≈ ¥0.58 | 约 3 分钟 | 国际信用卡 | ✅ 实测可连（不稳） | **官方 API 里最便宜**，支持自定义中文歌词；但单轮生成、输出带水印 |
| **Mureka**（V8/V9/O2） | platform.mureka.ai | **$0.045 / 首** ≈ ¥0.32 | 5m30s | 预充值，**$10 起** | ✅ 稳定 | **综合最优起手**：最便宜、国内通、额度最全 |
| **Mureka V7.6** | 同上 | $0.03 / 首 ≈ ¥0.22 | 5m30s | 同上 | ✅ | 更便宜但模型更老 |
| **ElevenLabs Music v2.5** | elevenlabs.io | **$0.15 / 分钟**（3 分钟歌 ≈ ¥3.24） | 5 分钟 | Stripe，**无支付宝** | ✅ | 授权条款最清晰；**最贵** |
| **MiniMax Music** | — | 仅存量账号 $0.15/首 | — | — | — | 🚫 **新用户已无法申请**（2026-08-20 起停开） |

> 三家互相独立，不必都申请。缺 Key 时系统自动降级到 Mock 合成器（界面显示「模拟」标记），
> 所以可以一家一家补，补一家就多一条真实链路。

### 连通性实测（2026-09-27，本机出口 IP 上海 · 中国移动/联通段）

六个端点全部 TLS 证书校验通过，且都不是中间人拦截：

| 端点 | 结果 | 证书签发 |
|---|---|---|
| `generativelanguage.googleapis.com` | ✅ HTTP 404（根路径正常响应） | Google Trust Services |
| `api.mureka.ai` | ✅ HTTP 404 | GoDaddy |
| `api.elevenlabs.io` | ✅ HTTP 404 | Google Trust Services |
| `dashscope.aliyuncs.com` | ✅ HTTP 404 | GlobalSign |
| `api.deepseek.com` | ✅ HTTP 401 | Amazon |
| `api.minimaxi.com` | ✅ HTTP 404 | WoTrus |

⚠️ 两个提醒：
1. **Google 的连通性不代表稳定性**。这次能直连，但大陆到 Google 的链路随时可能变化；
   要接 Lyria 又想稳，得准备代理或兜底降级。
2. Windows 上用 `curl` 测这些域名经常返回 `000`（schannel 反复 renegotiation），
   **那是 Windows 的假象，不是真连不上**。判断证书是否有效请用 Node。

---

## 1. 选型逻辑：为什么是这三家

本项目的核心机制是「**同轮多模型对比 + 人工评审选型**」（详见 PRD §7）。这个机制要成立，
参与的模型必须满足三条：

1. **有官方公开 API**（能自动化调用，不是网页版）
2. **支持自定义歌词**（歌词是我们自己产出的中文文旅文案，必须逐字进歌）
3. **能商用**（文旅项目最终要对外发布）

按这三条筛下来：

- **Suno / Udio** —— ❌ 无官方 API。市面上的「Suno API」都是逆向封装，条款与稳定性都有问题。
- **MiniMax** —— ❌ 官方 2026-08-20 起对新用户关闭（见第 5 节）。
- **Stable Audio 3** —— ❌ 无人声，做不了歌词歌曲（纯音乐/音效可用）。
- **AIVA / Mubert** —— ❌ 无确认的公开 API 或授权含混。
- **Mureka / ElevenLabs / Google Lyria** —— ✅ 三条都满足，构成当前默认对比名单。

名单可在 `.env` 里改：`MUSIC_MODEL_KEYS=mureka,eleven,lyria`（默认值）。

---

## 2. Google Lyria 3.5 —— 建议从这家起手

**为什么**：$0.08/首是所有官方 API 里最便宜的，而且是**按首计价**，不随时长涨。
一首 3 分钟的歌 $0.08，是同长度 ElevenLabs（$0.45）的 **1/5.6**。

### 申请步骤

1. 打开 `https://aistudio.google.com`，用 Google 账号登录。
2. 左侧 **Get API key** → **Create API key** → 选一个 Google Cloud 项目（没有就新建）。
3. **必须开通付费层（paid tier）**：Gemini API 的 Lyria 系列**没有免费额度**，
   免费层调用会直接报错。进 Google Cloud Console → Billing → 绑定一张**国际信用卡**
   （Visa/Mastercard；国内双币卡通常可用）。
4. 复制 Key（形如 `AIza...`）。

### 填进 .env

```dotenv
GEMINI_API_KEY=AIza...
GEMINI_BASE_URL=https://generativelanguage.googleapis.com
LYRIA_MODEL=lyria-3.5
LYRIA_PRICE_PER_SONG_USD=0.08
```

### 接口规格

- `POST {base}/v1beta/models/{model}:generateContent`
- 鉴权：请求头 `x-goog-api-key: <KEY>`
- 出参：`candidates[0].content.parts[]` 里既有 `inlineData`（base64 音频，44.1kHz 立体声，
  默认 MP3）也有 `text`（模型实际唱出来的歌词，可用来核对）
- 自定义歌词直接写在提示词里，用 `[Verse]` / `[Chorus]` / `[Bridge]` 段落标签

### 三个必须知道的约束

1. **单轮生成，没有二次编辑**。「把副歌再唱高一点」做不到，只能整首重新生成。
   所以本项目走的是「局部重写歌词 → 整首重生成」的路径，不是逐段微调。
2. **每首输出都带 SynthID 水印**（人耳不可闻，但持续存在、可被检测）。
   如果你们的交付标准要求「无水印」，这一点要先确认。
3. **提示词会被安全策略拦截**：点名具体歌手的音色、要求生成受版权保护的歌词，都会被拒。

### Key 有效性验证

```bash
curl -s "https://generativelanguage.googleapis.com/v1beta/models?key=$GEMINI_API_KEY" | head -c 300
```

能列出模型列表即 Key 有效，**不产生费用**（ListModels 是免费接口）。

---

## 3. Mureka —— 综合最优，建议作为主力

**为什么**：$0.045/首，国内可稳定直连，单曲最长 5m30s（比另两家都长），
额度最全（歌词生成、翻唱、人声克隆、分轨、视频配乐），且**官方明示付费 API 调用产出自带完整商用授权**。
起充门槛只有 **$10**。

### 申请步骤

1. 打开 `https://platform.mureka.ai` 注册。
   ⚠️ **C 端站点（mureka.ai）和开发者平台是两套账号体系**，在 C 端注册拿不到 API 权限，
   必须从开发者平台注册。
2. 控制台 **「API Keys」** → 创建 Key。认证方式 `Authorization: Bearer <KEY>`。
3. 充值到 **Billing**。API 走**预付费余额**，不是订阅。并发额度按**单笔充值额**给：

   | 单笔充值 | 并发额度 |
   |---|---|
   | **$10** | **1** |
   | $1,000 | 5 |
   | $3,000 | 15 |
   | $5,000 | 25 |

   ⚠️ 并发按**每一笔**算，不是累计：连充 5 次 $1,000 仍是 5 并发，不是 25。

### 填进 .env

```dotenv
MUREKA_API_KEY=你的Key
MUREKA_BASE_URL=https://api.mureka.ai
MUREKA_MODEL=auto
MUREKA_PRICE_PER_SONG_USD=0.045
MUREKA_CONCURRENCY=1          # $10 档填 1；充到 $1000 档填 5
```

**`MUREKA_CONCURRENCY` 必须填对。** 本项目的 api 队列默认并发 4，而 $10 档只给 1 并发 ——
4 个生曲任务同时打过去会有一半吃 429。适配器已经按这个值把实际并发收口，
并对 429 做 1/2/4/8 秒退避重试；但如果你把这里填成 5 而账号只有 1，照样会被拒。

### 计价明细（按首，官方页）

| 能力 | 模型 | 价格 |
|---|---|---|
| 歌词转歌（我们用的） | V7.6 | **$0.03/首** |
| 歌词转歌 | V8 / V9 / O2 | **$0.045/首** |
| 歌词转歌 | 9.5 | $0.15/首 |
| 提示词生歌 | V7.6 / O2 / V8 / V9 | $0.30/首 |
| 提示词生歌 | 9.5 | $0.50/首 |
| BGM（纯音乐） | V7.6 | $0.03/条 |
| BGM | V8 / V9 | $0.045/条 |
| **歌词生成** | Lyric model | **$0.009/首** |
| 单行歌词续写 | Lyric model | $0.002/行 |
| 人声克隆 | Vocal model | $5/个 |
| 分轨导出 | Separation V1 / V3 / V2 | $0.06 / $0.20 / $0.70 |
| 视频配乐（图/视频生音乐） | V9 | $0.10/首 |

> 一个对我们有用的点：**可以一次请求最多出 3 首**（默认 2 首）。想省调用次数时用它，
> 但价格是按首算的，出 2 首就是 2 首的钱，并不会打折。

### Key 有效性验证（省钱）

**别一上来就生曲**。用最便宜的接口验：

```bash
curl -s https://api.mureka.ai/v1/lyric/generate \
  -H "Authorization: Bearer $MUREKA_API_KEY" -H "Content-Type: application/json" \
  -d '{"prompt":"一座江边小城的清晨"}' | head -c 300
```

一次 $0.009，返回歌词即通。

---

## 4. ElevenLabs Music —— 授权最清晰，但最贵

**为什么仍值得考虑**：训练数据经 Merlin / Kobalt / Believe 授权，2026-09 又签了 UMG 多年协议，
**授权链条是三家里最干净的**。如果你们的法务对「训练数据来源」有要求，这家最好解释。

### 申请步骤

1. `https://elevenlabs.io` 注册。
2. 平台选择页里选 **ElevenAPI**（不是 ElevenCreative）。
3. 绑卡订阅或开通按量付费。
4. 左侧栏最下方 **Developers → API Keys → Create Key**。
   **务必设消费上限 + 只勾最小权限** —— 这个 Key 泄露后能被直接刷爆。
5. 认证头是 `xi-api-key`（不是 Bearer）。

### 填进 .env

```dotenv
ELEVEN_API_KEY=你的Key
ELEVEN_BASE_URL=https://api.elevenlabs.io
ELEVEN_MUSIC_MODEL=music_v2
```

### 计价与授权限制

- **$0.15 / 分钟**（≈ ¥1.08/分钟）。3 分钟歌 ≈ $0.45 ≈ ¥3.24，三家里最贵。
  本项目一条 MV 出 2 首该模型候选 ≈ **¥5.4**，会占掉整条成本的大头。
- 单次 3 秒 – 5 分钟，MP3 / WAV 可选。
- **授权限制（容易忽略）**：
  - 影视 / 电视 / 电台 / 工作室游戏**不在**商用范围内
  - **Free 和 Starter 档不得把曲目分发到流媒体平台**（Spotify 等），
    要分发至少 **Creator（$22/月）**
  - Free 档还要求标注「Eleven Music」
- **只走 Stripe 信用卡，不支持支付宝**，国内得备国际卡。

### 建议策略

放最后上。如果预算紧，**只在这一家上跑 1 首**（而不是 2 首）就够参与对比 ——
反正它是用来做「授权基线」的，不是用来做主力产能的。本项目支持按模型指定候选数。

### Key 有效性验证（免费）

```bash
curl -s https://api.elevenlabs.io/v1/user -H "xi-api-key: $ELEVEN_API_KEY"
```

返回订阅信息即通，**不产生费用**。

---

## 5. MiniMax —— 已对新用户关闭，只能开源自部署

### 现状

官方服务调整公告（**2026-08-20 生效**）：

> 付费的**音乐生成与歌词生成 API 不再对新用户开放**，存量付费用户可继续使用；
> 免费的 `music-3.0-free` / `music-2.6-free` / `music-cover-free` **已停止服务**。
> 要体验或使用音乐生成，请前往 MiniMax Audio，或使用 Hugging Face 上的开源
> **MiniMax Music 3**。

两个容易踩的误判：

- **买 Token Plan 没用。** 官方 Token Plan 覆盖范围是 `M3 / M2.7 / image / speech`，
  **音乐不在其中**；Credits 包同一份资源清单。
- **文档里还列着 model 枚举 ≠ 你能开通。** API 文档仍有 `music-3.0` / `music-2.6` / `music-cover`，
  价格页也还挂着 $0.15/首 —— 那是**给存量付费账号保留的**。
  已有社区报告新账号调用收到 HTTP 410。

**因此本项目默认名单里不含 MiniMax**（适配器保留，但停用）。
要重新启用：`MUSIC_MODEL_KEYS=mureka,eleven,lyria,minimax`。

### 如果走开源自部署

| 项 | 内容 |
|---|---|
| 权重 | `MiniMaxAI/MiniMax-Music3`（Hugging Face 官方仓库） |
| 许可证 | **MiniMax-Music3 Community License** —— 允许商用，要求产品显著标注 "MiniMax-Music3"；年收入 > $20M 需另行授权 |
| 官方要求 | ⚠️ 官方 README 局限性第一条：**推理需要两张 CUDA GPU**（参考路径 SGLang-Omni 会把 8B 全局 LLM 与 Flow Matching 拆到两张卡上） |
| 单卡替代 | ComfyUI INT8 量化档 / diffusers 分组卸载；社区锚点：4090 24GB + FP16 pruned DiT + INT8 文本编码器 + tiled decode → 3 分钟歌 219 秒、峰值显存 19.9GB |
| 权重体积 | 全仓库 57.4GB；diffusers 只需子集 **≈29GB**（可跳过 `qwen_7B/` 与 `flowmatching_vae.pth`） |
| 显存分档 | 全精度 ~24GB / CPU offload ~22GB / group offloading 8GB |
| 输出 | 32kHz / 16-bit 立体声 |
| 内存 | 官方建议 **32GB 起**（fp16 权重约 22GB），这是比显存更容易被忽略的硬门槛 |

```dotenv
MINIMAX_LOCAL_ENDPOINT=http://127.0.0.1:9000
```

**新用户不要填 `MINIMAX_API_KEY`** —— 申请不到有效 Key，填了只会让适配器以为该走云端。
注意项目的 `MINIMAX_LOCAL_ENDPOINT` 契约是 `POST /generate` + `GET /jobs/{id}`，
与模型原生的 `/v1/audio/speech` 不同，中间需要一层 wrapper。

---

## 6. 顺带要配的两项（不然真实链路仍跑不通）

### 大语言模型（写歌词、红线校验、分镜描述）

本项目走 **OpenAI 兼容端点**：

```dotenv
LLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
LLM_API_KEY=你的DashScope Key
LLM_MODEL=qwen-plus
```

- **通义千问（DashScope）**：`dashscope.console.aliyun.com`，阿里云账号 + 实名 + 开通百炼，
  有免费额度。**一个 Key 同时管歌词和素材打标**，建议先申请这个。
- **DeepSeek**：`platform.deepseek.com`，`LLM_BASE_URL=https://api.deepseek.com/v1`，更便宜。

### 视觉模型（素材 AI 打标）

```dotenv
VLM_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VLM_API_KEY=你的DashScope Key
VLM_MODEL=qwen-vl-plus
```

### 别忘了总开关

```dotenv
FORCE_MOCK=0
```

`FORCE_MOCK=1`（默认）时**所有外部调用强制走 Mock**，Key 填了也不生效。

---

## 7. 一条 MV 花多少钱

按默认流程：3 个模型各出 2 首候选（共 6 首），成片 2:30。汇率按 `USD_CNY=7.2`。

| 项 | 次数 | 单价 | 小计 |
|---|---|---|---|
| 歌词生成 + 校验 + 局部重写（LLM） | 约 5 次 | 千级 token | ≈ ¥0.3 |
| 素材 AI 打标（VLM） | 13 张 | 一次一图 | ≈ ¥0.5 |
| Mureka V8 生曲 | 2 首 | ¥0.32/首 | ¥0.65 |
| Google Lyria 3.5 生曲 | 2 首 | ¥0.58/首 | ¥1.15 |
| ElevenLabs 生曲 | 2 首 | ¥2.70/首 | ¥5.40 |
| **合计（三家全开）** | | | **≈ ¥8.0 / 条** |

**省钱的两条分档**：

| 方案 | 生曲成本 | 合计 |
|---|---|---|
| Mureka + Lyria（两家）+ LLM/VLM | ¥1.80 | **≈ ¥2.6 / 条** |
| 只 Mureka + LLM/VLM | ¥0.65 | **≈ ¥1.5 / 条** |
| 三家全开 | ¥7.20 | ≈ ¥8.0 / 条 |

> **按「可用成片」而不是「生成次数」算成本。** 行业经验：首轮生成基本都要淘汰，
> keeper rate 大约 1/4 – 1/8。如果评审严格，实际成本要乘以 2–4 倍。
> 用 `Mureka + Lyria` 这一档、按 1/5 命中率估，**一条可用成片约 ¥10–13**。

---

## 8. 建议的推进顺序

1. **先配 LLM + VLM（一个 DashScope Key 搞定）** —— 最便宜、最快见效：
   歌词和素材打标立刻变真实，只有生曲还是 Mock。
2. **Mureka 充 $10** —— 三家托管 API 里最便宜 + 国内最稳，三模型对比立刻有一条真链路。
   （顺带把 `MUREKA_CONCURRENCY=1` 填对。）
3. **Google Lyria 3.5** —— $0.08/首，补上第二条真链路，成本几乎可以忽略。
   需要注意付费层绑定国际卡 + 连通性波动。
4. **ElevenLabs 最后考虑** —— 除非确实需要它那套干净的授权链，否则不必为「音质」多付 8 倍价钱。

> 全部配好后仍只花 **¥8 左右一条 MV**。这个阶段的最大成本不是钱，是**评审人工时间**。

---

## 9. 配好之后怎么确认生效

界面上：打开 `/dashboard` → 右侧「音乐模型」卡片，`ready` 变绿、备注不再显示「未配置」。

命令行：

```bash
curl -s http://127.0.0.1:8000/api/music/models
```

逐项看：

- `ready` —— 凭证是否就绪（`false` = 会降级到 Mock）
- `effective` —— 实际生效的适配器。**还是 `mock` 就说明降级了**，别只看 `ready`
- `note` —— 实时反映当前配置下的用法与单价（不入库，不会过期）
- `capabilities` —— 中文支持、最长时长、并发额度
- `enabled` —— 是否参与本轮对比

---

## 10. 四个风险提醒

1. **别用第三方「中转/代理 API」跑生产。** 转售额度大多违反原厂条款、随时断服，
   且你的 Key 与内容会经过第三方。临时调试可以，别写进交付方案。
2. **Suno / Udio 没有公开 API。** 搜到的「Suno API」基本都是逆向封装，同样有条款与稳定性风险。
3. **别把「文档里还有这个接口」当成「你能开通」。** MiniMax 2026-08-20 那次就是活案例：
   文档还列着 model 枚举、价格页还挂着单价，但新账号已经调不通。
   看到 `429` / `410` / `permission_denied`，先核对服务公告，而不是改代码。
4. **「可商用」不等于「有著作权」。** 平台给的是「不因使用而起诉你」，不是「你可以用它去告别人」。
   目前各国实务上，纯 AI 生成内容通常**无法登记著作权**，别人用同一首曲子你也拦不住。
   涉及重要交付时，把使用条款、生成时间、账单记录一起归档。
