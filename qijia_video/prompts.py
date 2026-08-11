"""共享提示词契约与旧版默认值的兼容入口。"""

import re
from pathlib import Path


SCRIPT_TARGET_MIN_CHARS = 220
SCRIPT_TARGET_MAX_CHARS = 300
# This is only a technical safety ceiling. Whether a script fits the video is
# decided by the real TTS duration before any paid visual generation starts.
SCRIPT_HARD_MAX_CHARS = 600


def narration_char_count(text: str) -> int:
    """Count speakable characters while ignoring formatting whitespace."""

    return len(re.sub(r"\s+", "", str(text or "")))

MIN_IMAGE_CHAPTER_COUNT = 2
MAX_IMAGE_CHAPTER_COUNT = 10
DEFAULT_IMAGE_CHAPTER_COUNT = 10

_INPUT_FIRST_FALLBACK_SCRIPT_PROMPT = """请严格基于用户原始输入与本次已核验研究材料，写成一版约 45-60 秒的中文知识短视频完整脚本。

一条视频只讲清一个核心问题。先忠实识别用户实际提交的人物、观点、事件或主题，再依据来源卡区分事实、解释、争议与现实意义；不得把内容自动改写成家庭教育、心理学或任何预设领域。只有来源卡中的 verified_quote 才能作为逐字引语，其余内容必须转述并保留不确定性。

开场直接呈现主题中真实存在的冲突或反常识判断，前 2 秒建立相关性，前 5 秒说明继续观看的理由；不使用虚假悬念、恐慌、夸大结论或命令式 CTA。先在内部形成连贯全文，再按语义拆成 5-8 个叙事段，每段推进新的信息、因果或选择，结尾回答开场问题。面向来源卡给定的目标受众自然表达，不擅自替换受众。

旁白负责解释，visual_direction 只描述该段必须被看懂的可见语义，不规定画风、材质、构图、运镜或连续性；这些由 H3 提示词编排层统一完成。所有事实和直接引语都必须引用来源卡中明确可用的 fact/quote ID，不得补造经历、著作、数据、出处或原话。"""


_INPUT_FIRST_FALLBACK_VISUAL_PROMPT = (
    "竖屏 9:16，成熟的现代编辑插画视觉。使用简洁手绘轮廓、克制的几何色块、"
    "细腻纸张颗粒和轻微 2.5D 空间感；米白基底配低饱和海军蓝、陶土橙与少量辅助色。"
    "主体、人物关系、空间和关键物件必须由本任务原始输入与分镜语义决定，不预设家庭、"
    "儿童、职场或历史场景。跨章节保持已建立的角色和视觉锚点一致，但不得伪造名人肖像、"
    "史料、原典页面或现实品牌。不要逐字图解口播；用可见动作、关系、对照与结果推进理解。"
    "视频镜头只安排一个清楚可信的动作和一种克制运镜，图片镜头保持有呼吸感的静态构图，"
    "底部留出字幕安全区。不出现文字、字幕、Logo 或水印，避免夸张表演、突然变形和肢体错误。"
)


_CONTENT_SKILL_ROOT = Path(__file__).resolve().parent / "content_skills"


def _skill_prompt(skill_id: str, filename: str, fallback: str) -> str:
    """Load the default prompt from its versioned Skill package."""

    path = _CONTENT_SKILL_ROOT / skill_id / "references" / filename
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        # Keep partially upgraded/source-only deployments readable. Packaged
        # releases always include the Skill references.
        return fallback.strip()
    return value or fallback.strip()


DEFAULT_SCRIPT_PROMPT = _skill_prompt(
    "explain-expert-view",
    "script-prompt.md",
    _INPUT_FIRST_FALLBACK_SCRIPT_PROMPT,
)
DEFAULT_SEEDANCE_PROMPT = _skill_prompt(
    "../visual_styles/content-skill-default",
    "director-prompt.md",
    _INPUT_FIRST_FALLBACK_VISUAL_PROMPT,
)


SCRIPT_OUTPUT_CONTRACT = """【系统输出格式】
输出严格 JSON，不要 Markdown。字段为 schema_version、video_title、cover_text、hook、closing、caption、hashtags、beats。schema_version 固定为 "2.0"。beats 按语义自然划分为 5-8 段，依次使用 id n01、n02……；第一段 role 为 hook，最后一段为 closing，中间按内容使用 suspense、context、reframe、explanation、example 或 application。每段包含 id、role、narration、visual_direction、on_screen_text、source_refs、quote_ref。

narration 是唯一会送入 TTS 的口播，所有 narration 合计字数遵循系统在本任务末尾追加的语速目标；visual_direction 只写本段必须被观众看懂的可见语义，包括主体、关系、变化或结果，不规定画风、色彩、材质、构图、景别、运镜、连续性方案，也不包含字幕、文字、Logo 或排版指令；on_screen_text 是由 Remotion 后期叠加的少量强调文字，不会送入 TTS，也不会送给画面模型，不需要时填空字符串。每段 source_refs 至少填写一个明确列出的可用 fact/quote ID，绝不能填写 source ID 或自行创造 ID；没有直接引文时 quote_ref 填 null。hook 与第一段 narration 相同，closing 与最后一段 narration 相同；hashtags 输出 3-5 个不带 # 的词。"""
