# 齐家 AI 家庭教育内容工作台

面向齐家 AI 家庭教练抖音账号的内容研究与短视频生产服务。工作台先通过 TikHub 研究抖音家庭教育选题，人工采用候选并补充可靠来源后，再生成脚本、旁白、五章分镜和可下载的发布包。

环境变量中的管理员账号可以创建、启停同事账号，授予或收回工作台使用权限，并重置密码。同事可以查看团队创建的全部内容、成本和抖音效果，只能修改和继续执行自己创建的内容；管理员可以管理全部内容。

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
  → Seedance 1.0 Pro Fast 三段所选画质视频 + 两张动态图片（复杂镜头可单独升级 2.0）
  → Remotion 合成 480P / 720P / 1080P 竖屏成片（新任务默认 1080P）
  → 人工确认成片
  → final.mp4 与发布包
  → 人工发布到抖音
  → 手填抖音作品链接
  → TikHub 作品表现快照、成本与 10 倍 ROI
```

抖音趋势只用于解释“为什么值得研究”，不会自动成为来源卡中的已核验事实。生产链路不使用 Mock，也不会自动发布到抖音。Mock Provider 只存在于显式 CLI 演示和测试中。

## 抖音效果回流（一期）

- 本版本只采集抖音作品的播放、点赞、评论、分享和收藏累计数据，明确忽略小红书和视频号，也不采集 APP 下载或注册归因。
- 视频完成发布包并由人工发布后，在该视频详情中粘贴抖音分享文本、`v.douyin.com` 短链或标准作品链接。系统不会自动发布，也不会要求抖音账号登录授权。
- 标准作品链接会在本地提取作品 ID 后调用 TikHub Web 批量详情；短链首次使用 TikHub 官方分享链接接口解析。绑定成功后只按已保存的作品 ID 手动刷新，不做定时轮询。
- 作品数据不会自动更新；每次绑定或点击“手动刷新作品数据”只发起 1 次 TikHub 请求，操作按钮会提前显示人民币规划成本；成功、明确失败响应和网络结果未知分别按“规划价、¥0、待对账”入账。播放量缺失时不会保存本次快照；TikHub 未返回的互动指标保存为未知并显示“未返回”，不会伪装成 0。
- 创建者和管理员可以绑定或刷新；同事可以查看团队全部视频的链接、播放量、成本和 ROI，但不能替别人产生费用。
- 播放价值固定按 `播放量 ÷ 1000 × ¥10`，当前 ROI 为 `播放价值 ÷ 该视频已核算成本`，10 倍目标播放量为 `向上取整(已核算成本 × 1000)`。每次播放量读取成本也计入该视频；未绑定到该视频的选题研究公摊不计入。存在待对账费用时，成本、ROI 和目标都会明确标为暂估。

## 一期选题边界

- 主题固定为家庭教育，不扩展到泛母婴、婚恋或社会热点。
- 数据平台固定为抖音，数据服务固定为 TikHub。
- 当前固定流程每轮计划最多 13 次 TikHub 请求，按固定汇率和当前规划价约 `¥0.0871`；请求硬上限为 100 次，折算为 `¥0.67`。硬上限由 `QIJIA_TOPIC_TIKHUB_REQUEST_BUDGET` 控制，当前流程不会为了用满预算而增加调用。
- 六组低粉爆款榜查询先执行；有效、相关且去重后不足 5 条时会在高点赞率榜查询前提前停止。达到门槛后再执行六组高点赞率榜查询，并用一次批量详情调用补齐最多 50 条视频指标。
- 每轮只调用 1 次编辑模型，输出恰好 5 个候选，不做播放量或爆款预测。
- TikHub 每次调用完成后都会立即保存请求 ID、检索标签、仅含字段名的返回结构摘要与规划成本；不保存原始响应。编辑模型的 Token 和供应商上报费用也会在候选门禁前入账。
- 付费研究在服务重启后不会自动重跑，避免重复计费；用户可人工开始新一轮。

## TikHub 契约依据

接口边界按 2026-08-06 的 TikHub 官方文档收敛：

- 六组家庭教育关键词分别读取 TikHub 抖音“低粉爆款榜”和“高点赞率榜”：每次取第一页 20 条、不限制视频时长，低粉固定 `date_window=72`，高点赞补充固定 `date_window=168`。两个榜单都由官方声明支持关键词、滚动时间窗口和分页：<https://docs.tikhub.io/252393854e0>、<https://docs.tikhub.io/252393856e0>
- `date_window` 是榜单查询窗口，不等同于作品发布时间限制。系统优先排序发布 72 小时内的视频，其次是 7 天内；更早作品如仍在当前榜单，会标记为回潮线索而不是直接淘汰。发布时间缺失也不会按 0 或异常样本处理。
- TikHub 没有公开“低粉爆款”的内部判定公式，因此榜单身份与齐家指标复核分开表达。进入 TikHub 低粉爆款榜且通过数据完整性、家庭教育相关性和去重检查的视频可作为“平台低粉榜样本”；强/潜力阈值只增加“指标已复核”标签，不再决定能否入池。原强复核参考为粉丝不超过 5 万、播放不少于 50 万、播粉比不低于 20、赞播比不低于 5%、深度互动率不低于 0.8%；潜力复核参考为粉丝不超过 10 万、播粉比不低于 10、赞播比不低于 3%、深度互动率不低于 0.3%，并按发布年龄参考 10 万或 20 万播放。深度互动率按 `(评论 + 分享 + 收藏) / 播放` 计算。
- 榜单归一化后调用抖音 Web 批量视频详情，一次最多支持 50 个作品 ID、固定 `$0.001/次`；详情失败或单项字段缺失时保留榜单样本，缺失值不参与指标复核，也不按 0 淘汰：<https://docs.tikhub.io/244469112e0>
- 效果回流复用同一批量视频详情端点，从同一次响应读取播放、点赞、评论、分享和收藏累计数据；`v.douyin.com` 短链首次通过 Web 分享链接接口读取单个作品。官方说明分享链接接口的字段少于 APP 接口，因此除播放量外的指标允许缺失：<https://docs.tikhub.io/257556744e0>
- 家庭教育相关性使用“受控检索词 + 视频标题”共同判断，仍会直接排除孕期、奶粉、辅食、纸尿裤等泛母婴内容。每轮保存低粉榜样本整理结果，展示唯一可用数、批量详情补齐数、指标强/潜力复核数、榜单待补数，以及发布时间和粉丝字段缺失等排序观察；只有作品 ID/标题异常、偏离家庭教育和跨检索重复会被移出可用池。
- 作者粉丝数是采集时快照，不是视频发布前的粉丝数；当前接口也不返回 DOU+ 或其他投流支出，因此系统不会把“低粉爆款”进一步表述成“纯自然流量爆款”。
- 中国大陆使用官方建议的 `api.tikhub.dev` 加速域名：<https://docs.tikhub.io/4579297m0>
- 编辑模型使用 OpenRouter 非流式响应自带的 `usage` 记录 Token 和供应商上报成本，不额外发起费用查询：<https://openrouter.ai/docs/cookbook/administration/usage-accounting>

TikHub 文档的示例响应没有提供稳定的业务 `data` 样例，因此适配器采用保守归一化，兼容常见的大小写、下划线和榜单字段变体。必须至少有 8 条可用榜单视频且其中至少 5 条来自低粉爆款榜，否则不会调用编辑模型；每个候选必须由至少 2 条独立榜单视频共同验证且包含低粉榜样本，五个候选合计引用至少 8 条不同榜单视频和 5 条不同低粉榜视频，排名前三位还必须分别引用不同的低粉榜视频。排序先看低粉榜身份与发布时间，再看指标复核层级、跨榜单出现、日均播放、播粉比和播放量。不保存庞大的原始响应，只保存候选可复核所需的证据快照、样本整理结果、请求 ID 和字段结构摘要。

## 成本账本与团队效果分析

登录后从工作台右上角进入 `/qijia-video/costs`。页面是纯读取报表，查看、筛选、排序和导出 CSV 都不会触发 TikHub、OpenRouter 或火山引擎调用。管理员和有工作台权限的同事看到同一份团队账本与内容效果。

团队效果看板只汇总已手填绑定的抖音作品，首屏聚焦累计播放、累计视频成本、整体 ROI、团队 10 倍目标差距和单条视频 ROI 排行；供应商、阶段、调用明细和计价依据默认折叠在“成本明细与对账”中，效果 CSV 同时导出最新点赞、评论、分享和收藏累计数据。时间范围按作品首次绑定时间确定发布批次；手动刷新不会把旧作品移入新批次。作品一旦纳入，播放量使用最新累计快照，成本使用该视频全生命周期累计成本，避免用累计播放除以局部成本造成虚高 ROI。同一抖音作品被误绑定到多个任务时，明细保留全部绑定供排查，团队合计只按最早绑定任务计一次。尚未绑定的已打包视频只计入回流覆盖缺口；选题研究费用不强行分摊到单条视频。工作台的刷新按钮始终可以响应点击，并在原位显示读取中、成功摘要、权限提示或 Provider 失败原因；即使累计数据没有变化也能确认本次操作结果。

账本覆盖当前全部内容生产收费节点：

- TikHub：选题研究和抖音效果回流都逐次保存成功/失败、端点和请求 ID，按成功请求规划价估算；原始美元金额在报表中固定按 `1 USD = ¥6.7` 换算。
- OpenRouter：选题编辑、脚本生成和分镜生成逐次保存 Token，并使用非流式响应内的 `usage.cost` 作为供应商回传金额；记录动作先于下游 JSON 和质量门禁，报表按固定汇率换算人民币。
- Seedream：按成功生成图片数保存当次 CNY 单价快照；失败或结果未知的请求保留为待对账。
- Seedance：新任务默认使用 1.0 Pro Fast 无声原生 1080P，复杂镜头可在单镜头换版时升级 2.0；每次请求冻结模型，并按该模型的 `usage.total_tokens` 和 CNY 刊例价保存成本快照。1.5 Pro 仅用于兼容已取得 Provider Task ID 的历史任务；从未取得任务 ID 的失败请求会安全迁移到 1.0 Pro Fast。未知提交也保留为待对账。
- 豆包语音：逐个实际合成请求保存发送字符数，按当次 CNY 单价快照估算；音频返回后即入账，不受后续本地音频处理结果影响。

报表只显示人民币，“已计成本”统一计算为：`供应商回传金额 + 有计价依据的估算`。所有原始 USD 成本固定按 `1 USD = ¥6.7` 换算，底层账本仍保存供应商原始币种和金额，便于对账。供应商未回传金额、Token 缺失或网络结果未知的调用显示为“待对账”，不会按 0 元伪装成完整成本。页面提供团队效果、10 倍目标进度、视频排行、时间趋势、供应商、生产阶段、创建人、每项内容、最近调用明细，以及成本与效果两份 CSV 导出。

默认估算依据为 TikHub `$0.001/成功请求`（报表显示 `¥0.0067/成功请求`）、Seedream `¥0.22/张`、Seedance 1.0 Pro Fast 无声视频 `¥4.2/百万 tokens`、Seedance 1.5 Pro 历史无声视频 `¥8/百万 tokens`、Seedance 2.0 无视频输入 `¥46/百万 tokens`、豆包语音 `¥5/万字符`。OpenRouter 的供应商响应金额同样按固定汇率换算。价格可用 `QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS`、`QIJIA_VIDEO_SEEDREAM_PRICE_PER_IMAGE`、`QIJIA_VIDEO_SEEDANCE_10_FAST_PRICE_PER_MILLION`、`QIJIA_VIDEO_SEEDANCE_15_PRICE_PER_MILLION`、`QIJIA_VIDEO_SEEDANCE_20_PRICE_PER_MILLION` 和 `QIJIA_VIDEO_TTS_PRICE_PER_10000_CHARACTERS` 覆盖；每次可计价调用保存当时快照，之后改价不会重写新账本记录。最终仍以 [TikHub 账单说明](https://docs.tikhub.io/4579905m0)、[OpenRouter usage accounting](https://openrouter.ai/docs/cookbook/administration/usage-accounting) 和[火山引擎豆包大模型计价](https://www.volcengine.com/product/doubao/)为准。

范围刻意只包含模型与数据 API，不包含 Railway、TOS、带宽、人工、税费和购买积分手续费。账本上线前的脚本与分镜没有持久化 OpenRouter `usage`，无法可靠反推；历史图片、视频和语音仅在有保存产物或 Token 时按当前配置补算，并明确标记为历史估算。

## 独立架构

- `qijia_video/`：领域契约、工作流、Provider、API、鉴权和 Web 页面。
- `qijia_video/infrastructure/postgres_repository.py`：来源卡与视频任务聚合仓储。
- `qijia_video/accounts.py`、`qijia_video/account_api.py`：同事账号、密码哈希、会话失效与管理员 API。
- `qijia_video/run_service.py`：后台任务进度、互斥和重启恢复。
- `qijia_video/topic_*`：选题契约、确定性研究流程、任务执行与 API。
- `qijia_video/cost_analysis.py`、`qijia_video/cost_api.py`：统一成本账本归一化、团队分析 API 与只读页面。
- `qijia_video/infrastructure/tikhub.py`：TikHub 抖音读接口与请求预算。
- `video_renderer/`：独立 Remotion 渲染包，只消费 Render Manifest。
- PostgreSQL：保存业务聚合、后台运行状态与同事账号；密码只保存带随机盐的 `scrypt` 哈希。
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

`railway.json` 已固定 Dockerfile 构建、`/health` 部署健康检查和失败重启策略。应用启动时会在 PostgreSQL 中幂等创建 `video_resources`、`video_runs` 与 `qijia_users` 三张专用表；已有业务表不会被重写。

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
中国大陆的 TikHub 默认地址为 `https://api.tikhub.dev`。`QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS` 是底层供应商原币规划价；报表统一按固定汇率换算为 `¥0.0067/成功请求`。配合默认的 100 次请求硬上限，单轮 TikHub 硬上限为 `¥0.67`，当前固定流程只计划 13 次请求（约 `¥0.0871`）；具体端点价格、每日阶梯折扣和最终费用始终以 TikHub 账单为准：<https://docs.tikhub.io/4579905m0>。

