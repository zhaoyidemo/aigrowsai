# 模块边界

`qijia_video` 是以家庭教育为默认场景、通过工作流预设扩展内容领域的短视频生产模块。Content Skill 只负责输入路由、研究模式、事实政策和质量规则；Visual Style 只负责艺术语言；固定的 H3 Prompt Writing Profile 把原始输入与 EvidencePack 收敛为唯一 CreativeBrief，再统一约束脚本和视觉提示词。TTS、Seedream、Seedance、Remotion、FFmpeg、存储、成本账本与状态机保持统一。

- `contracts.py`：来源卡、脚本、分镜、生成请求、资产、发布包和抖音播放快照协议；
- `content_skills/`：`explain-expert-view` 与 `brief-recent-news` 的版本化 `SKILL.md` 和工作流 manifest，不包含运行时提示词；
- `skill_registry.py`：Skill 校验、按内容格式路由、公开目录与任务不可变快照；
- `visual_styles/`、`prompt_writing_profiles/`：独立版本化的视觉语言与 H3 内部提示词编排资源；
- `prompt_orchestration.py`：编译任务专属证据研究问题，并把原始输入与研究结果收敛为唯一 EvidencePack 和 H3 CreativeBrief 输入；
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

新任务不再接受或展示自由 `script_prompt`、`seedance_prompt`、`image_count` 或 `shot_count`；调用方只选择任务类型、Visual Style、画质与配音。付费联网前会冻结 `research_prompt_snapshot`，研究只交付 EvidencePack；同一次脚本调用返回唯一 CreativeBrief 与 ScriptDraft v3，后续按语义自适应规划章节和最多 3 段必要视频。旧任务快照中的历史字段仍可读取，用于保持已经产生资产的任务可恢复。

待确认成片支持逐镜头上传图片或视频。上传版本与 AI 版本并存，当前选择只通过 `StoryboardShot.selected_media_id` 指向，不删除历史；视频先由 FFmpeg 标准化为静音 H.264 并匹配章节时长，再沿用单镜头重渲染、自动质检和失败回滚链路。

新增 Content Skill 时应复制目录契约而不是生产代码：使用动词开头的稳定 `skill_id`，提升 `manifest.json` 语义版本，并只声明 `input_mode`、兼容 `content_format`、研究模式、政策 ID 与质量规则。不得给 Skill 新增研究、脚本或视觉提示词文件；任务专属研究问题由证据编排器生成，创作决策只由 H3 CreativeBrief 生成。纯视觉变化新增 Visual Style，不复制内容研究逻辑。所有新任务固定使用 H3 Prompt Writing Profile，不把提示词方法做成用户可选 Content Skill；旧字段与 Profile 只用于历史快照兼容。需要新的外部研究能力时扩展受限 Provider 方法，不允许 Skill 直接执行任意工具。

完整部署、配置、测试和数据边界见仓库根目录 `README.md`。
