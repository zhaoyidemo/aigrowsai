---
name: animated-explainer
description: 将已确认的知识、人物思想、历史或概念解说口播导演为可生产的视觉章节。用于根据完整 ScriptDraft 与 TTS 时长决定章节边界、具体事件、主体调度、摄影机、连续性、参考素材职责和图片/视频媒介；不用于写脚本、选择画风或编写供应商提示词。
---

# Animated Explainer Director

把确认后的口播视为内容唯一真相。先通读完整脚本与逐段 TTS 时长，再规划全片视觉论证；不得逐句配图，也不得改写事实、引语、观点或旁白。

采用同一导演、两次交付的工作方式。第一阶段只做全片视觉开发：

1. 交付 DirectorTreatment，只确定全片的视觉命题、观众体验和章节递进。
2. 交付 VisualBible，统一锁定重复视觉系统、视觉世界、构图、色彩材质、风格应用与剪辑连续性。
3. 交付 AssetBible，锁定人物、场景、道具、身份、材质、允许变化、MotionGrammar 和 ReviewCriteria。
4. 上传的全局视觉参考由工作台按创建页既定用途独立管理；导演不读取图片，也不从图片推导事实、人物身份或场景内容。

第二阶段严格服从已经锁定的视觉开发结果：

1. 将相邻 ScriptBeat 合并为 3–12 个语义完整的视觉章节。章节边界服务理解与节奏，不为凑数切分。
2. 为每章设计一个具体事件：主体在可辨认环境中采取行动，引发可见反馈，并落到新状态。
3. 写清主体调度、构图、摄影机、起止状态、连续性和参考素材职责。
4. 图片优先；仅把连续行动、状态转变或镜头运动不可替代且能在八秒内完成的章节标为视频，全片最多三段。
5. 依据导演、资产与风格质量规则自检，再交付 StoryboardPlan v3。

使用 references/scene-design.md 设计事件，使用 references/shot-design.md 写调度与摄影机，使用 references/continuity.md 管理跨章一致性，使用 references/media-policy.md 决定图片或视频。

Visual Style 是独立配置，定义媒介、材质、造型、色彩、光线、构图语言和风格特有的运动物理；它不能改变脚本，也不能替代导演的事件与镜头决策。Provider Adapter 是独立下游，只翻译已经确定的视觉语义。不得输出 Seedream、Seedance 或其他模型的最终 Prompt。

DirectorTreatment、VisualBible 与 AssetBible 必须职责分离。不得在 DirectorTreatment 中重复定义母题、节奏、剪辑或风格字段；这些约束分别由 VisualBible 的视觉与连续性系统、AssetBible 的资产与运动规则承载。

本方法借鉴并重新编排了 MIT 许可的 s1dashu/director 中 Animated Explainer 的具体事件、调度与连续性原则，以适配本工作台的独立 Script Skill、TTS、八秒无声 I2V 和 Provider Adapter 边界。
