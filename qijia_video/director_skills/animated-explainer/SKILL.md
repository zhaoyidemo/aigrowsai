---
name: animated-explainer
description: 将已确认的知识、人物思想、历史或概念解说口播导演为可生产的视觉章节。用于根据完整 ScriptDraft 与 TTS 时长决定章节边界、具体事件、主体调度、摄影机、连续性、参考素材职责和图片/视频媒介；不用于写脚本、选择画风或编写供应商提示词。
---

# Animated Explainer Director

把确认后的口播视为内容唯一真相。先通读完整脚本与逐段 TTS 时长，再规划全片视觉论证；不得逐句配图，也不得改写事实、引语、观点或旁白。

按以下顺序工作：

1. 确定全片的视觉命题、重复主体和必要连续性锚点。
2. 将相邻 ScriptBeat 合并为 3–12 个语义完整的视觉章节。章节边界服务理解与节奏，不为凑数切分。
3. 为每章设计一个具体事件：主体在可辨认环境中采取行动，引发可见反馈，并落到新状态。
4. 写清主体调度、构图、摄影机、起止状态、连续性和参考素材职责。
5. 图片优先；仅把连续行动、状态转变或镜头运动不可替代且能在八秒内完成的章节标为视频，全片最多三段。
6. 依据质量规则自检，再交付 VisualBible 与 StoryboardPlan v3。

使用 references/scene-design.md 设计事件，使用 references/shot-design.md 写调度与摄影机，使用 references/continuity.md 管理跨章一致性，使用 references/media-policy.md 决定图片或视频。

Visual Style 是独立配置，只定义媒介、材质、造型、色彩、光线和构图语言。Provider Adapter 是独立下游，只翻译已经确定的视觉语义。不得输出 Seedream、Seedance 或其他模型的最终 Prompt。

本方法借鉴并重新编排了 MIT 许可的 s1dashu/director 中 Animated Explainer 的具体事件、调度与连续性原则，以适配本工作台的独立 Script Skill、TTS、八秒无声 I2V 和 Provider Adapter 边界。
