# 即梦 / Seedream 文生图 API：申请与接入（逐句歌词配图用）

> 目的：让「每句歌词 → 对应景区图片」这一步用上即梦同源的图像模型，
> 把画质从万相的 1440×810 提到 2K。

## 0. 先澄清一件事：即梦没有独立的开放 API

即梦（Dreamina）是 App / Web 产品，**官方没有面向开发者的独立 API**。
它的图像能力（Seedream 模型）通过**火山引擎**对外开放，有**两条通道**：

| 通道 | 是什么 | 鉴权方式 | 本项目对应的 provider |
|---|---|---|---|
| **火山方舟 Ark**（推荐） | OpenAI 兼容接口，一行 JSON 出图 | `Authorization: Bearer <API Key>` | `seedream` |
| **视觉智能 · 即梦AI-图片生成** | 即梦品牌能力，异步任务接口 | AK/SK + HMAC-SHA256 V4 签名 | `jimeng` |

两者底层都是 Seedream 系模型，**画质一致**。差别只在接入复杂度：

- 方舟：**只需一个 API Key**，Bearer 认证，代码最简单 → **优先走这条**
- 视觉智能：要 AK/SK 两个凭证 + 自己实现 V4 签名（本项目已实现好，但配置更繁琐）

下面两条都写，按需选一条即可。

---

## 1. 路线 A（推荐）：火山方舟 + Seedream 4.0

### 1.1 申请步骤

1. **注册火山引擎账号**
   打开 https://www.volcengine.com/ ，用手机号或抖音扫码注册登录。

2. **完成实名认证（必须）**
   进入「账号 → 实名认证」，个人认证走**人脸核验**。
   ⚠️ 没实名认证无法开通任何模型服务，这一步不能跳。

3. **开通 Seedream 模型服务**
   顶部「产品」→「人工智能与机器学习」→ 找到**图像生成大模型（即梦AI / Seedream）**，
   进入产品页 → 「立即体验」→ 左侧选**图片生成**→ **开通服务**。
   或者直接在火山方舟控制台的「**开通管理**」里勾选 Seedream 系模型并开通。

4. **创建 API Key**
   火山方舟控制台 → 左侧「**API Key 管理**」→「**创建 API Key**」→ 复制整串值
   （形如 `api-key-20261011xxxxxx`）。

   > ⚠️ **最容易踩的坑**：这里要的是方舟的 **API Key**，
   > **不是** IAM 里的 AccessKey / SecretKey（AK/SK）。
   > AK/SK 是给 V4 签名用的，拿它去当 Bearer Token 会直接 **401**。

5. **确认模型 ID**
   在方舟控制台「开通管理 / 模型列表」里确认已开通的模型 ID。当前为 **5.0 系列**：

   | 模型 | Model ID | 特点 |
   |---|---|---|
   | Doubao-Seedream-5.0-flash | `doubao-seedream-5-0-flash-260915` | 快、便宜（$0.03/张），**默认选它** |
   | Doubao-Seedream-5.0-pro | `doubao-seedream-5-0-pro-260628` | 画质更高，时延更大 |

   两者都支持文生图 / 单图生图 / 多图生图 / 交互编辑 / 图层拆分。

6. **充值 / 领免费额度**
   新用户通常有一定免费额度；额度用完后在「费用中心」充值。
   出图按张计费，**具体单价以控制台刊例价为准**（不要按本文件估）。

### 1.2 填到本项目

编辑 `wenlv-mv/.env`，加两行（文件里已有注释占位，去掉 `#` 填值即可）：

```ini
ARK_API_KEY=api-key-你复制到的那一串

# 可选：默认已内置 5.0 flash，账号开通的是 pro / 4.0 时在这里覆盖
# SEEDREAM_IMAGE_MODEL=doubao-seedream-5-0-flash-260915
# SEEDREAM_IMAGE_SIZE=2560x1440      # 16:9，见下方「size 的坑」
```

再把默认出图通道切到 seedream：

```ini
IMAGE_PROVIDER=seedream
```

> 也可以不改 `IMAGE_PROVIDER`，只在配图审核页里「换模型重出」时单选 Seedream，
> 这样万相仍是默认、Seedream 按需用。

**改完 `.env` 必须重启后端**（`settings` 是 `lru_cache`，不重启不生效）。

### 1.3 验证

```bash
cd wenlv-mv
python _probe/image_probe.py seedream
```

Seedream 会**分三步发请求**（最小请求 → 加尺寸 → 完整参数），把「Key 问题 /
模型 ID 问题 / 参数问题」分开暴露；成功后落一张样图到 `data/_probe_t2i/`：

```
【步骤 1】检查凭证    ✅ 已读到 Key：api-key-…（长度 44）
【步骤 2】最小请求    ✅ [最小请求] HTTP 200
【步骤 3】带尺寸请求  ✅ [完整请求] HTTP 200
✅ 全部通过  最终尺寸=2560x1440  耗时=13.8s  体积=1180KB
```

也可以绕开项目代码，用 cURL 直接打一发（最快确认连通性）：

