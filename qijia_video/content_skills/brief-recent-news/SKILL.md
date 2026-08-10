---
name: brief-recent-news
description: 检索并讲解企业、产品、科技或商业主题的最新公开新闻。用于“最新消息”“最近发生了什么”、新闻口播和需要严格冻结检索时间、区分事件时间与发布时间的短视频。
---

# 最新新闻口播

把主题输入视为检索请求，而不是新闻事实。联网研究必须成功并形成可追溯证据，之后才能生成脚本。

执行以下流程：

1. 冻结任务创建时间与检索截止时间。
2. 按 [research-prompt.md](references/research-prompt.md) 优先检索官方信源和独立信源。
3. 只把与检索注释匹配且可追溯的证据写入来源卡；没有可追溯证据时停止，时间缺失、只有单一站点或来源类型不完整时醒目标注并进入人工审核。
4. 使用 [script-prompt.md](references/script-prompt.md) 生成严格的 `ScriptDraft v2`。
5. 使用 [visual-prompt.md](references/visual-prompt.md) 生成不伪造界面、数字或 Logo 的通用新闻视觉。

明确区分已发生事实、官方计划、第三方判断和仍未确认的传闻。
