"""Input-first prompt compilation for model-led script generation."""
from __future__ import annotations

import json

from qijia_video.contracts import (
    ContentSkillSnapshot,
    CreativeInputSnapshot,
    NewsResearchBrief,
    PersonResearchBrief,
    PromptAdapterSnapshot,
    PromptWritingProfileSnapshot,
    ScriptSkillSnapshot,
    SourceCard,
    content_hash,
    timestamp,
)


def _join_blocks(*blocks: str) -> str:
    return "\n\n".join(
        str(block or "").strip()
        for block in blocks
        if str(block or "").strip()
    )


def _evidence_pack(
    card: SourceCard,
    research_brief: PersonResearchBrief | NewsResearchBrief | None = None,
) -> dict:
    """Return the one evidence representation consumed by creative writing."""

    pack = {
        "facts": [item.model_dump(mode="json") for item in card.verified_facts],
        "quotes": [item.model_dump(mode="json") for item in card.verified_quotes],
        "sources": [item.model_dump(mode="json") for item in card.sources],
        "boundaries": [
            item.model_dump(mode="json")
            for item in card.interpretation_boundary
        ],
        "uncertainties": (
            list(research_brief.uncertainties) if research_brief else []
        ),
    }
    if isinstance(research_brief, PersonResearchBrief):
        pack["attribution"] = {
            "input_type": research_brief.input_type,
            "status": research_brief.attribution_status,
            "verified_wording": research_brief.verified_wording,
            "note": research_brief.attribution_note,
            "source_context": research_brief.source_context,
        }
    elif isinstance(research_brief, NewsResearchBrief):
        pack["research_as_of"] = research_brief.as_of
    return pack


def compile_direct_script_prompt(
    input_snapshot: CreativeInputSnapshot,
    *,
    prompt_adapter: PromptAdapterSnapshot,
    content_policy: ContentSkillSnapshot,
    script_skill: ScriptSkillSnapshot,
    minimum_characters: int,
    maximum_characters: int,
) -> str:
    """Compile the v3 direct prompt without an intake or planning surrogate."""

    materials = [
        {
            "material_id": f"material_{index:02d}",
            "title": item.title,
            "text": item.text,
            "url": item.url,
        }
        for index, item in enumerate(input_snapshot.verified_materials, start=1)
    ]
    knowledge_policy = {
        "mode": content_policy.knowledge_mode.value,
        "external_retrieval": False,
        "user_materials_are_verified_for_this_task": bool(materials),
        "model_knowledge_is_not_a_verified_source": True,
        "freshness_guarantee": False,
    }
    return _join_blocks(
        (
            "你是本任务唯一的 Script Skill。H3 Prompt Adapter 已把原始请求组织成以下"
            "指令边界，但没有替你做内容决策。请在一次调用内完成必要的角度取舍、结构设计、"
            "成稿与内部审稿，最终只交付口播稿；不要展示任何中间分析、计划或视觉方案。"
        ),
        (
            f"【内部 H3 Prompt Adapter】{prompt_adapter.adapter_id}@"
            f"{prompt_adapter.version}\n{prompt_adapter.compilation_framework}\n- "
            + "\n- ".join(prompt_adapter.quality_rules)
        ),
        "【用户原始创作请求｜最高优先级】\n" + input_snapshot.original_request,
        (
            "【用户明确核对的材料】\n"
            + json.dumps(materials, ensure_ascii=False)
            + "\n这里没有材料时，不能把模型记忆伪装成用户材料或已核对来源。"
        ),
        "【知识与事实边界】\n"
        + json.dumps(knowledge_policy, ensure_ascii=False)
        + "\n禁止联网搜索、浏览器、检索插件或外部研究工具。可以使用模型训练中已有的稳定知识"
        "解释背景、概念和思想脉络，但不得声称本次已经查证。人物归属、逐字引语、精确出处、"
        "版本、日期、数据和最新动态把握不足时，必须降格表述或提醒人工复核。",
        "【硬性政策】\n政策 ID："
        + "、".join(content_policy.policy_ids)
        + "\n- "
        + "\n- ".join(content_policy.quality_rules),
        f"【Script Skill】{script_skill.skill_id}@{script_skill.version}",
        "【内部创作方法】\n" + script_skill.planning_instructions,
        "【口播写作方法】\n" + script_skill.writing_instructions,
        "【交付前内部审稿】\n- " + "\n- ".join(script_skill.critic_rules),
        (
            "【口播边界】\n"
            f"所有 narration 合计建议 {minimum_characters}-{maximum_characters} 个汉字，"
            "目标 45—75 秒，内容完整和自然优先。只有用户明确核对材料对应的 material ID "
            "可以写入 source_refs；模型已有知识、解释、过渡和编辑判断保持为空。用户输入中的"
            "候选引语如果没有核对材料，不得写成已核验逐字原话。最终只输出 ScriptDraft。"
        ),
    )