```bash
bash _probe/ark_curl.sh            # Key 与模型 ID 从 .env 读取
```

### 1.4 Seedream 5.0 的参数要点（调试前必读）

这几条是 5.0 相对 4.0 的变化，也是 400 报错的主要来源：

| 参数 | 规则 | 踩坑后果 |
|---|---|---|
| `size` | **只有两种写法且不可混用**：档位词 `1K`/`1.5K`/`2K`（默认 2K），或像素值 `宽x高`（总像素须落在 **921600~4624220**、宽高比须落在 **1/16~16**） | 混用或超范围 → **400** |
| `aspect_ratio` | **不存在这个参数** | 只传 `2K` 会拿到模型自己的默认比例（偏方），16:9 配图会被裁坏 |
| `sequential_image_generation`（组图） | **5.0 不支持**（4.x 才有） | 传了 → **400** |
| `stream`（流式） | **5.0 不支持** | 传了 → **400** |
| `watermark` | 接口默认 **true** | 不显式设 false → 图上有水印 |

**要 16:9 横版配图就必须传像素值**。项目默认 `2560x1440`（= 3,686,400 像素，合规）；
官方常用的 16:9 档位还有 `2816x1584`（4,460,544）与 `2048x1152`，都在允许区间内。

> 代码里已按**主版本号**决定是否传组图参数（只有 4.x 传），并对 400 做了
> 「自动剥掉可选参数重试一次」的兜底 —— 见 `adapters/image.py::_seedream()`。

---

## 2. 路线 B：火山引擎视觉智能（即梦AI-图片生成）

需要 **AK/SK + 开启视觉智能服务**，项目里 `jimeng` provider 已实现 V4 签名，配置好即可用。

1. **开通服务**
   火山引擎控制台 →「产品」→「人工智能与机器学习」→ **图像生成大模型（即梦AI）**
   → 选「**即梦AI-图片生成**」→ 开通服务。

2. **创建 Access Key**
   「访问控制 IAM」→「**密钥管理**」→ 新建 Access Key，得到
   `Access Key ID` 与 `Access Key Secret`。

3. **记下 req_key（即模型标识）**
   控制台该能力的文档页会标注，常见：
   - `jimeng_t2i_v40`（4.0）
   - `jimeng_t2i_v31`（3.1）

4. **填到 `.env`**：

```ini
VOLC_ACCESS_KEY=你的 AK
VOLC_SECRET_KEY=你的 SK
# JIMENG_IMAGE_MODEL=jimeng_t2i_v40
IMAGE_PROVIDER=jimeng
```

5. **验证**：`python _probe/image_probe.py jimeng`

---

## 3. 常见错误对照

| 报错 | 原因 | 处理 |
|---|---|---|
| **401** | Key 类型不对。把 AK/SK 当成 Bearer 用了 | 改用方舟「API Key 管理」里的 `api-key-xxx` |
| **404** | 模型 ID 不存在或未开通 | 方舟控制台确认已开通，把 ID 抄进 `SEEDREAM_IMAGE_MODEL` |
| **403** | 未实名认证 / 未开通该服务 | 完成人脸实名 + 在「开通管理」里开通 |
| **400** | 参数不被接受。5.0 最常见的是 size 写法混用，或误传了组图/流式参数 | 尺寸在「档位词」与「宽x高」中**二选一**（16:9 传 `2560x1440`）；不要传 `sequential_image_generation` / `stream` |
| **429** | 触发限流 | 调低 `IMAGE_CONCURRENCY`（默认 3），或稍后重试 |
| 出图是插画风 | 模型默认审美偏插画 | 本项目已自动追加写实摄影后缀 + 负面提示词，无需手工处理 |

## 4. 接入点（代码位置，便于排障）

- provider 注册与分派：`backend/app/adapters/image.py`
  - `PROVIDERS` 字典：三个 provider 的 label / model / size / 凭证字段 / 单价
  - `_seedream()`：方舟同步接口 `POST /api/v3/images/generations`
  - `_jimeng()`：视觉智能异步接口（`CVSync2AsyncSubmitTask` → `CVSync2AsyncGetResult`）
- 逐句配图调用它：`backend/app/services/storyboard.py`（每句歌词一张图，并发出图）
- 前端可选模型列表：`GET /api/projects/{project_id}/storyboard` 返回体里的
  `providers` / `provider` 字段（来自 `image_ad.list_providers()`），前端据此渲染模型选择

## 5. 换模型后的注意事项

1. **出图单价会变**：`price()` 按 provider 取价，改完记得核对 `SEEDREAM_IMAGE_PRICE`，
   否则项目成本统计会失真。
2. **已审核通过的配图不会自动重出**。换模型只影响新出图；要让老项目用新模型，
   需在配图审核页逐句「换模型重出」，或重跑 `storyboard_generate`。
3. **尺寸从 1440×810 提到 2560×1440 后**，`media.image_to_clip` 的
   `scale → crop → zoompan` 链路对源图是**缩小**而非放大，不会再出现放大造成的柔化，
   观感应明显好于万相链路（滤镜链里的 unsharp 仍会执行，属轻微锐化，无害）。
