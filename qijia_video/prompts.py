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

# Pipeline v1 fallback. New jobs use SCRIPT_SKILL_OUTPUT_CONTRACT and a frozen
# Script Skill instead of any global prompt-writing profile.
_INPUT_FIRST_FALLBACK_SCRIPT_PROMPT = """依据冻结的原始输入和 EvidencePack，先建立一份唯一的 H3 CreativeBrief，再写成连贯的中文知识短视频脚本。

CreativeBrief 必须只确定一个中心问题、一个核心判断、一条完整论证路径和一个贯穿全片的视觉概念。不得套用预设领域、受众焦虑或平台话术。研究事实、合理解释与未确认内容必须分开；只有 verified_quote 可以作为人物逐字原话。

先写完整口播，再按真实语义变化拆段，不为凑数量切碎论证；通常 4-8 段，内容很集中时可以 3 段，复杂论证最多 12 段。开场直接进入内容自身的矛盾、选择或结果，后续每段推进新的证据或推理，结尾回答中心问题。脚本不设计逐镜头画面；视觉章节由后续 H3 Visual Director 根据完整旁白和 CreativeBrief 统一规划。"""


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


# Pipeline v1 persisted-job defaults. Public v2 requests cannot submit either
# field, and create_job clears both values before saving a new task.
DEFAULT_SCRIPT_PROMPT = _INPUT_FIRST_FALLBACK_SCRIPT_PROMPT.strip()
DEFAULT_SEEDANCE_PROMPT = _skill_prompt(
    "../visual_styles/content-skill-default",
    "director-prompt.md",
    _INPUT_FIRST_FALLBACK_VISUAL_PROMPT,
)


SCRIPT_OUTPUT_CONTRACT = """【系统输出格式】
输出严格 JSON，不要 Markdown。先输出 creative_brief，再输出脚本。creative_brief 包含 central_question、core_thesis、audience_promise、narrative_arc、tone、visual_concept、continuity_anchors、must_include、must_avoid、evidence_refs。

脚本 schema_version 固定为 "3.0"；字段为 video_title、cover_text、caption、hashtags、beats。beats 按完整口播中的自然语义变化拆分，不为凑数量切段；通常 4-8 段，最少 3 段、最多 12 段，依次使用 id n01、n02……；第一段 role 为 hook，最后一段为 closing，中间使用 suspense、context、reframe、explanation、example 或 application。每段只包含 id、role、narration、on_screen_text、source_refs、quote_ref。

narration 是唯一送入 TTS 的口播。脚本阶段不写 visual_direction，不逐段导演画面；后续 H3 Visual Director 会依据完整旁白和 creative_brief 统一规划。on_screen_text 是 Remotion 后期叠加的少量强调文字，不需要时填空字符串。source_refs 只为本段实际出现的事实主张或直接引语填写可用 fact/quote ID；纯过渡、提问、解释或编辑判断可以为空，绝不能填写 source ID 或自行创造 ID。直接引语必须同时填写 quote_ref；没有直接引语时填 null。hashtags 输出 3-5 个不带 # 的词。"""

SCRIPT_SKILL_OUTPUT_CONTRACT = '''【v2 输出契约】
只输出一个 JSON 对象，顶层包含 editorial_plan 与脚本字段，不要 Markdown。

editorial_plan 必须包含 objective、central_question、candidate_angles、selected_angle_id、selection_reason、core_thesis、audience_promise、narrative_arc、tone、must_include、must_avoid、evidence_refs、critic_summary。candidate_angles 必须有 2—3 项，每项只包含 angle_id、premise、audience_value、evidence_refs、risk；selected_angle_id 必须引用其中一项。EditorialPlan 禁止出现任何视觉概念、风格、镜头或模型提示词。

脚本 schema_version 固定为 3.0；字段为 video_title、cover_text、caption、hashtags、beats。beats 通常 4—8 段，最少 3 段、最多 12 段，依次使用 n01、n02……；第一段 role 为 hook，最后一段为 closing，中间使用 suspense、context、reframe、explanation、example 或 application。每段只包含 id、role、narration、on_screen_text、source_refs、quote_ref。

narration 是唯一送入 TTS 的口播，不写 visual_direction 或逐镜头画面。on_screen_text 仅用于 Remotion 后期排版，不需要时为空。source_refs 只能使用 EvidencePack 中存在的 fact/quote ID；直接引语必须填写 quote_ref，没有直接引语时为 null。hashtags 输出 3—5 个不带 # 的词。'''
