# 齐家 AI 家庭教育内容工作台

面向齐家 AI 家庭教练抖音账号的内容研究与短视频生产服务。工作台先通过 TikHub 研究抖音家庭教育选题，人工采用候选并补充可靠来源后，再生成脚本、旁白、五章分镜和可下载的发布包。

## 当前真实链路

```text
TikHub 抖音家庭教育数据
  → 5 个候选选题与成本记录
  → 人工采用选题
  → 人工补充并确认可靠来源
  → OpenRouter 脚本
  → 人工确认脚本
  → 豆包 TTS 2.0 完整旁白
  → OpenRouter 五章分镜
  → Seedream 五张首帧
  → Seedance 三段 480p 视频 + 两张动态图片
  → Remotion 合成
  → 人工确认成片
  → final.mp4 与发布包
```

抖音趋势只用于解释“为什么值得研究”，不会自动成为来源卡中的已核验事实。生产链路不使用 Mock，也不会自动发布到抖音。Mock Provider 只存在于显式 CLI 演示和测试中。

## 一期选题边界

- 主题固定为家庭教育，不扩展到泛母婴、婚恋或社会热点。
- 数据平台固定为抖音，数据服务固定为 TikHub。
- 每轮计划最多 13 次 TikHub 请求，硬上限由 `QIJIA_TOPIC_TIKHUB_REQUEST_BUDGET` 控制。
- 每轮只调用 1 次编辑模型，输出恰好 5 个候选，不做播放量或爆款预测。
- TikHub 每次调用完成后都会立即保存请求 ID 与规划成本；编辑模型的 Token 和供应商上报费用也会在候选门禁前入账。
- 付费研究在服务重启后不会自动重跑，避免重复计费；用户可人工开始新一轮。

## TikHub 契约依据

接口边界按 2026-08-05 的 TikHub 官方文档收敛：

- `617` 是抖音指数的“母婴”垂类；系统在此基础上再次过滤家庭教育词，不把泛母婴内容混入候选：<https://docs.tikhub.io/443673045e0>
- 创作热门关键词、飙升话题和关键词相关视频只使用文档声明的 `tag_id / period / end_date / rank_type / keyword` 查询参数：<https://docs.tikhub.io/444247763e0>、<https://docs.tikhub.io/444247765e0>、<https://docs.tikhub.io/444247764e0>
- 六组家庭教育样本使用抖音视频搜索 V2 的近一周、1 分钟内视频筛选：<https://docs.tikhub.io/370212780e0>
- “高完播率”和“低粉爆款”只保存为 TikHub 的平台标签，不会伪造文档未返回的完播率百分比：<https://docs.tikhub.io/443673045e0>
- 中国大陆使用官方建议的 `api.tikhub.dev` 加速域名：<https://docs.tikhub.io/4579297m0>
- 编辑模型使用 OpenRouter 非流式响应自带的 `usage` 记录 Token 和供应商上报成本，不额外发起费用查询：<https://openrouter.ai/docs/cookbook/administration/usage-accounting>

TikHub 文档的示例响应没有提供稳定的业务 `data` 样例，因此适配器采用保守归一化：无法识别有效日期会立即停止；可识别视频少于 6 条时不会调用编辑模型；不保存庞大的原始响应，只保存候选可复核所需的证据快照和请求 ID。

## 独立架构

- `qijia_video/`：领域契约、工作流、Provider、API、鉴权和 Web 页面。
- `qijia_video/infrastructure/postgres_repository.py`：来源卡与视频任务聚合仓储。
- `qijia_video/run_service.py`：后台任务进度、互斥和重启恢复。
- `qijia_video/topic_*`：选题契约、确定性研究流程、任务执行与 API。
- `qijia_video/infrastructure/tikhub.py`：TikHub 抖音读接口与请求预算。
- `video_renderer/`：独立 Remotion 渲染包，只消费 Render Manifest。
- PostgreSQL：保存业务聚合与后台运行状态。
- 火山 TOS：保存参考图、音频、首帧、视频和发布包。

服务不依赖“继续追问”的代码、数据库表、登录系统或 Railway 项目。

## 本地安装

```powershell
python -m pip install -r requirements-dev.txt
npm.cmd ci --prefix video_renderer --ignore-scripts
```

复制 `.env.example` 为 `.env`。本地真实 Web 链路同样需要 PostgreSQL 和 TOS；不会把本地磁盘伪装成可供 Seedance 拉取的公网存储。
本地只通过 HTTP 调试登录时，将 `AUTH_COOKIE_SECURE=false`；Railway 必须保持 `true`。

```powershell
uvicorn main:app --reload
```

访问 `http://127.0.0.1:8000/`。

## Railway

在同一个 Railway Project 中创建：

1. 一个 PostgreSQL Service；
2. 一个连接本仓库的 App Service。

App Service 设置：

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
```

其余变量按 `.env.example` 配置。`ADMIN_PASSWORD` 至少 12 个字符，`SESSION_SECRET` 至少 32 个随机字符。真实 Secret 只保存在 Railway，不进入 Git。

`railway.json` 已固定 Dockerfile 构建、`/health` 部署健康检查和失败重启策略。应用启动时会在全新的 PostgreSQL 中幂等创建 `video_resources` 与 `video_runs` 两张专用表。

不需要 Railway Volume：可恢复状态在 PostgreSQL，长期媒体在 TOS，本地工作目录只是可丢弃的渲染缓存。
生产环境先保持一个 App Service 副本和 `REMOTION_CONCURRENCY=1`；需要水平扩容时再把 Worker 拆成独立服务。

## 必需配置

```text
DATABASE_URL
ADMIN_USERNAME
ADMIN_PASSWORD
SESSION_SECRET
OPENROUTER_API_KEY
TIKHUB_API_KEY
ARK_API_KEY
VOLCENGINE_SPEECH_API_KEY
VOLCENGINE_TOS_ACCESS_KEY_ID
VOLCENGINE_TOS_SECRET_ACCESS_KEY
VOLCENGINE_TOS_BUCKET
VOLCENGINE_TOS_REGION
QIJIA_VIDEO_STORAGE=tos
```

Seedream 与 Seedance 复用 `ARK_API_KEY`。豆包 TTS 默认复用 `VOLCENGINE_SPEECH_API_KEY`。
中国大陆的 TikHub 默认地址为 `https://api.tikhub.dev`。`QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS` 默认采用 TikHub 文档公开的常见基础价 `$0.001/成功请求` 做规划估算；具体端点价格、每日阶梯折扣和最终费用始终以 TikHub 账单为准：<https://docs.tikhub.io/4579905m0>。

## 验证

```powershell
python -m pytest tests/test_qijia_video.py tests/test_standalone_app.py tests/test_topic_research.py -q
node --test tests/qijia_video_frontend.test.js
npm.cmd run typecheck --prefix video_renderer
```

真实部署验收至少包括：登录、生成并确认脚本、完整旁白、五张首帧、三段视频、Remotion 成片、发布包下载，以及服务重启后的任务可见性。

## 数据边界

- 新服务默认不迁移旧任务；旧系统可暂时只读保留。
- TOS 继续使用 `qijia-video/` 前缀，无需复制历史媒体。
- 新任务 ID 使用随机命名，不会覆盖旧对象。
- 历史任务如需迁移，应单独导出、校验并导入，不与首次上线绑定。
