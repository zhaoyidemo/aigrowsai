# 齐家 AI 知识视频工作台

面向齐家 AI 家庭教练抖音账号、同时可扩展到其他内容领域的选题与短视频生产服务。生产 Web 新任务使用 `Pipeline v4`：用户原始请求不经研究简报、Content Policy、EvidencePolicy、CreativeBrief、EditorialPlan 或 H3 前置改写，直接进入唯一 Script Skill；脚本确认后才进入逐段精确配音、两阶段导演与独立审片、H3 多模态提示词编译和混合制作。视频创作主链不联网，模型可直接使用用户材料与自身已有知识。

环境变量中的管理员账号可以创建、启停同事账号，授予或收回工作台使用权限，并重置密码。同事可以查看团队创建的全部内容、成本和抖音效果，只能修改和继续执行自己创建的内容；管理员可以管理全部内容。

## v4 职责边界

- Script Skill：`insight-led-scriptwriter@1.2.0` 直接读取原始请求与用户材料。DGrid 上的 `anthropic/claude-fable-5` 依次完成主编初稿、非阻断编辑建议和主编终稿；编辑只提供建议，没有评分或否决权。唯一业务交付是等待创作者确认的 `ScriptDraft`，不包含视觉决策。
- Director Skill：脚本人工确认且 TTS 完成后，`animated-explainer@2.4.0` 使用同一 DGrid Claude Fable 5。视觉方向与资产连续性分别通过结构化调用锁定，再以扁平章节对象填充固定 `chapter_01...chapter_N` 槽位并归一化为 `StoryboardPlan + ShotContextIR`，最后使用隔离上下文独立审片。审片发现关键问题时最多追加一次定向修订和复审；审片格式偏差或仍有改进建议不会中断交付，因此正常 4 次、最多 6 次。上传参考图不进入 DGrid。人类文档、来源说明与媒体平台语法不会进入模型请求。
- DGrid 传输层：脚本、导演和选题编辑只发送单一 `anthropic/claude-fable-5` 模型请求，不携带备用模型列表。请求只使用 DGrid 官方 Chat Completions 字段；最终脚本和导演产物执行必要结构校验，内部编辑建议采用宽容解析，缺失或格式偏差不会阻断脚本交付。
- Visual Style：四套风格只定义资产、材质、造型、色彩、构图、运动语法和验收标准，不选择论点，也不改写导演事件。
- H3 Provider Adapter：只在脚本与导演之后工作，把创建页上传图固定为全局风格参考，并把冻结的导演产物连同版本化图片/视频框架、负向边界和真实视频时长编译为 Seedream/Seedance 自然语言提示词；不参与脚本创作。三张视觉开发样片固定比较同一代表性事件；若用户上传参考图，原图只发送给 Seedream。正式首帧再按固定顺序同时输入原图与已选样片，分别保持原图风格与已确认视觉基线。
- 媒体生产：先生成三张视觉开发样片，由编辑锁定一张，再为未上传自有素材的章节调用 Seedream 5 Lite 和测试期默认 Seedance 1.5 Pro。样片确认是正式批量生成前唯一新增的人工质量门。

