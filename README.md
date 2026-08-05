# 齐家 AI 短视频生产工作台

面向齐家 AI 抖音账号的独立短视频生产服务。输入一个人物和他的观点，系统生成脚本、旁白、五章分镜、Seedream 首帧、三段 Seedance 视频和两段动态图片，最后由 Remotion 合成为可下载的发布包。

## 当前真实链路

```text
人物与观点
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

生产链路不使用 Mock，也不会自动发布到抖音。Mock Provider 只存在于显式 CLI 演示和测试中。

## 独立架构

- `qijia_video/`：领域契约、工作流、Provider、API、鉴权和 Web 页面。
- `qijia_video/infrastructure/postgres_repository.py`：来源卡与视频任务聚合仓储。
- `qijia_video/run_service.py`：后台任务进度、互斥和重启恢复。
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
ARK_API_KEY
VOLCENGINE_SPEECH_API_KEY
VOLCENGINE_TOS_ACCESS_KEY_ID
VOLCENGINE_TOS_SECRET_ACCESS_KEY
VOLCENGINE_TOS_BUCKET
VOLCENGINE_TOS_REGION
QIJIA_VIDEO_STORAGE=tos
```

Seedream 与 Seedance 复用 `ARK_API_KEY`。豆包 TTS 默认复用 `VOLCENGINE_SPEECH_API_KEY`。

## 验证

```powershell
python -m pytest tests/test_qijia_video.py tests/test_standalone_app.py -q
node --test tests/qijia_video_frontend.test.js
npm.cmd run typecheck --prefix video_renderer
```

真实部署验收至少包括：登录、生成并确认脚本、完整旁白、五张首帧、三段视频、Remotion 成片、发布包下载，以及服务重启后的任务可见性。

## 数据边界

- 新服务默认不迁移旧任务；旧系统可暂时只读保留。
- TOS 继续使用 `qijia-video/` 前缀，无需复制历史媒体。
- 新任务 ID 使用随机命名，不会覆盖旧对象。
- 历史任务如需迁移，应单独导出、校验并导入，不与首次上线绑定。
