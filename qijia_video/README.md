# 模块边界

`qijia_video` 是以家庭教育为默认场景、通过 Content Skill 扩展内容领域的短视频生产模块。Skill 只负责输入方式、研究策略、内容提示词、视觉方向和质量边界；脚本后续的 TTS、Seedream、Seedance、Remotion、FFmpeg、存储、成本账本与状态机保持统一。

- `contracts.py`：来源卡、脚本、分镜、生成请求、资产、发布包和抖音播放快照协议；
- `content_skills/`：`explain-expert-view` 与 `brief-recent-news` 的版本化 `SKILL.md`、manifest 和提示词资源；
- `skill_registry.py`：Skill 校验、按内容格式路由、公开目录与任务不可变快照；
- `service.py`：状态机、完整生产链路、抖音回流与单视频 ROI；
- `ports.py`：生成 Provider、抖音播放读取、存储、渲染和仓储端口；
- `infrastructure/`：OpenRouter、火山引擎、TOS、PostgreSQL、FFmpeg 与 Remotion 适配器；
- `run_service.py`：后台任务进度、互斥和部署重启恢复；
- `auth.py`：管理员与同事的签名 Cookie 会话；
- `accounts.py`、`account_api.py`：同事账号、权限、密码重置与管理员页面；
- `api.py`：独立 API 与页面路由；
- `topic_contracts.py`、`topic_service.py`：选题证据、候选、成本和确定性流程；
- `topic_ports.py`：TikHub 采集、编辑模型与逐步成本账本的端口；
- `topic_runtime.py`、`topic_api.py`：轻量任务骨架与选题 API；
- `cost_analysis.py`、`cost_api.py`：统一成本账本、团队抖音效果与只读经营分析 API；
- `infrastructure/tikhub.py`：TikHub 抖音选题与作品播放量数据适配器；
- `infrastructure/topic_providers.py`：只基于证据归纳角度的选题编辑模型；
- `web/`：不依赖前端框架的工作台、团队成本效果看板与账号管理页面。

Remotion 位于仓库根目录 `video_renderer/`，只读取 `render_manifest.json` 与本地化素材，不访问数据库，也不调用生成模型。

新增 Skill 时应复制目录契约而不是生产代码：使用动词开头的稳定 `skill_id`，提升 `manifest.json` 语义版本，声明唯一的 `input_mode`、兼容 `content_format`、研究模式和三类提示词文件。新版本只影响之后创建的任务；已存在任务始终使用自身的 `skill_snapshot` 和冻结提示词。需要新的外部研究能力时扩展受限 Provider 方法，不允许 Skill 直接执行任意工具。

完整部署、配置、测试和数据边界见仓库根目录 `README.md`。
