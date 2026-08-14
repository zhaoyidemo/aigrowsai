# 模块边界

`qijia_video` 是以家庭教育为默认场景、可扩展到其他知识内容领域的短视频生产模块。普通创作入口原子冻结一个自然语言 `creative_request`。生产 Web 新任务统一使用 `Pipeline v4`：原始请求和用户材料直接进入唯一 Script Skill，以“主编初稿—非阻断编辑建议—主编终稿”交付最终 `ScriptDraft`，唯一内容质量门是人工脚本确认；人工确认并完成逐段实测 TTS 后，Director Skill 用两阶段导演与独立审片流程交付视觉方案。H3 方法只在脚本之后负责多模态参考角色和供应商提示词编译，不再改写创作起点。视频创作主链不启用外部检索。

- `contracts.py`：原始创作输入、历史来源卡兼容、脚本、分镜、生成请求、资产、发布包和抖音播放快照协议；
- `content_skills/`：只供 `Pipeline v1/v2/v3` 历史快照恢复，按需加载，不参与或阻塞 v4 新任务；
- `skill_registry.py`：Skill 校验、按内容格式路由、公开目录与任务不可变快照；
- `prompt_adapters/`、`prompt_adapter_registry.py`：只供历史 v3 快照恢复并按需加载；v4 不执行 H3 前置改写；
- `script_skills/`、`script_skill_registry.py`：版本化脚本方法；v4 的 `insight-led-scriptwriter` 统领主编、非阻断编辑建议与终稿，只对外交付最终 `ScriptDraft`；
- `director_skills/`、`director_skill_registry.py`、`director_prompting.py`：版本化 Director Skill，基于确认脚本与真实 TTS 时长，先分别交付 `DirectorTreatment + VisualBible` 与扁平 `AssetBible`，再交付 `StoryboardPlan + ShotContextIR`，最后由隔离上下文独立审片并在必要时完成一次受控修订；
- `visual_styles/`、`visual_style_registry.py`：独立 Visual Style，定义媒介、资产设计、材质、色彩、光线、运动语法和可观察的审美验收标准；
- `provider_adapters/`、`provider_adapter_registry.py`、`provider_prompting.py`：在导演之后以 H3 多模态方法分配参考图角色，并把冻结视觉语义编译为当前图片/视频模型的自然提示词；
- `prompt_orchestration.py`：v4 只组合原始输入、用户材料与 Script Skill；同时保留历史链路编译能力；
- `prompt_writing_profiles/`、`visual_prompting.py`：仅保留给 `Pipeline v1` 历史任务恢复，旧 Profile 注册表按需加载；
- `service.py`：状态机、完整生产链路、逐镜头混合素材版本、抖音回流与单视频 ROI；
- `ports.py`：生成 Provider、抖音播放读取、存储、渲染和仓储端口；
- `infrastructure/`：DGrid、火山引擎、TOS、PostgreSQL、FFmpeg 与 Remotion 适配器；
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
- `model_registry.py`：代码内唯一生产模型目录，部署环境不得覆盖具体型号；
- `web/`：不依赖前端框架的工作台、团队成本效果看板与账号管理页面。

Remotion 位于仓库根目录 `video_renderer/`，只读取 `render_manifest.json` 与本地化素材，不访问数据库，也不调用生成模型。

新任务只接受创作者真正能决定的原始请求、核对材料、Visual Style、可选视觉参考、画质、配音和视频模型规格。v4 不创建 Content Policy、EvidencePolicy、CreativeBrief、EditorialPlan、研究简报或 H3 前置产物；Script Skill、Director Skill、Visual Style 与 Provider Adapter 版本由服务端冻结。三次脚本请求均不携带搜索工具：DGrid 上的 `anthropic/claude-fable-5` 先写主编初稿，再由隔离上下文提供不评分、不裁决的编辑建议，最后由主编决定是否采纳并交付终稿。编辑建议缺失、格式偏差或请求失败均不会阻断终稿。

脚本确认后，TTS 按 `ScriptBeat` 分别合成、实测每段音频并零间隔拼成唯一完整旁白。Director Skill 使用 DGrid 上的 `anthropic/claude-fable-5`：前两次以独立契约分别确定视觉方向和资产连续性，第三次以扁平章节对象逐一填充已锁定的 `chapter_01...chapter_N` 槽位并由系统归一化可执行字段，第四次使用隔离上下文独立审片，必要时追加一次定向修订和一次复审。格式偏差、媒介超限和审片建议不再中断交付：系统会补齐可推导字段、把超时或超额视频确定性改为图片，并把质量意见保留给样片人工确认。上传参考图不进入 DGrid。系统随后以 H3 Provider Adapter `2.1.0` 的冻结方法生成 3 张同事件风格样片并暂停，用户选定一张后才批量生成正式镜头。若用户上传参考图，工作台将其固定为 `global_reference` 风格参考并只发送给 Seedream；正式首帧再按“原图、已选样片”的固定顺序同时输入，分别保持原图风格与已确认视觉基线。

脚本确认页的整单区间包含已经发生的三次脚本 DGrid 计费快照、未来 Director 正常 4 次至最多 6 次的公开价预估、完整 TTS、3 张视觉样片、全部预计正式首帧和最多三段 Seedance。后续画面用量卡只汇总 Seedream 与 Seedance，但同时覆盖视觉样片、正式首帧和所有视频版本；脚本、Director 与 TTS 保留在任务总账，任何无价格调用均显示为待对账。

创建页以自然语言主输入为第一动作，只保留视觉风格这个常用选择，并用同一场景的真实样片进行比较。脚本、导演、选题编辑、图片、视频与配音的具体型号只在 `model_registry.py` 管理，Railway 不提供型号覆盖入口。页面通过 `/capabilities.production_pipeline` 展示后端解析出的真实生产架构，明确区分 AI 同事、创作方法、生成模型、生产工具和人工质量门；任务详情则通过冻结快照、生成产物与用量账本展示本次真实执行、模型、调用和成本。两层信息均只读，不形成第二套前端配置或新的确认门槛。

待确认成片支持逐镜头上传图片或视频。上传版本与 AI 版本并存，当前选择只通过 `StoryboardShot.selected_media_id` 指向，不删除历史；视频先由 FFmpeg 标准化为静音 H.264 并匹配章节时长，再沿用单镜头重渲染、自动质检和失败回滚链路。

新增脚本创作能力时扩展或替换唯一 Script Skill，不在它前面叠加 Content Skill 或 Prompt Adapter；新增导演方法时扩展或替换唯一 Director Skill；新增画风时创建 Visual Style；替换媒体模型时创建 Provider Adapter。Script Skill 只负责作品内容，Director Skill 是视觉叙事决策唯一负责人，Visual Style 是其资产与美术系统，Provider Adapter 只负责编译模型语言。旧字段、Content Skill、H3 前置适配器与 Prompt Writing Profile 只用于历史快照兼容。

`animated-explainer@2.4.0` 借鉴并重新编排了 MIT 许可的 [s1dashu/director](https://github.com/s1dashu/director) 中 Animated Explainer 的具体事件、主体调度与连续性原则；人类文档和来源说明与模型运行时指令分开保存，未引入其研究、脚本、语音或 CLI 流程。MiniMax H3 的借鉴范围限定在导演之后的多模态参考角色和供应商提示词表达，不参与脚本选题与论证。

完整部署、配置、测试和数据边界见仓库根目录 `README.md`。