def compile_script_skill_prompt(
    card: SourceCard,
    *,
    content_policy: ContentSkillSnapshot,
    script_skill: ScriptSkillSnapshot,
    research_brief: PersonResearchBrief | NewsResearchBrief | None,
    minimum_characters: int,
    maximum_characters: int,
) -> str:
    '''Compile the v2 editorial prompt owned by exactly one Script Skill.'''

    original_input = {
        'subject': (
            card.subject.model_dump(mode='json')
            if card.subject.type != 'topic'
            else None
        ),
        'original_input': card.core_idea,
        'focus_question': card.parent_question,
        'target_audience': card.target_audience,
        'content_format': card.content_format.value,
    }
    knowledge_policy = {
        'mode': content_policy.knowledge_mode.value,
        'external_retrieval': False,
        'allowed_inputs': [
            'immutable_original_input',
            'user_provided_verified_material',
            'model_pretrained_knowledge',
        ],
        'freshness_guarantee': False,
        'source_rule': (
            'Only fact/quote IDs already present in ContextPack may be used in '
            'source_refs. Model knowledge must never receive an invented source ID.'
        ),
    }
    return _join_blocks(
        '你是任务冻结的唯一 Script Skill。直接理解完整原始输入，使用模型已有知识完成'
        '内容判断、角度规划、论证结构与口播。Input Policy 只限定可用知识和事实边界，'
        '不是第二个创作负责人。不得设计画面，也不得调用任何媒体提示词方法。',
        '【不可变原始输入】\n'
        + json.dumps(original_input, ensure_ascii=False),
        '【唯一 ContextPack（用户提供材料）】\n'
        + json.dumps(_evidence_pack(card, research_brief), ensure_ascii=False),
        '【知识方式】\n'
        + json.dumps(knowledge_policy, ensure_ascii=False)
        + '\n禁止联网搜索、浏览器、检索插件和外部研究工具。可以使用模型训练中已有的稳定知识，'
        '但不得声称它已经被本次任务实时查证。不得虚构链接、书名章节、版本、精确日期、'
        '统计数据或最新动态；把握不足时采用克制表述、解释观点本身或提醒人工确认。',
        '【冻结的事实、安全与质量政策】\n政策 ID：'
        + '、'.join(content_policy.policy_ids)
        + '\n- '
        + '\n- '.join(content_policy.quality_rules)
        + '\n这些是硬约束，不是第二套写作风格；发生冲突时优先遵守。',
        f'【Script Skill】{script_skill.skill_id}@{script_skill.version}',
        '【角度规划方法】\n' + script_skill.planning_instructions,
        '【脚本写作方法】\n' + script_skill.writing_instructions,
        '【内部审稿规则】\n- ' + '\n- '.join(script_skill.critic_rules),
        (
            '【口播边界】\n'
            f'所有 narration 合计建议 {minimum_characters}-{maximum_characters} 个汉字，'
            '目标 45—75 秒；内容完整和自然优先。只有 ContextPack 已存在的 fact/quote 才能'
            '填写 source_refs；使用模型已有知识、解释、过渡或编辑判断时保持为空。用户输入'
            '中的候选引语若没有 verified_quote，不得写成已经核验的人物逐字原话。最终只交付'
            'EditorialPlan 与 ScriptDraft，不交付任何视觉决策。'
        ),
    )


def compile_legacy_h3_script_prompt(
    card: SourceCard,
    *,
    profile: PromptWritingProfileSnapshot | None,
    research_brief: PersonResearchBrief | NewsResearchBrief | None,
    minimum_characters: int,
    maximum_characters: int,
) -> str:
    """Compile the frozen Pipeline v1 H3 brief-and-script instruction."""

    framework = (
        profile.creative_brief_framework.strip()
        if profile and profile.creative_brief_framework.strip()
        else (
            "先从原始输入与 EvidencePack 生成唯一 CreativeBrief，再依据该总纲"
            "写完整口播。不得套用预设主题，不得逐段设计画面。"
        )
    )
    original_input = {
        "subject": (
            card.subject.model_dump(mode="json")
            if card.subject.type != "topic"
            else None
        ),
        "original_input": card.core_idea,
        "focus_question": card.parent_question,
        "target_audience": card.target_audience,
        "content_format": card.content_format.value,
    }
    evidence_pack = _evidence_pack(card, research_brief)
    return _join_blocks(
        (
            "你是本任务唯一的 H3 Creative Director。先生成 creative_brief，"
            "再严格按同一总纲写脚本；不要调用第二套内容模板。"
        ),
        "【不可变原始输入】\n"
        + json.dumps(original_input, ensure_ascii=False),
        "【唯一 EvidencePack】\n"
        + json.dumps(evidence_pack, ensure_ascii=False),
        f"【H3 CreativeBrief 方法】\n{framework}",
        (
            "【本任务口播边界】\n"
            f"所有 narration 合计建议 {minimum_characters}-{maximum_characters} 个汉字，"
            "目标 45-75 秒；内容完整和自然优先。先在内部写成连贯全文，再按真实语义变化"
            "拆段，不为凑数量切碎论证；通常 4-8 段，最少 3 段、最多 12 段。开场张力"
            "必须来自内容本身；不使用模板化寒暄、虚假悬念、"
            "恐吓或命令式 CTA。脚本不写逐镜头画面，事实主张才填写 source_refs，"
            "纯解释和过渡允许为空。"
        ),
    )
