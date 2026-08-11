# 模块边界

`qijia_video` 是以家庭教育为默认场景、可扩展到其他知识内容领域的短视频生产模块。普通创作入口原子冻结一个自然语言 `creative_request`。新任务统一使用 `Pipeline v3 + Director v3`：内部 H3 Prompt Adapter 编译原始请求，唯一 Script Skill 一次调用只交付 `ScriptDraft`；人工确认后，Director Skill 才负责具体事件、章节、调度、摄影机与媒介选择。Visual Style 只提供美术语言，Provider Adapter 只管模型语法。视频创作主链不启用外部检索。

- `contracts.py`：原始创作输入、历史来源卡兼容、脚本、分镜、生成请求、资产、发布包和抖音播放快照协议；
- `content_skills/`：公开的 `explain-expert-view` Input Policy 与只供历史快照识别的 `brief-recent-news` 定义，不包含运行时提示词；
- `skill_registry.py`：Skill 校验、按内容格式路由、公开目录与任务不可变快照；
- `prompt_adapters/`、`prompt_adapter_registry.py`：内部 H3 Prompt Adapter，把原始请求编译为 Script Skill 指令，不产生中间业务产物；
- `script_skills/`、`script_skill_registry.py`：版本化脚本方法，一次调用唯一交付 `ScriptDraft`；
- `director_skills/`、`director_skill_registry.py`、`director_prompting.py`：版本化 Director Skill，基于确认脚本与真实 TTS 时长交付 `StoryboardPlan v3 + VisualBible + ShotContextIR`；
- `visual_styles/`、`visual_style_registry.py`：独立 Visual Style，只约束媒介、材质、造型、色彩、光线和构图；
- `provider_adapters/`、`provider_adapter_registry.py`、`provider_prompting.py`：把冻结镜头语义编译为当前图片/视频模型提示词；
- `prompt_orchestration.py`：组合原始输入、H3 Prompt Adapter、用户核对材料、模型知识方式和硬政策边界；
- `prompt_writing_profiles/`、`visual_prompting.py`：仅保留给 `Pipeline v1` 历史任务恢复；
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

新任务只接受创作者真正能决定的原始请求、核对材料、Visual Style、画质、配音和视频模型规格；Content Policy、H3 Prompt Adapter、Script Skill、Director Skill 与 Provider Adapter 由服务端固定并随任务冻结。脚本请求不携带 `tools`、`tool_choice` 或 `max_tool_calls`，一次调用只返回 `ScriptDraft`；人工确认并完成 TTS 后，导演自主划分 3–12 个章节，每章先设计具体事件与主体调度，再选择图片或最多 3 段必要视频。

创建页以自然语言主输入为第一动作，只保留视觉风格这个常用选择，并用同一场景的真实样片进行比较。内部角色、模型名、中间规划和技术责任链不在创建页与脚本确认页呈现；冻结版本只进入发布包审计快照。

待确认成片支持逐镜头上传图片或视频。上传版本与 AI 版本并存，当前选择只通过 `StoryboardShot.selected_media_id` 指向，不删除历史；视频先由 FFmpeg 标准化为静音 H.264 并匹配章节时长，再沿用单镜头重渲染、自动质检和失败回滚链路。

新增 Content Skill 时只声明输入、模型知识边界、事实/安全政策和质量规则，并且必须保持 `knowledge_mode=model_knowledge`、`research_mode=none`；不得加入脚本写法、视觉方案或工具调用。新增脚本方法时创建或替换一个 Script Skill；新增导演方法时创建或替换一个 Director Skill；新增画风时创建 Visual Style；替换媒体模型时创建 Provider Adapter。Director Skill 是导演决策唯一负责人，Visual Style 只能作为其美术输入，不得以“叠加总纲”的方式争夺决策。旧字段与 Prompt Writing Profile 只用于历史快照兼容。

`animated-explainer@1.0.0` 借鉴并重新编排了 MIT 许可的 [s1dashu/director](https://github.com/s1dashu/director) 中 Animated Explainer 的具体事件、主体调度与连续性原则；未引入其研究、脚本、语音或 CLI 流程。

完整部署、配置、测试和数据边界见仓库根目录 `README.md`。
