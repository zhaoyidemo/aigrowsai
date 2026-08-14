'''Compile ShotContextIR at the last mile for the configured media providers.'''
from __future__ import annotations

import json

from qijia_video.contracts import (
    AssetBible,
    DirectorTreatment,
    ProviderAdapterSnapshot,
    StoryboardShot,
    VisualBible,
)


SEEDREAM_IMAGE_PROMPT_BUDGET = 4000


def _join(*blocks: str) -> str:
    return '\n'.join(str(item or '').strip() for item in blocks if str(item or '').strip())


def _compact_text(value: str, max_chars: int) -> str:
    """Keep provider prompts concise without dropping whole semantic sections."""

    text = ' '.join(str(value or '').split())
    if len(text) <= max_chars:
        return text
    if max_chars <= 1:
        return text[:max_chars]
    target = max_chars - 1
    floor = max(1, int(target * 0.6))
    cut = max(
        (text.rfind(mark, floor, target) for mark in ('。', '；', '，', '.', ';', ',')),
        default=-1,
    )
    if cut < floor:
        cut = target
        return text[:cut].rstrip() + '…'
    return text[:cut + 1].rstrip() + '…'


def _section(label: str, *values: str, max_chars: int) -> str:
    content = '；'.join(
        ' '.join(str(item or '').split())
        for item in values
        if str(item or '').strip()
    )
    return _compact_text(label + content, max_chars) if content else ''


def _bounded_image_prompt(*blocks: str) -> str:
    prompt = _join(*blocks)
    if len(prompt) > SEEDREAM_IMAGE_PROMPT_BUDGET:
        raise ValueError(
            'Seedream 图片提示词编译超出内部预算：'
            f'{len(prompt)}>{SEEDREAM_IMAGE_PROMPT_BUDGET}'
        )
    return prompt


def _shot_payload(shot: StoryboardShot) -> str:
    if shot.context is None:
        raise ValueError('v2 Provider Adapter 只能编译带 ShotContextIR 的分镜')
    return json.dumps(shot.context.model_dump(mode='json'), ensure_ascii=False)


def _bible_payload(bible: VisualBible) -> str:
    return json.dumps(
        {
            'core_visual_idea': bible.core_visual_idea,
            'visual_world': bible.visual_world,
            'recurring_subjects': bible.recurring_subjects,
            'scene_anchors': bible.scene_anchors,
            'continuity_rules': bible.continuity_rules,
            'color_material_system': bible.color_material_system,
            'composition_system': bible.composition_system,
            'reference_strategy': bible.reference_strategy,
            'forbidden_elements': bible.forbidden_elements,
        },
        ensure_ascii=False,
    )


def _legacy_adapter(adapter: ProviderAdapterSnapshot) -> bool:
    try:
        major = int(str(adapter.version).split('.', 1)[0])
    except (TypeError, ValueError):
        return True
    return major < 2


def _asset_anchors(asset_bible: AssetBible | None) -> str:
    if not asset_bible:
        return ''
    parts = [
        '主体锁定：' + '；'.join(asset_bible.identity_locks[:4]),
        '材质锁定：' + '；'.join(asset_bible.material_locks[:4]),
    ]
    if asset_bible.props:
        parts.append('贯穿道具：' + '；'.join(asset_bible.props[:4]))
    return '。'.join(item for item in parts if item)


def _reference_contract(
    asset_bible: AssetBible | None,
    available_reference_ids: set[str] | None = None,
    reference_order: list[str] | None = None,
) -> str:
    if not asset_bible or not asset_bible.references:
        return ''
    position_by_id = {
        reference_id: index
        for index, reference_id in enumerate(reference_order or [], 1)
        if reference_id
    }
    rows = []
    references = sorted(
        asset_bible.references,
        key=lambda item: position_by_id.get(item.reference_id, 10_000),
    )
    for item in references:
        if (
            available_reference_ids is not None
            and item.reference_id not in available_reference_ids
        ):
            continue
        position = position_by_id.get(item.reference_id)
        reference_label = (
            f'参考图 {position}（{item.reference_id}）'
            if position is not None
            else item.reference_id
        )
        rows.append(
            f'{reference_label}只控制 {"、".join(item.roles)}；'
            f'保留 {"、".join(item.preserve) or "已声明属性"}；'
            f'允许改变 {"、".join(item.allow_change) or "未声明属性"}；'
            f'不得迁移 {"、".join(item.forbidden_transfer) or "无关属性"}'
        )
    return ('参考关系：' + '。'.join(rows)) if rows else ''