两种纸艺 Visual Style、电影级风格化 3D Visual Style 与 H3 Provider Adapter 借鉴了 [MiniMax-H3](https://github.com/MiniMax-AI/MiniMax-H3) 的多模态提示词结构、参考职责分离、纸张拼贴、纸艺定格和风格化三维动画方法；这里只提炼通用制作原则，不复刻特定工作流或品牌风格，不耦合 MiniMax 模型，也不新增 MiniMax API Key。`animated-explainer@2.4.0` 借鉴并重新编排了 MIT 许可的 [s1dashu/director](https://github.com/s1dashu/director) 中具体事件、调度、摄影机与连续性原则，不引入其研究、脚本、语音或 CLI 链路。

生产新任务只冻结 `input_snapshot`、`script_skill_snapshot`、`director_skill_snapshot`、`visual_style_snapshot` 与 `provider_adapter_snapshot`；`skill_snapshot` 和 `prompt_adapter_snapshot` 必须为空。Content Skill、旧 H3 Script Adapter 与 Prompt Writing Profile 只用于读取、恢复 v1/v2/v3 历史任务，并采用按需加载，不进入或阻塞 v4 创建链路。只有 Visual Style 是公开选择，目录接口为 `GET /api/qijia-video/visual-styles`。

工作台创建区只展示自然语言主输入、四种同场景视觉样片、可选参考图和生产规格。内部 Adapter、Policy、Script Skill、Director Skill、Provider、模型名与中间规划不在创建页或脚本确认页展示。发布包的 `pipeline_snapshot.json` 仍记录冻结版本，供故障审计而非参与创作。

成片阶段的单镜头重生成只接受编辑者填写的 `revision_intent`。服务端会把它与冻结的 `VisualBible`、`ShotContextIR`、首帧和参考图角色通过 Provider Adapter 重新编译；前端只读展示最终提示词，不允许直接编辑或提交。局部修改不会反向改写脚本或导演世界，也不会重做旁白、图片章节或其他视频镜头。

新任务 API 只接受原始请求、用户明确核对的材料、Visual Style、画质、配音和视频模型规格；不接受 Content/Script/Director/Provider 选择，也不接受 `script_prompt`、`seedance_prompt`、`image_count` 或 `shot_count`。已经冻结的历史任务仍可由服务层恢复和重试，但旧创建协议不再公开。

## 当前真实链路

```text
入口 A：TikHub 抖音家庭教育数据 → 5 个候选与成本记录 → 人工采用并补充可靠来源
入口 B：统一自然语言创作请求
两种入口汇入统一生产链路
  → 原子冻结原始 CreativeInput、唯一 Script Skill、唯一 Director Skill、Visual Style 与 Provider Adapter
  → 原始输入与用户材料直接进入 Script Skill；不生成研究简报、Content/Evidence Policy、CreativeBrief 或 EditorialPlan
  → Claude Fable 5 主编初稿 → 非阻断编辑建议 → 同一主编完成终稿
  → 人工修改并确认脚本；确认后的 ScriptDraft 成为内容唯一真相
  → 豆包 TTS 2.0 按 ScriptBeat 合成并实测每段时长，再无缝拼成一条完整旁白
  → Director 第一阶段交付 DirectorTreatment、VisualBible 与 AssetBible
  → Director 第二阶段按真实 TTS 时长交付 StoryboardPlan 与 ShotContextIR
  → 独立 Director Critic 审片；必要时只做一次定向修订与复审
  → 图片为默认媒介；仅在连续动作不可替代时使用视频，全片最多 3 段但不要求凑满
  → H3 Provider Adapter 编译三张 Seedream 视觉开发样片；若有上传参考图则仅作为全局风格参考共同使用 → 人工锁定一张
  → 按文字分镜连续上传自有图片 / 视频，并一次确认视觉与素材安排
  → H3 Provider Adapter 只为未上传素材的章节编译 Seedream 5 Lite / Seedance 1.5 Pro 提示词；正式首帧按序使用原图与已选样片
  → Remotion 合成 480P / 720P / 1080P 竖屏成片（新任务默认 1080P）
  → 人工混合制作：逐镜头保留 AI，或连续上传自有图片 / 视频并暂存修改
  → 一次应用全部待处理镜头，只重新合成和自动质检 1 次
  → 人工确认成片
  → final.mp4 与发布包
  → 人工发布到抖音
  → 手填抖音作品链接
  → TikHub 作品表现快照、成本与 10 倍 ROI
```

抖音趋势只用于解释“为什么值得研究”，不会自动成为 `verified_materials`。生产链路不使用 Mock，也不会自动发布到抖音。Mock Provider 只存在于显式 CLI 演示和测试中。

新任务不预设图片或视频数量。Script Skill 先写完整论证，Director Skill 再按确认脚本与真实 TTS 时长规划章节，不逐句配图、不重复构图，也不为配额拆镜。每章必须先成立一个可观察的具体事件，隐喻只能作为可选辅助。每个视觉章节生成一张 Seedream 图；只有连续动作、状态转变或镜头运动对理解不可替代、章节旁白不超过 10 秒且动作可在 8 秒内完成时才继续生成 Seedance，最多 3 段但可以为 0。成本按实际章节与实际视频数计算。历史任务中已经冻结的固定数量仍按原规格恢复。

v4 脚本确认后固定先进入视觉开发和素材安排，不再提供“是否先安排素材”的旧复选框。系统生成正式旁白、两阶段导演产物、独立审片结果和三张同事件视觉样片后暂停；此时尚未生成正式镜头。编辑锁定一张样片，并可连续上传多个自有镜头素材；上传和选择只做校验、转码与保存，不触发 AI 生成或 Remotion 渲染。一次确认后，Seedream 和 Seedance 只处理没有自有素材的镜头，再由 Remotion 合成和质检首版成片一次。

成片确认前仍可继续逐镜头混合制作：每个章节都可以保留 AI 素材，也可以上传 JPG、PNG、WebP 图片（最大 20 MB）或 MP4、MOV、WebM 视频（最大 200 MB）。生产环境由服务端签发短期 TOS PUT 地址，浏览器直传并显示进度，再以小型确认请求启动后台处理，避免大文件请求经过 Cloudflare/Railway 后触发 524。上传图片会完成格式校验，上传视频会先由 FFmpeg 转为静音 H.264、从开头截取，并在不足章节时长时冻结最后一帧；这些结果先作为“待应用修改”暂存，不会立即重渲染成片。编辑可以继续替换多个镜头、逐个或全部撤销，最后点击一次批量应用，由 Remotion 只合成和质检一次。上传、历史上传版本和 AI 版本都保留在任务中，可随时选择；任何一次处理、渲染或质检失败都不会覆盖上一版可确认成片，待应用清单也会保留以便重试。

旁白只开放火山引擎公开的 3 个 Seed-TTS 2.0 音色：Vivi 2.0、流畅女声、儒雅逸辰；语速只开放 `1.0x / 1.1x / 1.2x`，新任务默认 `1.2x`，历史任务保持 `1.0x`。脚本确认页可以用真实开场旁白试听，单次最多 60 字、费用写入任务成本账本，同一页面内相同脚本/音色/语速的重复播放不会再次调用。正式配音以每个 `ScriptBeat` 为语义与计时单元分别合成、实测音频时长并零间隔拼接为唯一旁白资产；字幕、Director 章节和剪辑位置直接使用这些实测边界，不再按字数比例猜测。音色 ID 依据[火山引擎 TTS 更新说明](https://www.volcengine.com/docs/6561/162929?lang=en)，三档产品语速依据[火山引擎语速参数说明](https://www.volcengine.com/docs/6348/1807452?lang=zh)分别映射为 `speech_rate=0/10/20`。

## 抖音效果回流（一期）

- 本版本只采集抖音作品的播放、点赞、评论、分享和收藏累计数据，明确忽略小红书和视频号，也不采集 APP 下载或注册归因。
- 视频完成发布包并由人工发布后，在该视频详情中粘贴抖音分享文本、`v.douyin.com` 短链或标准作品链接。系统不会自动发布，也不会要求抖音账号登录授权。
- 标准作品链接会在本地提取作品 ID 后调用 TikHub 星图 V2 作品指标端点；短链首次先使用 TikHub Web 分享链接接口解析作品 ID，再读取星图指标。绑定成功后只按已保存的作品 ID 手动刷新，不做定时轮询。
- 作品数据不会自动更新；视频详情中的“手动刷新作品数据”和团队效果看板每行的“更新此视频”每次都只发起 1 次星图指标请求（规划成本 `¥0.0134`）。标准链接首次绑定也是 1 次；无法本地提取 ID 的 `v.douyin.com` 短链首次绑定需要 2 次请求，规划成本最高 `¥0.0201`。看板范围内只有 1 条可更新作品时，也可使用顶部的单条更新按钮；有多条时必须在表格中逐条选择，避免一次点击产生多笔费用。成功、明确失败响应和网络结果未知分别按“规划价、¥0、待对账”入账。播放量缺失或低于已有累计快照时不会保存本次数据；TikHub 未返回的互动指标保存为未知并显示“未返回”，不会伪装成 0。
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

接口边界按 2026-08-07 的 TikHub 官方文档收敛：

- 六组家庭教育关键词分别读取 TikHub 抖音“低粉爆款榜”和“高点赞率榜”：每次取第一页 20 条、不限制视频时长，低粉固定 `date_window=72`，高点赞补充固定 `date_window=168`。两个榜单都由官方声明支持关键词、滚动时间窗口和分页：<https://docs.tikhub.io/252393854e0>、<https://docs.tikhub.io/252393856e0>
- `date_window` 是榜单查询窗口，不等同于作品发布时间限制。系统优先排序发布 72 小时内的视频，其次是 7 天内；更早作品如仍在当前榜单，会标记为回潮线索而不是直接淘汰。发布时间缺失也不会按 0 或异常样本处理。
- TikHub 没有公开“低粉爆款”的内部判定公式，因此榜单身份与齐家指标复核分开表达。进入 TikHub 低粉爆款榜且通过数据完整性、家庭教育相关性和去重检查的视频可作为“平台低粉榜样本”；强/潜力阈值只增加“指标已复核”标签，不再决定能否入池。原强复核参考为粉丝不超过 5 万、播放不少于 50 万、播粉比不低于 20、赞播比不低于 5%、深度互动率不低于 0.8%；潜力复核参考为粉丝不超过 10 万、播粉比不低于 10、赞播比不低于 3%、深度互动率不低于 0.3%，并按发布年龄参考 10 万或 20 万播放。深度互动率按 `(评论 + 分享 + 收藏) / 播放` 计算。
- 榜单归一化后调用抖音 Web 批量视频详情，一次最多支持 50 个作品 ID、固定 `$0.001/次`；详情失败或单项字段缺失时保留榜单样本，缺失值不参与指标复核，也不按 0 淘汰：<https://docs.tikhub.io/244469112e0>
- 效果回流使用星图 V2 作品指标端点，从同一次响应读取 `watch_cnt` 总播放量和点赞、评论、分享、收藏累计数据，固定 `$0.002/次`；该播放量按官方定义包含投流播放。TikHub 已说明多数普通抖音端点不再返回播放量，因此 Web 批量详情不再作为效果回流来源。无法本地提取作品 ID 的 `v.douyin.com` 短链首次仍通过 Web 分享链接端点解析 ID，再发起星图请求：<https://docs.tikhub.io/493289600e0>、<https://docs.tikhub.io/186826221e0>
- 家庭教育相关性使用“受控检索词 + 视频标题”共同判断，仍会直接排除孕期、奶粉、辅食、纸尿裤等泛母婴内容。每轮保存低粉榜样本整理结果，展示唯一可用数、批量详情补齐数、指标强/潜力复核数、榜单待补数，以及发布时间和粉丝字段缺失等排序观察；只有作品 ID/标题异常、偏离家庭教育和跨检索重复会被移出可用池。
- 作者粉丝数是采集时快照，不是视频发布前的粉丝数；当前接口也不返回 DOU+ 或其他投流支出，因此系统不会把“低粉爆款”进一步表述成“纯自然流量爆款”。
- 中国大陆使用官方建议的 `api.tikhub.dev` 加速域名：<https://docs.tikhub.io/4579297m0>
- 编辑模型使用 DGrid 响应记录 Token，并以 `DGrid-Request-ID` 查询不可变 `billing-json` 计费快照：<https://docs.dgrid.ai/api-reference/usage-and-billing/get-request-billing-details>

TikHub 文档的示例响应没有提供稳定的业务 `data` 样例，因此适配器采用保守归一化，兼容常见的大小写、下划线和榜单字段变体。必须至少有 8 条可用榜单视频且其中至少 5 条来自低粉爆款榜，否则不会调用编辑模型；每个候选必须由至少 2 条独立榜单视频共同验证且包含低粉榜样本，五个候选合计引用至少 8 条不同榜单视频和 5 条不同低粉榜视频，排名前三位还必须分别引用不同的低粉榜视频。排序先看低粉榜身份与发布时间，再看指标复核层级、跨榜单出现、日均播放、播粉比和播放量。不保存庞大的原始响应，只保存候选可复核所需的证据快照、样本整理结果、请求 ID 和字段结构摘要。

## 成本账本与团队效果分析

登录后从工作台右上角进入 `/qijia-video/costs`。查看、筛选、排序、导出 CSV 和“重新载入看板”都只读取已保存数据，不会触发 TikHub、DGrid 或火山引擎调用。只有明确点击“更新这条抖音数据”或表格中的“更新此视频”才会调用 TikHub，而且每次严格只更新 1 个唯一作品并显示预计人民币成本。管理员和有工作台权限的同事看到同一份团队账本与内容效果；成员只能为自己创建的内容产生刷新费用，管理员可更新全部。

团队效果看板只汇总已手填绑定的抖音作品，首屏聚焦累计播放、累计视频成本、整体 ROI、团队 10 倍目标差距和单条视频 ROI 排行；供应商、阶段、调用明细和计价依据默认折叠在“成本明细与对账”中，效果 CSV 同时导出最新点赞、评论、分享和收藏累计数据。时间范围按作品首次绑定时间确定发布批次；手动刷新不会把旧作品移入新批次。作品一旦纳入，播放量使用最新累计快照，成本使用该视频全生命周期累计成本，避免用累计播放除以局部成本造成虚高 ROI。同一抖音作品被误绑定到多个任务时，明细保留全部绑定供排查，团队合计只按最早绑定任务计一次，刷新入口也不会为重复绑定重复请求。尚未绑定的已打包视频只计入回流覆盖缺口；选题研究费用不强行分摊到单条视频。视频详情和团队看板的付费刷新都会在原位显示读取中、成功摘要、权限提示或 Provider 失败原因；即使累计数据没有变化也能确认本次操作结果。

账本覆盖当前全部内容生产收费节点：

- TikHub：选题研究和抖音效果回流都逐次保存成功/失败、端点和请求 ID，按成功请求规划价估算；原始美元金额在报表中固定按 `1 USD = ¥6.7` 换算。
- DGrid：选题编辑、脚本生成和分镜生成逐次保存 Token 与 `DGrid-Request-ID`，并查询单次请求 `billing-json.total_cost`；账单暂未可读时按 Fable 5 公开价保存估算，记录动作先于下游 JSON 和质量门禁。
- Seedream：按成功生成图片数保存当次 CNY 单价快照；失败或结果未知的请求保留为待对账。
- Seedance：测试期由代码内 `qijia_video/model_registry.py` 唯一决定新任务默认模型，当前为 1.5 Pro 无声模式；Railway 只保存凭证和基础设施配置，不保存具体模型型号。1.0 Pro Fast 是更低成本选项，2.0 是单镜头高质量升级选项。前端从 `/capabilities` 读取同一值并展示，不维护第二套默认。每次请求冻结模型，并按该模型的 `usage.total_tokens` 和 CNY 刊例价保存成本快照；失败不会偷偷切换模型，未知提交保留为待对账。
- 豆包语音：逐个实际合成请求保存发送字符数，按当次 CNY 单价快照估算；音频返回后即入账，不受后续本地音频处理结果影响。

报表只显示人民币，“已计成本”统一计算为：`供应商回传金额 + 有计价依据的估算`。所有原始 USD 成本固定按 `1 USD = ¥6.7` 换算，底层账本仍保存供应商原始币种和金额，便于对账。供应商未回传金额、Token 缺失或网络结果未知的调用显示为“待对账”，不会按 0 元伪装成完整成本。页面提供团队效果、10 倍目标进度、视频排行、时间趋势、供应商、生产阶段、创建人、每项内容、最近调用明细，以及成本与效果两份 CSV 导出。

默认估算依据为 TikHub 选题研究及短链解析 `$0.001/成功请求`（报表显示 `¥0.0067/成功请求`）、TikHub 抖音效果回流 `$0.002/成功请求`（`¥0.0134/成功请求`）、Claude Fable 5 公开价 `$10/百万输入 tokens + $50/百万输出 tokens`、Seedream `¥0.22/张`、Seedance 1.0 Pro Fast 无声视频 `¥4.2/百万 tokens`、Seedance 1.5 Pro 无声视频 `¥8/百万 tokens`、Seedance 2.0 无视频输入 `¥46/百万 tokens`、豆包语音 `¥5/万字符`。脚本确认页使用三次已发生脚本调用的 DGrid 计费快照，并补入尚未执行的 Director 正常 4 次、审片修订时最多 6 次的 token 区间预估；画面用量卡同时计入 3 张视觉样片、所有正式首帧及全部 Seedance 版本。DGrid 调用发生后始终以 `billing-json` 的不可变计费快照取代公开价估算。价格可用 `QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS`、`QIJIA_VIDEO_TIKHUB_PERFORMANCE_USD_PER_SUCCESS`、`QIJIA_VIDEO_SEEDREAM_PRICE_PER_IMAGE`、`QIJIA_VIDEO_SEEDANCE_10_FAST_PRICE_PER_MILLION`、`QIJIA_VIDEO_SEEDANCE_15_PRICE_PER_MILLION`、`QIJIA_VIDEO_SEEDANCE_20_PRICE_PER_MILLION` 和 `QIJIA_VIDEO_TTS_PRICE_PER_10000_CHARACTERS` 覆盖；每次可计价调用保存当时快照，之后改价不会重写新账本记录。最终仍以 [TikHub 账单说明](https://docs.tikhub.io/4579905m0)、[DGrid request billing](https://docs.dgrid.ai/api-reference/usage-and-billing/get-request-billing-details) 和[火山引擎豆包大模型计价](https://www.volcengine.com/product/doubao/)为准。

范围刻意只包含模型与数据 API，不包含 Railway、TOS、带宽、人工、税费和购买积分手续费。切换 DGrid 前的历史脚本与分镜可能只保存 OpenRouter `usage`，当前账本继续按原 Provider 展示且不会改写；更早缺少用量记录的金额无法可靠反推。历史图片、视频和语音仅在有保存产物或 Token 时按当前配置补算，并明确标记为历史估算。

## 独立架构

- `qijia_video/`：领域契约、工作流、Provider、API、鉴权和 Web 页面。
- `qijia_video/content_skills/`、`qijia_video/skill_registry.py`：只读兼容 v1/v2/v3 内容快照；不进入 v4 创建链路。
- `qijia_video/script_skills/`、`qijia_video/script_skill_registry.py`：唯一脚本负责人的版本化方法与冻结快照。
- `qijia_video/director_skills/`、`qijia_video/director_skill_registry.py`、`qijia_video/director_prompting.py`：独立 Director Skill、DirectorTreatment、VisualBible、AssetBible、具体事件与 ShotContextIR 编排；
- `qijia_video/visual_styles/`、`qijia_video/visual_style_registry.py`：独立、可组合且不拥有导演决策的视觉语言；历史 Profile 读取逻辑继续隔离保留。
- `qijia_video/provider_adapters/`、`qijia_video/provider_adapter_registry.py`、`qijia_video/provider_prompting.py`：Seedream/Seedance 末端提示词编译，不参与脚本或导演决策。
- `qijia_video/prompt_orchestration.py`：v4 只把未经改写的原始输入、用户材料和唯一 Script Skill 编译为脚本提示词；旧编译器仅恢复历史任务。
- `qijia_video/infrastructure/postgres_repository.py`：视频任务与历史兼容数据的聚合仓储。
- `qijia_video/accounts.py`、`qijia_video/account_api.py`：同事账号、密码哈希、会话失效与管理员 API。
- `qijia_video/run_service.py`：后台任务进度、互斥和重启恢复。
- `qijia_video/topic_*`：选题契约、确定性研究流程、任务执行与 API。
- `qijia_video/cost_analysis.py`、`qijia_video/cost_api.py`：统一成本账本归一化、团队分析 API、只读报表动作与受控单条抖音回流入口。
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
DGRID_API_KEY
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

TOS 存储桶必须配置 CORS：`AllowedOrigins` 至少包含实际工作台域名（生产为 `https://aigrowsai.com`），`AllowedMethods` 包含 `PUT`，`AllowedHeaders` 包含 `*`。直传只使用 15 分钟有效的对象级签名，TOS AK/SK 始终留在服务端；建议给 `qijia-video/staged-uploads/` 配置 1 天生命周期，兜底清理用户关闭页面后未确认的临时对象。

中国大陆的 TikHub 默认地址为 `https://api.tikhub.dev`。`QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS` 是选题研究及短链解析的底层供应商原币规划价；报表统一按固定汇率换算为 `¥0.0067/成功请求`。配合默认的 100 次请求硬上限，单轮选题 TikHub 硬上限为 `¥0.67`，当前固定流程只计划 13 次请求（约 `¥0.0871`）。抖音效果回流由 `QIJIA_VIDEO_TIKHUB_PERFORMANCE_USD_PER_SUCCESS` 单独配置，默认 `$0.002`，报表显示 `¥0.0134/成功请求`；具体端点价格、每日阶梯折扣和最终费用始终以 TikHub 账单为准：<https://docs.tikhub.io/4579905m0>。

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

真实部署验收至少包括：登录后默认进入视频生产，确认创建页“当前真实生产链路”与 `/capabilities.production_pipeline` 完全一致，并验证任务详情中的冻结 Skill、实际模型、调用次数、成本与产物均来自任务轨迹而非前端硬编码；验证选题未配置不会锁死脚本入口。用统一创作请求检查 `pipeline_version=v4`、原始输入逐字冻结、`skill_snapshot=null`、`prompt_adapter_snapshot=null` 且没有外部检索；确认脚本产生“主编初稿、编辑建议、主编终稿”三条 DGrid 用量记录，编辑建议格式异常或请求失败不会阻断终稿，最终脚本哈希仍与结构完整性记录一致。人工确认脚本后，核对逐段实测 TTS 时间轴、两阶段 Director 产物、独立审片结果、三张同事件视觉样片和样片选择门；确认样片前不得出现正式首帧或 Seedance 请求。随后验证自有素材跳过对应 AI 生成、测试期 Seedance 1.5 Pro 默认且方舟真实可提交、Remotion 成片、发布包、成本数据、服务重启恢复及 v1/v2/v3 历史任务读取。

## 数据边界

- 新服务默认不迁移旧任务；旧系统可暂时只读保留。
- TOS 继续使用 `qijia-video/` 前缀，无需复制历史媒体。
- 新任务 ID 使用随机命名，不会覆盖旧对象。
- 历史任务如需迁移，应单独导出、校验并导入，不与首次上线绑定。
