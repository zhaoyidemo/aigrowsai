'''Compile one Director Skill instruction without provider prompt syntax.'''
from __future__ import annotations

from qijia_video.contracts import DirectorSkillSnapshot


def _join(*blocks: str) -> str:
    return '\n\n'.join(str(item or '').strip() for item in blocks if str(item or '').strip())


def compile_director_instruction(
    director: DirectorSkillSnapshot,
    *,
    has_reference_image: bool,
) -> str:
    '''Return the sole visual-planning instruction consumed by the director model.'''

    reference_scope = (
        '存在一张全局参考图。只在每个 ShotContextIR.reference_roles 中声明它的用途；'
        '身份、场景与风格权力不得自动混用，参考图也不得覆盖脚本事实。'
        if has_reference_image
        else '没有全局参考图，由本 Director Skill 完整定义视觉世界与连续性。'
    )
    return _join(
        '【唯一视觉负责人】你是本任务选中的 Director Skill。已确认脚本是内容唯一真相；'
        '不得改写事实、引语、论点或旁白，也不得生成任何供应商提示词。你的唯一职责是把'
        '完整脚本转化为 VisualBible 与逐章 ShotContextIR。',
        '【导演方法】\n' + director.directing_instructions,
        '【分镜规则】\n' + director.storyboard_rules,
        '【静态艺术方向】\n' + director.image_art_direction,
        '【动态艺术方向】\n' + director.motion_art_direction,
        '【参考素材边界】' + reference_scope,
        '【职责边界】VisualBible 只建立视觉世界与连续性。ShotContextIR 只写可观察的'
        '语义目标、视觉隐喻、主体、动作、环境、构图、起止状态、连续性承接、镜头意图和'
        '媒介理由。必须把本 Skill 的静态艺术、动态语言、材质、色彩和构图决定完整固化'
        '到 VisualBible 与各 ShotContextIR；下游只读取这两项标准产物，不会再次读取本 '
        'Skill 的原始规则。不得输出首帧提示词、I2V 提示词、模型参数或第二套导演方法。',
        '【统一排除】' + '；'.join(director.negative_rules),
    )
