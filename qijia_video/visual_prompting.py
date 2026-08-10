"""Compile frozen, provider-neutral visual intent into provider prompt text."""
from __future__ import annotations

from qijia_video.contracts import (
    DEFAULT_VISUAL_STYLE_ID,
    H3_PROMPT_WRITING_PROFILE_ID,
    PromptWritingProfileSnapshot,
    StoryboardShot,
    VisualStyleSnapshot,
)


def _join_blocks(*blocks: str) -> str:
    return "\n".join(block.strip() for block in blocks if block and block.strip())


def _rules(label: str, rules: list[str]) -> str:
    values = [item.strip() for item in rules if item and item.strip()]
    return f"【{label}】" + "；".join(values) if values else ""


def _uses_distinct_style(snapshot: VisualStyleSnapshot | None) -> bool:
    return bool(snapshot and snapshot.style_id != DEFAULT_VISUAL_STYLE_ID)


def _uses_h3(profile: PromptWritingProfileSnapshot | None) -> bool:
    return bool(
        profile
        and profile.profile_id == H3_PROMPT_WRITING_PROFILE_ID
    )


def compile_storyboard_base_style(
    director_prompt: str,
    style: VisualStyleSnapshot | None,
    profile: PromptWritingProfileSnapshot | None,
    *,
    has_reference_image: bool,
    content_policy: str = "",
) -> str:
    if _uses_h3(profile):
        reference_scope = (
            f"【参考素材】{profile.reference_policy}"
            if has_reference_image and profile
            else "【参考素材】本任务没有全局参考图，由所选视觉表现完整定义视觉属性。"
        )
        style_specification = _join_blocks(
            style.director_prompt if style else director_prompt,
            style.storyboard_rules if style else "",
            style.image_rules if style else "",
            style.motion_rules if style else "",
        )
        negatives = _rules(
            "边界",
            (list(profile.negative_rules) if profile else [])
            + (list(style.negative_rules) if style else []),
        )
        return _join_blocks(
            (
                "【唯一编排层】使用 H3 Prompt Writing 方法完成全部视觉提示词编排。"
                "Content Skill 只提供内容语义与真实性边界，视觉表现只提供艺术语言；"
                "不要把方法说明、字段标签或多套导演规则复制进最终媒体提示词。"
            ),
            (
                "【冲突优先级】事实、安全与领域边界 > 本章可见语义 > "
                "参考素材已经确定的视觉属性 > 所选视觉表现对未定义属性的补全 > "
                "供应商语法。参考素材不是事实来源，也不能覆盖内容安全边界。"
            ),
            f"【H3 编排方法】{profile.planning_framework}",
            f"【首帧输出标准】{profile.image_framework}",
            f"【I2V 输出标准】{profile.video_framework}",
            (
                f"【内容视觉策略】{content_policy}"
                if content_policy.strip()
                else ""
            ),
            f"【视觉表现】{style_specification}",
            reference_scope,
            f"【声音边界】{profile.audio_policy}",
            negatives,
        )

    if has_reference_image:
        base = (
            "本任务提供全局参考图。参考图是画风、色彩、光影、材质和人物视觉特征的"
            "最高优先级。分镜只设计场景、人物动作、空间关系、构图和运镜，不另行规定"
            "艺术媒介、固定配色或与参考图冲突的造型。"
        )
        style_rules = ""
    else:
        base = director_prompt
        style_rules = (
            f"【视觉风格执行】{style.storyboard_rules}"
            if _uses_distinct_style(style) and style and style.storyboard_rules
            else ""
        )
    framework = (
        f"【结构化多模态规划】{profile.planning_framework}"
        if profile
        else ""
    )
    reference_policy = (
        f"【参考素材规则】{profile.reference_policy}"
        if profile and has_reference_image
        else ""
    )
    negatives = _rules(
        "统一排除",
        (list(profile.negative_rules) if profile else [])
        + (
            list(style.negative_rules)
            if _uses_distinct_style(style) and style and not has_reference_image
            else []
        ),
    )
    return _join_blocks(base, framework, reference_policy, style_rules, negatives)