## 账号与团队共享

- `ADMIN_USERNAME` / `ADMIN_PASSWORD` 是唯一管理员，不写入数据库，也不能在账号管理页被停用。
- 管理员登录后从工作台右上角进入“账号管理”，创建同事账号并设置至少 12 个字符的初始密码。
- 停用账号、收回使用权限或重置密码会递增服务端会话版本，使该账号已有 Cookie 立即失效。
- 同事账号拥有团队内容的只读视图，但付费调用、修改、重试和审批仍只允许内容创建者；管理员不受此限制。
- 账号不提供删除操作，避免历史内容失去创建者标识。

## 验证

```powershell
python -m pytest tests/test_qijia_video.py tests/test_standalone_app.py tests/test_topic_research.py tests/test_accounts.py tests/test_cost_analysis.py tests/test_douyin_performance.py -q
node --test tests/qijia_video_frontend.test.js
npm.cmd run typecheck --prefix video_renderer
```

真实部署验收至少包括：登录、生成并确认脚本、完整旁白、五张首帧、三段视频、Remotion 成片、发布包下载、手填一条真实抖音作品链接并刷新作品数据、核对播放与四项互动指标、单视频与团队看板的 10 倍 ROI、排行和 CSV、与供应商账单抽样核对，以及服务重启后的任务与效果快照可见性。

## 数据边界

- 新服务默认不迁移旧任务；旧系统可暂时只读保留。
- TOS 继续使用 `qijia-video/` 前缀，无需复制历史媒体。
- 新任务 ID 使用随机命名，不会覆盖旧对象。
- 历史任务如需迁移，应单独导出、校验并导入，不与首次上线绑定。
