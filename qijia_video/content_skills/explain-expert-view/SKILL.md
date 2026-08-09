---
name: explain-expert-view
description: 将教育、心理学或思想领域人物的观点转化为有来源边界的知识短视频。用于人物观点解读、家庭教育观点展开，以及需要区分原话、转述和编辑解释的内容。
---

# 人物观点解读

先校验输入与来源边界，再依据来源卡生成脚本。把用户输入的观点视为创作命题，除非来源逐字支持，不得写成人物原话。

执行以下流程：

1. 校验人物、观点、来源与解释边界。
2. 对人物观点输入可选地联网补充有引用的研究简报；研究失败时保留警告并使用原始来源卡。已经核验的概念、书籍或研究来源卡不额外触发人物检索。
3. 使用 [script-prompt.md](references/script-prompt.md) 生成严格的 `ScriptDraft v2`。
4. 使用 [visual-prompt.md](references/visual-prompt.md) 统一分镜和视觉生成。
5. 应用来源完整性、引语完整性、教育心理安全和未成年人安全规则。

联网研究时遵循 [research-prompt.md](references/research-prompt.md)。
