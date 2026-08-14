'''Compile provider-neutral runtime instructions from frozen directing methods.'''
from __future__ import annotations

from qijia_video.contracts import DirectorSkillSnapshot, VisualStyleSnapshot


DIRECTOR_RUNTIME_PROMPT_VERSION = 'qijia_director_runtime_v2_provider_neutral'

_FORBIDDEN_RUNTIME_TERMS = (
    'seedream',
    'seedance',
    'provider adapter',
    'director skill',
    'script skill',
    'h3',
    'github',
    's1dashu',
    '供应商',
    '下游',
    'prompt',
)
_DROP_RUNTIME_LINES = (
    'provider adapter',
    'seedream',
    'seedance',
    'github',
    's1dashu',
    'references/',
    'mit 许可',
)
_RUNTIME_REPLACEMENTS = (
    ('DirectorTreatment、VisualBible 与 AssetBible', '全片视觉方案、视觉规则与资产规则'),
    ('DirectorTreatment、VisualBible 和 AssetBible', '全片视觉方案、视觉规则和资产规则'),
    ('DirectorTreatment', '全片视觉方案'),
    ('StoryboardPlan v3', '正式分镜'),
    ('ShotContextIR.reference_roles', 'reference_roles'),
    ('ShotContextIR', '章节视觉描述'),
    ('VisualBible', '全片视觉规则'),
    ('AssetBible', '资产规则'),
    ('ScriptDraft', '已确认脚本'),
    ('ScriptBeat', '脚本段落'),
    ('Visual Style', '视觉风格'),
    ('ReviewCriteria', '验收标准'),
    ('MotionGrammar', '运动规则'),
    ('TTS', '旁白'),
    ('Animated Explainer Director', '动画解说导演'),
    ('负向 Prompt 或供应商名称', '与视觉创作无关的技术配置'),
)


def _join(*blocks: str) -> str:
    return '\n\n'.join(
        str(item or '').strip() for item in blocks if str(item or '').strip()
    )


def _runtime_text(value: str) -> str:
    '''Remove documentation/provenance language from a frozen runtime resource.'''

    lines: list[str] = []
    for raw_line in str(value or '').splitlines():
        line = raw_line.strip()
        folded = line.casefold()
        if any(marker in folded for marker in _DROP_RUNTIME_LINES):
            continue
        if line.startswith('#'):
            line = line.lstrip('#').strip()
        lines.append(line)
    result = '\n'.join(lines).strip()
    for old, new in _RUNTIME_REPLACEMENTS:
        result = result.replace(old, new)
    return result


def assert_provider_neutral_runtime_prompt(value: str) -> None:
    '''Reject internal architecture/provenance language at the model boundary.'''

    folded = str(value or '').casefold()
    offenders = [term for term in _FORBIDDEN_RUNTIME_TERMS if term in folded]
    if offenders:
        raise ValueError(
            '导演运行时指令包含内部架构或平台术语：' + '、'.join(offenders)
        )


def compile_director_instruction(
    director: DirectorSkillSnapshot,
    *,
    visual_style: VisualStyleSnapshot | None = None,
    has_reference_image: bool,
) -> str:
    '''Return the sole visual-planning instruction consumed by the director model.'''

    reference_scope = (
        '存在一张全局参考图。请在每章的 reference_roles 中只声明它实际承担的身份、'
        '服装、物件、地点、风格或构图职责；这些职责不得自动混用，参考图也不得覆盖'
        '已确认脚本中的事实。'
        if has_reference_image
        else '本次导演工作不接收外部参考图，请依据已确认脚本和视觉语言建立完整、连续的视觉世界。'
    )
    if director.mode == 'legacy-style-director':
        compiled = _join(
            '【工作目标】把已确认脚本与真实旁白时长转化为连续、可生产的视觉章节。'
            '脚本中的事实、引语、论点和旁白不可改写。',
            '【导演方法】\n' + _runtime_text(director.directing_instructions),
            '【章节设计】\n' + _runtime_text(director.storyboard_rules),
            '【静态画面】\n' + _runtime_text(director.image_art_direction),
            '【动态画面】\n' + _runtime_text(director.motion_art_direction),
            '【参考素材】' + reference_scope,
            '【排除项】' + _runtime_text('；'.join(director.negative_rules)),
        )
        assert_provider_neutral_runtime_prompt(compiled)
        return compiled
    if visual_style is None:
        raise ValueError('新版导演方法必须搭配独立视觉风格快照')

    workflow = _runtime_text(director.workflow_instructions)
    if not workflow:
        workflow = (
            '先通读完整口播与逐段旁白时长，确定全片视觉命题、观众体验、章节递进、'
            '重复母题和剪辑节奏。先锁定视觉世界与可复用资产，再把相邻脚本段落合并为'
            '语义完整的视觉章节；每章必须形成一个具体事件和可见结果。'
        )
    delivery_boundary = (
        '第一阶段先锁定视觉命题、视觉世界、资产、运动规则和验收标准；第二阶段再把'
        '每个语义目标落实为具体事件、主体调度、环境、构图、起止状态、连续性承接、'
        '可执行摄影机方案和媒介理由。visual_metaphor 可以为空，绝不能替代具体事件。'
    )
    compiled = _join(
        '【工作目标】你负责把已确认脚本与真实旁白时长转化为完整的视觉叙事。'
        '保持脚本中的事实、引语、论点和旁白不变，只做视觉设计。',
        '【工作方式】\n' + workflow,
        '【具体事件】\n' + _runtime_text(director.scene_design_rules),
        '【调度与摄影机】\n' + _runtime_text(director.shot_design_rules),
        '【连续性】\n' + _runtime_text(director.continuity_rules),
        '【媒介选择】\n' + _runtime_text(director.media_rules),
        '【视觉语言｜' + visual_style.display_name + '】\n'
        + _runtime_text(visual_style.director_prompt),
        '【构图规则】\n' + _runtime_text(visual_style.storyboard_rules),
        '【静态画面规则】\n' + _runtime_text(visual_style.image_rules),
        '【动态画面规则】\n' + _runtime_text(visual_style.motion_rules),
        '【参考素材】' + reference_scope,
        '【交付标准】' + delivery_boundary,
        '【验收标准】\n- '
        + '\n- '.join(_runtime_text(item) for item in director.critic_rules),
        '【排除项】' + _runtime_text('；'.join(visual_style.negative_rules)),
    )
    assert_provider_neutral_runtime_prompt(compiled)
    return compiled
