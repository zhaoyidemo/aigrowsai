'''Compile one Director Skill instruction without provider prompt syntax.'''
from __future__ import annotations

from qijia_video.contracts import DirectorSkillSnapshot, VisualStyleSnapshot


def _join(*blocks: str) -> str:
    return '\n\n'.join(str(item or '').strip() for item in blocks if str(item or '').strip())


def compile_director_instruction(
    director: DirectorSkillSnapshot,
    *,
    visual_style: VisualStyleSnapshot | None = None,
    has_reference_image: bool,
) -> str:
    '''Return the sole visual-planning instruction consumed by the director model.'''

    reference_scope = (
        '存在一张全局参考图。只在每个 ShotContextIR.reference_roles 中声明它的用途；'
        '身份、场景与风格权力不得自动混用，参考图也不得覆盖脚本事实。'
        if has_reference_image
        else '没有全局参考图，由本 Director Skill 完整定义视觉世界与连续性。'
    )
    if director.mode == 'legacy-style-director':
        return _join(
            '【历史唯一视觉负责人】这是升级前由视觉风格兼任导演的冻结任务。'
            '已确认脚本是内容唯一真相；不得改写事实、引语、论点或旁白。',
            '【历史导演方法】\n' + director.directing_instructions,
            '【历史分镜规则】\n' + director.storyboard_rules,
            '【历史静态艺术方向】\n' + director.image_art_direction,
            '【历史动态艺术方向】\n' + director.motion_art_direction,
            '【参考素材边界】' + reference_scope,
            '【职责边界】只交付 VisualBible 与 ShotContextIR，不得输出媒体模型'
            '提示词、参数或第二套导演方法。',
            '【统一排除】' + '；'.join(director.negative_rules),
        )
    if visual_style is None:
        raise ValueError('新 Director Skill 必须搭配独立 Visual Style 快照')
    return _join(
        '【唯一视觉负责人】你是本任务选中的 Director Skill。已确认脚本是内容唯一真相；'
        '不得改写事实、引语、论点或旁白，也不得生成任何供应商提示词。你的唯一职责是把'
        '完整脚本与真实 TTS 时长转化为 VisualBible 与 StoryboardPlan v3。',
        '【导演方法】'
        + f'{director.display_name}@{director.version} · mode={director.mode}',
        '【导演工作流】\n' + director.workflow_instructions,
        '【具体事件规则】\n' + director.scene_design_rules,
        '【调度与摄影机规则】\n' + director.shot_design_rules,
        '【连续性规则】\n' + director.continuity_rules,
        '【媒介规则】\n' + director.media_rules,
        '【独立 Visual Style】'
        + f'{visual_style.display_name}@{visual_style.version}\n'
        + visual_style.director_prompt,
        '【Style 构图语法】\n' + visual_style.storyboard_rules,
        '【Style 静态语言】\n' + visual_style.image_rules,
        '【Style 动态语言】\n' + visual_style.motion_rules,
        '【参考素材边界】' + reference_scope,
        '【职责边界】VisualBible 建立全片视觉世界和必要连续性。每个 ShotContextIR '
        '必须把语义目标落实为具体事件、主体调度、环境、构图、起止状态、连续性承接、'
        '可执行摄影机方案和媒介理由。visual_metaphor 可以为空，绝不能替代具体事件。'
        '下游只读取 VisualBible 与 ShotContextIR，不会重新解释 Director Skill。',
        '【导演质量门槛】' + '；'.join(director.critic_rules),
        '【Style 排除】' + '；'.join(visual_style.negative_rules),
    )