def compile_first_frame_prompt(
    director_prompt: str,
    style: VisualStyleSnapshot | None,
    profile: PromptWritingProfileSnapshot | None,
    shot: StoryboardShot,
    *,
    has_reference_image: bool,
) -> str:
    if _uses_h3(profile):
        reference_anchor = (
            "延续输入参考图已经确定的主体身份、造型、材质、色彩与光影。"
            if has_reference_image
            else ""
        )
        return _join_blocks(
            shot.first_frame_prompt.strip()[:1800],
            reference_anchor,
            (
                "竖屏 9:16；只生成画面，不生成任何可读文字、字幕、数字、"
                "Logo、水印或界面；底部保留干净字幕安全区。"
            ),
        )

    frame_prompt = shot.first_frame_prompt.strip()[:750]
    style_direction = (
        "【视觉基准】已提供的全局参考图是画风、色彩、光影、材质、人物造型与视觉"
        "气质的最高优先级。严格延续参考图，只根据本镜头内容重新组织场景、动作和"
        "构图；不要采用其他文字设定中的艺术风格，也不要照搬参考图里的文字、Logo"
        "或水印。"
        if has_reference_image
        else f"【全局视觉导演设定】{director_prompt.strip()[:750]}"
    )
    framework = (
        f"【首帧提示词结构】{profile.image_framework}"
        if profile
        else ""
    )
    style_rules = (
        f"【风格化首帧】{style.image_rules}"
        if _uses_distinct_style(style)
        and style
        and style.image_rules
        and not has_reference_image
        else ""
    )
    negatives = _rules(
        "额外排除",
        (list(profile.negative_rules) if profile else [])
        + (
            list(style.negative_rules)
            if _uses_distinct_style(style) and style and not has_reference_image
            else []
        ),
    )
    return _join_blocks(
        style_direction,
        framework,
        style_rules,
        f"【静止首帧】{frame_prompt}",
        "主体关系清楚，构图简洁，优先保证核心变化或信息关系一眼可懂。",
        (
            "只生成一张竖屏 9:16 画面首帧。画面中不得出现任何文字、字幕、"
            "字母、数字、Logo、水印、可读书页、屏幕界面或品牌标识；"
            "底部保留干净的字幕安全区。"
        ),
        negatives,
    )


def compile_video_prompt(
    director_prompt: str,
    style: VisualStyleSnapshot | None,
    profile: PromptWritingProfileSnapshot | None,
    shot: StoryboardShot,
    *,
    has_reference_image: bool,
    opening_direction: str = "",
) -> str:
    if _uses_h3(profile):
        return _join_blocks(
            (
                "输入首帧就是视频第一帧和唯一视觉基准；保持其中的主体身份、"
                "造型、材质、空间、构图、色彩与光影不变。"
            ),
            shot.motion_prompt.strip()[:1800],
            (
                "只生成无声画面；不新增任何可读文字、字幕、数字、Logo、"
                "水印或界面。"
            ),
        )

    style_context = (
        "【视觉基准】严格以已提供的首帧为唯一画风、色彩、光影、材质和人物造型"
        "基准；任何文字描述与首帧冲突时，以首帧为准。"
        if has_reference_image
        else director_prompt.strip()[:1200]
    )
    framework = (
        f"【视频提示词结构】{profile.video_framework}"
        if profile
        else ""
    )
    style_rules = (
        f"【风格动作语法】{style.motion_rules}"
        if _uses_distinct_style(style)
        and style
        and style.motion_rules
        and not has_reference_image
        else ""
    )
    audio_policy = (
        f"【声音边界】{profile.audio_policy}"
        if profile
        else ""
    )
    negatives = _rules(
        "额外排除",
        (list(profile.negative_rules) if profile else [])
        + (
            list(style.negative_rules)
            if _uses_distinct_style(style) and style and not has_reference_image
            else []
        ),
    )
    return _join_blocks(
        style_context,
        opening_direction,
        framework,
        style_rules,
        f"【本镜头视觉意图】{shot.visual_intent[:450]}",
        (
            "【首帧驱动】严格从已提供的首帧自然延展，保持人物、服装、"
            "空间、构图、配色和画风稳定。"
        ),
        f"【动作与运镜】{shot.motion_prompt[:1000]}",
        (
            "本镜头只安排一个清楚可信的动作和一种克制运镜，结尾保持自然。"
            "只生成自然动画画面，不生成旁白或模型音频。不得新增任何文字、"
            "字幕、字母、数字、Logo、水印、可读书页、屏幕界面或品牌标识。"
        ),
        audio_policy,
        negatives,
    )
