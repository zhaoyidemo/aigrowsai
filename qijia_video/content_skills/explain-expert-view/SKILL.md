---
name: explain-expert-view
description: 将思想、历史、教育、心理学、科学或商业人物的观点转化为有来源边界的知识短视频。用于人物观点解读，以及需要区分原话、转述、语境和编辑解释的内容。
---

# 人物观点解读

先由 H3 根据原始输入编排研究问题，再校验出处、语境与来源边界，并依据冻结的研究结果生成脚本。把用户输入的观点视为创作命题，除非来源逐字支持，不得写成人物原话，也不得先套入预设的受众或领域模板。

执行以下流程：

1. 校验人物、观点、来源与解释边界。
2. H3 依据人物与完整原始表述生成任务专属研究提示词；Content Skill 的 [research-prompt.md](references/research-prompt.md) 只补充来源层级和事实边界。
3. 联网核验人物归属、可靠原文、出处、语境与必要解释；研究失败时保留警告并使用原始来源卡。已经核验的概念、书籍或研究来源卡不额外触发人物检索。
4. H3 以原始输入和研究结论为上游约束，再结合 [script-prompt.md](references/script-prompt.md) 生成严格的 `ScriptDraft v2`。
5. 使用 [visual-policy.md](references/visual-policy.md) 约束可见语义的真实性；画风由 Visual Style 提供，分镜、首帧和 I2V 提示词继续由 H3 统一生成。
6. 应用来源完整性、引语完整性、语境保持，以及主题适用的教育心理和未成年人安全规则。

联网研究时遵循 [research-prompt.md](references/research-prompt.md)。
