# 模块边界

`qijia_video` 是以家庭教育为默认场景、通过 Content Skill 扩展内容领域的短视频生产模块。Content Skill 负责输入方式、研究策略、内容提示词和质量边界；Visual Style 负责视觉语言，Prompt Writing Profile 负责模型无关的提示词结构；脚本后续的 TTS、Seedream、Seedance、Remotion、FFmpeg、存储、成本账本与状态机保持统一。

- `contracts.py`：来源卡、脚本、分镜、生成请求、资产、发布包和抖音播放快照协议；
- `content_skills/`：`explain-expert-view` 与 `brief-recent-news` 的版本化 `SKILL.md`、manifest 和提示词资源；
- `skill_registry.py`：Skill 校验、按内容格式路由、公开目录与任务不可变快照；
- `visual_styles/`、`prompt_writing_profiles/`：独立版本化的视觉语言与内部结构化多模态提示词资源；
- `visual_style_registry.py`、`visual_prompting.py`：风格/方法快照冻结，以及到分镜、首帧和视频提示词的供应商无关编译；
- `service.py`：状态机、完整生产链路、逐镜头混合素材版本、抖音回流与单视频 ROI；
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

待确认成片支持逐镜头上传图片或视频。上传版本与 AI 版本并存，当前选择只通过 `StoryboardShot.selected_media_id` 指向，不删除历史；视频先由 FFmpeg 标准化为静音 H.264 并匹配章节时长，再沿用单镜头重渲染、自动质检和失败回滚链路。

新增 Content Skill 时应复制目录契约而不是生产代码：使用动词开头的稳定 `skill_id`，提升 `manifest.json` 语义版本，声明唯一的 `input_mode`、兼容 `content_format`、研究模式和内容提示词文件。纯视觉变化新增 Visual Style，不复制内容研究逻辑；提示词组织方法只进入内部 Prompt Writing Profile，不做用户可选 Content Skill。新版本只影响之后创建的任务；已存在任务始终使用自身快照和冻结提示词。需要新的外部研究能力时扩展受限 Provider 方法，不允许 Skill 直接执行任意工具。

完整部署、配置、测试和数据边界见仓库根目录 `README.md`。
