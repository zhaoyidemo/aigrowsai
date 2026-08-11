'''Compile ShotContextIR at the last mile for the configured media providers.'''
from __future__ import annotations

import json

from qijia_video.contracts import (
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


def compile_image_provider_prompt(
    adapter: ProviderAdapterSnapshot,
    bible: VisualBible,
    shot: StoryboardShot,
    *,
    has_reference_image: bool,
) -> str:
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
) -> str:
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
