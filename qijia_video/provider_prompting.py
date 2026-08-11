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


def _join(*blocks: str) -> str:
    return '\n'.join(str(item or '').strip() for item in blocks if str(item or '').strip())


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
) -> str:
    if not asset_bible or not asset_bible.references:
        return ''
    rows = []
    for item in asset_bible.references:
        if (
            available_reference_ids is not None
            and item.reference_id not in available_reference_ids
        ):
            continue
        rows.append(
            f'{item.reference_id} 只控制 {"、".join(item.roles)}；'
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
) -> str:
    """Create one representative frame for visual approval, not a shot."""

    emphasis = {
        1: '重点检验核心主体、贯穿道具和身份连续性。',
        2: '重点检验主场景、前中后景层次、色彩与材质系统。',
        3: '重点检验一个具体行动、环境反馈和风格特有的运动前状态。',
    }.get(int(variant), '重点检验完整视觉系统。')
    return _join(
        '视觉开发样片，竖屏 9:16，单幅完整画面，不是拼版、设定表或多格分镜。',
        f'视觉命题：{treatment.visual_thesis}',
        f'风格落实：{treatment.style_application}',
        f'代表性场景：{treatment.chapter_progression[min(max(variant - 1, 0), len(treatment.chapter_progression) - 1)]}',
        emphasis,
        '核心主体：' + '；'.join(asset_bible.subjects[:4]),
        ('关键场景：' + '；'.join(asset_bible.locations[:3])),
        ('关键道具：' + '；'.join(asset_bible.props[:3]) if asset_bible.props else ''),
        (
            f'视觉世界：{bible.visual_world}；{bible.color_material_system}；'
            f'{bible.composition_system}'
        ),
        _asset_anchors(asset_bible),
        '运动前状态：' + '；'.join(asset_bible.motion_grammar[:3]),
        '验收重点：' + '；'.join(asset_bible.review_criteria[:5]),
        (
            '必须排除：' + '；'.join(bible.forbidden_elements[:5])
            if bible.forbidden_elements else ''
        ),
        '用户原始参考图不会在视觉开发样片阶段再次发送给图片模型。',
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
) -> str:
    if not _legacy_adapter(adapter):
        if shot.context is None:
            raise ValueError('v4 图片编译必须包含 ShotContextIR')
        context = shot.context
        reference = (
            _reference_contract(asset_bible, available_reference_ids)
            if has_reference_image
            else ''
        )
        if has_reference_image and not reference:
            reference = (
                '输入参考图只承担本镜头 ShotContextIR.reference_roles 已声明的职责；'
                '不得自动继承未声明的人物、场景、物件、构图或风格属性。'
            )
        return _join(
            '竖屏 9:16 单幅画面。',
            f'决定性事件：{context.concrete_event or context.semantic_goal}',
            f'主体与空间：{context.subject}；{context.blocking}',
            f'可见动作和结果：{context.action}；画面停在 {context.end_state}',
            f'环境：{context.environment}',
            f'构图与摄影机：{context.composition}；{context.camera_intent}',
            (
                '全片视觉语言：'
                f'{bible.visual_world}；{bible.color_material_system}；'
                f'{bible.composition_system}'
            ),
            _asset_anchors(asset_bible),
            reference,
            (
                '保持连续：'
                + '；'.join(
                    [context.continuity_handoff, *bible.continuity_rules[:3]]
                )
            ),
            '底部保留干净字幕安全区。画面中不要出现可读文字、Logo、水印或多格分镜。',
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
            '八秒内只完成这一条动作链和一种克制运镜。不要新增人物、道具、'
            '文字、镜头切换或风格；不要生成对白、音乐、音效和旁白。',
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
