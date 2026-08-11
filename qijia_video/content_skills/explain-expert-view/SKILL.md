---
name: explain-expert-view
description: 将一段自然语言创作请求转化为有来源边界的知识短视频。适用于人物、引语、观点、问题或主题解读，以及需要区分原话、转述、语境和编辑解释的内容。
---

# 观点与主题解读

这是 EvidencePipeline 的工作流预设，不是写作或视觉提示词包。它只决定输入类型、是否需要人物研究，以及必须遵守的来源、引语与安全政策；内容创作由任务冻结的 Script Skill 单独负责。

执行以下流程：

1. 把用户的一段完整自然语言创作请求作为不可变原始输入，不在入口拆分或改写。
2. 从原始请求识别主要人物、引语、观点和研究目标，再联网核验归属、可靠原文、出处、语境与必要解释。
3. 研究阶段只输出 EvidencePack，不设计钩子、内容角度或视觉方案。
4. 应用来源完整性、引语完整性、语境保持和主题适用的安全政策。
5. 把 EvidencePack 和冻结政策交给唯一 Script Skill，产出不含视觉决策的 EditorialPlan 与 ScriptDraft。
6. 人工确认脚本后，才由唯一 Director Skill 生成 VisualBible 与 ShotContextIR；本 Skill 不参与该阶段。