def compile_style_frame_prompt(
    treatment: DirectorTreatment,
    bible: VisualBible,
    asset_bible: AssetBible,
    *,
    variant: int,
    has_reference_image: bool = False,
) -> str:
    """Create one representative frame for visual approval, not a shot."""

    emphasis = {
        1: '方案 A：强调主体层级、负空间和第一眼阅读顺序。',
        2: '方案 B：强调材质触感、色彩关系和光线塑形。',
        3: '方案 C：强调空间纵深、动作前状态和环境反馈。',
    }.get(int(variant), '重点检验完整视觉系统。')
    representative_scene = treatment.chapter_progression[
        min(len(treatment.chapter_progression) - 1, len(treatment.chapter_progression) // 2)
    ]
    reference = (
        _reference_contract(
            asset_bible,
            {'global_reference'},
            ['global_reference'],
        )
        if has_reference_image
        else ''
    )
    if has_reference_image and not reference:
        reference = (
            '参考图 1（global_reference）只承担 Director 已声明的身份、场景、'
            '物件、构图或风格职责；不得自动继承其他属性。'
        )
    return _bounded_image_prompt(
        '视觉开发样片，竖屏 9:16，单幅完整画面，不是拼版、设定表或多格分镜。',
        _section('视觉命题：', treatment.visual_thesis, max_chars=360),
        _section(
            '三张样片共同使用的代表性事件：',
            representative_scene,
            max_chars=420,
        ),
        '不得改变这一事件的主体、场景、动作、道具和结果，只比较视觉处理。',
        emphasis,
        _section('核心主体：', *asset_bible.subjects[:4], max_chars=260),
        _section('关键场景：', *asset_bible.locations[:3], max_chars=220),
        _section('关键道具：', *asset_bible.props[:3], max_chars=180),
        _section(
            '视觉世界：',
            bible.visual_world,
            '色彩与材质：' + bible.color_material_system,
            '构图系统：' + bible.composition_system,
            max_chars=500,
        ),
        _compact_text(_asset_anchors(asset_bible), 280),
        _section('运动前状态：', *asset_bible.motion_grammar[:3], max_chars=240),
        _section('验收重点：', *asset_bible.review_criteria[:5], max_chars=300),
        _section('必须排除：', *bible.forbidden_elements[:5], max_chars=280),
        (
            _compact_text(reference, 450)
            if has_reference_image
            else '本样片没有输入参考图，不得虚构参考素材约束。'
        ),
        '画面必须像最终成片中的一个成熟镜头，主体行动一眼可读，底部保留字幕安全区。'
        '不要可读文字、Logo、水印、界面、色板、标注线或无关装饰。',
    )


def compile_image_provider_prompt(
    adapter: ProviderAdapterSnapshot,
    bible: VisualBible,
    shot: StoryboardShot,
    *,
    has_reference_image: bool,
    asset_bible: AssetBible | None = None,
    available_reference_ids: set[str] | None = None,
    reference_order: list[str] | None = None,
) -> str:
    if not _legacy_adapter(adapter):
        if shot.context is None:
            raise ValueError('v4 图片编译必须包含 ShotContextIR')
        context = shot.context
        reference = (
            _reference_contract(
                asset_bible,
                available_reference_ids,
                reference_order,
            )
            if has_reference_image
            else ''
        )
        if has_reference_image and not reference:
            reference = (
                '输入参考图只承担本章节 reference_roles 已声明的职责；'
                '不得自动继承未声明的人物、场景、物件、构图或风格属性。'
            )
        # adapter.image_framework and adapter.reference_policy are compiler
        # instructions, not visible scene content. Their H3 method is executed
        # by this structure instead of being copied into the provider prompt.
        return _bounded_image_prompt(
            '竖屏 9:16 单幅画面。',
            _section(
                '决定性事件：',
                context.concrete_event or context.semantic_goal,
                max_chars=420,
            ),
            _section(
                '主体与空间：',
                context.subject,
                context.blocking,
                max_chars=360,
            ),
            _section(
                '可见动作和结果：',
                context.action,
                '画面停在 ' + context.end_state,
                max_chars=360,
            ),
            _section('环境：', context.environment, max_chars=240),
            _section(
                '构图与摄影机：',
                context.composition,
                context.camera_intent,
                max_chars=360,
            ),
            _section(
                '全片视觉语言：',
                bible.visual_world,
                bible.color_material_system,
                bible.composition_system,
                max_chars=520,
            ),
            _compact_text(_asset_anchors(asset_bible), 280),
            _compact_text(reference, 520),
            _section(
                '保持连续：',
                context.continuity_handoff,
                *bible.continuity_rules[:3],
                max_chars=300,
            ),
            '底部保留干净字幕安全区。画面中不要出现可读文字、Logo、水印或多格分镜。',
            _section('硬边界：', *adapter.negative_rules, max_chars=320),
        )
    reference = (
        '【参考素材保留协议】' + adapter.reference_policy
        if has_reference_image
        else '【参考素材】无；不得虚构参考图约束。'
    )
    return _join(
        f'【Provider Adapter】{adapter.adapter_id}@{adapter.version}；只做语法编译。',
        '【Seedream 编译结构】' + adapter.image_framework,
        '【VisualBible】' + _bible_payload(bible),
        '【ShotContextIR】' + _shot_payload(shot),
        reference,
        '竖屏 9:16，只生成一张完整画面；底部保留干净字幕安全区。',
        '【硬边界】' + '；'.join(adapter.negative_rules),
    )


def compile_video_provider_prompt(
    adapter: ProviderAdapterSnapshot,
    bible: VisualBible,
    shot: StoryboardShot,
    *,
    opening_direction: str = '',
    revision_intent: str = '',
    asset_bible: AssetBible | None = None,
    duration_seconds: int = 8,
) -> str:
    if not _legacy_adapter(adapter):
        if shot.context is None:
            raise ValueError('v4 视频编译必须包含 ShotContextIR')
        context = shot.context
        revision = (
            '本次只调整：' + revision_intent.strip()[:600]
            if revision_intent.strip()
            else ''
        )
        motion_grammar = (
            '；'.join(asset_bible.motion_grammar[:4])
            if asset_bible
            else ''
        )
        return _join(
            '输入模式：首帧驱动的无声 I2V。首帧是最高且唯一视觉基准；其中的身份、'
            '造型、材质、空间、构图、色彩和光线全部保持，不重新设计画面。',
            f'起始状态：{context.start_state}；{context.blocking}',
            f'主要动作链：{context.action}',
            f'环境反馈与结束状态：{context.end_state}',
            f'摄影机：{context.camera_intent}',
            f'连续性：{context.continuity_handoff}',
            ('风格运动语法：' + motion_grammar if motion_grammar else ''),
            opening_direction,
            revision,
            f'在 {max(4, min(15, int(duration_seconds)))} 秒内只完成这一条动作链和一种克制运镜。不要新增人物、道具、'
            '文字、镜头切换或风格；不要生成对白、音乐、音效和旁白。',
            '硬边界：' + '；'.join(adapter.negative_rules),
        )
    revision = (
        '【本次编辑意图】在不改变主体身份、事实关系、首帧和起止状态的前提下：'
        + revision_intent.strip()[:600]
        if revision_intent.strip()
        else ''
    )
    return _join(
        f'【Provider Adapter】{adapter.adapter_id}@{adapter.version}；只做 I2V 语法编译。',
        '【Seedance 编译结构】' + adapter.video_framework,
        '【VisualBible】' + _bible_payload(bible),
        '【ShotContextIR】' + _shot_payload(shot),
        opening_direction,
        revision,
        '【声音边界】' + adapter.audio_policy,
        '【硬边界】' + '；'.join(adapter.negative_rules),
    )
