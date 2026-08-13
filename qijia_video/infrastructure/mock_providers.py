"""跑通链路用的确定性 Provider；不伪装成真实 AI 服务。"""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

from qijia_video.contracts import (
    AssetBible,
    ContentFormat,
    DirectorReview,
    DirectorTreatment,
    EditorialAngle,
    EditorialPlan,
    NarrationAudioSegment,
    NarrationManifest,
    MultimodalReferenceIR,
    ScriptBeat,
    ScriptDraft,
    ScriptReview,
    SourceCard,
    StoryboardPlan,
    StoryboardShot,
    ShotContextIR,
    VisualBible,
    content_hash,
    storyboard_review_hash,
    timestamp,
)
from qijia_video.ports import GeneratedFile


class TemplateScriptProvider:
    name = "template-mock"

    async def generate(
        self, card: SourceCard, prompt: str | None = None
    ) -> ScriptDraft:
        del prompt
        subject = card.subject.name
        boundaries = "；".join(item.text for item in card.interpretation_boundary[:2])
        facts = list(card.verified_facts)
        first_fact = facts[0] if facts else None
        second_fact = facts[1] if len(facts) > 1 else first_fact
        is_news = card.content_format == ContentFormat.RECENT_NEWS
        hook = (
            f"{subject}最近发生了什么？先把已经确认的变化和仍待验证的判断分开。"
            if is_news
            else f"{card.parent_question} 真正需要分辨的，往往不是一句口号，而是它的依据和边界。"
        )
        script = ScriptDraft(
            schema_version="3.0",
            source_card_id=card.id,
            source_card_revision=card.revision,
            video_title=card.title,
            cover_text=(f"{subject} 最新变化" if is_news else card.parent_question)[:30],
            hook=hook,
            beats=[
                ScriptBeat(
                    id="n01",
                    narration=hook,
                    role="hook",
                ),
                ScriptBeat(
                    id="n02",
                    narration=f"这次只围绕一个问题展开：{card.core_idea}",
                    role="context",
                ),
                ScriptBeat(
                    id="n03",
                    narration=(
                        first_fact.text
                        if first_fact
                        else "目前没有可核验材料证明这段表述的逐字归属，因此只能把它作为用户提供的命题来理解。"
                    ),
                    role="explanation",
                    source_refs=([first_fact.id] if first_fact else []),
                ),
                ScriptBeat(
                    id="n04",
                    narration=(
                        second_fact.text + " 这条材料帮助我们继续限定结论。"
                        if second_fact
                        else "理解它时，先区分能够确认的事实、合理解释和仍然未知的部分。"
                    ),
                    role="application",
                    source_refs=([second_fact.id] if second_fact else []),
                ),
                ScriptBeat(
                    id="n05",
                    narration=(
                        "所以，比急着接受一句有力量的话更重要的，是看清它在什么证据和语境下成立。"
                        + (f" 同时保留这条边界：{boundaries}" if boundaries else "")
                    ),
                    role="closing",
                    on_screen_text="先看依据，再下结论",
                ),
            ],
            closing="先看依据，再下结论。",
            estimated_duration_seconds=48,
            caption=card.title,
            hashtags=(
                ["最新动态", "事实核验", "新闻观察"]
                if is_news else ["人物观点", "知识解读", "事实核验"]
            ),
        )
        return script

    async def generate_direct_script(
        self,
        card: SourceCard,
        prompt: str,
        *,
        on_usage=None,
    ) -> ScriptDraft:
        del on_usage
        return await self.generate(card, prompt)

    async def generate_quality_script(
        self,
        card: SourceCard,
        prompt: str,
        *,
        on_usage=None,
    ) -> tuple[ScriptDraft, ScriptReview]:
        del on_usage
        script = await self.generate(card, prompt)
        review = await self.review(card, script)
        review.quality_scores = {
            'input_fidelity': 9,
            'central_insight': 8,
            'argument_progression': 8,
            'specificity': 8,
            'spoken_language': 8,
            'originality': 8,
            'factual_discipline': 9,
        }
        review.strengths = ['测试脚本保持单一中心判断并完整覆盖输入']
        review.preserve = ['开场判断与结尾收束']
        review.reviewed_draft_hash = content_hash(script)
        review.prompt_version = 'template_quality_script_v1'
        return script, review

    async def generate_with_plan(
        self,
        card: SourceCard,
        prompt: str,
        *,
        on_usage=None,
    ) -> tuple[EditorialPlan, ScriptDraft]:
        del on_usage
        script = await self.generate(card, prompt)
        evidence_refs = [
            item.id for item in (*card.verified_facts, *card.verified_quotes)
        ]
        plan = EditorialPlan(
            objective=f'准确回答：{card.parent_question}',
            central_question=card.parent_question,
            candidate_angles=[
                EditorialAngle(
                    angle_id='evidence_context',
                    premise='从出处、语境与证据边界解释用户提出的命题。',
                    audience_value='帮助观众区分原话、转述、解释与未知部分。',
                    evidence_refs=evidence_refs,
                    risk='证据较少时必须明确降级。',
                ),
                EditorialAngle(
                    angle_id='meaning_limits',
                    premise='从命题成立的条件与边界解释其现实含义。',
                    audience_value='避免把有力量的表达简化成口号。',
                    evidence_refs=evidence_refs,
                    risk='不能脱离原始语境强行应用。',
                ),
            ],
            selected_angle_id='evidence_context',
            selection_reason='该角度最忠实于来源卡并能提供可核验的解释增量。',
            core_thesis=card.core_idea,
            audience_promise=f'帮助{card.target_audience}理解命题的依据、含义与边界。',
            narrative_arc=[item.narration for item in script.beats[:5]],
            tone='准确、克制、具体、有思考感',
            must_include=[item.text for item in card.interpretation_boundary[:6]],
            must_avoid=['把用户输入冒充已经核验的人物逐字原话'],
            evidence_refs=evidence_refs,
            critic_summary='已核对输入忠实度、证据引用、段落推进和职责边界。',
            model_id=self.name,
            prompt_version='template_editorial_plan_v1',
            input_hash=content_hash({
                'card': card.model_dump(mode='json'),
                'prompt': prompt,
            }),
            generated_at=timestamp(),
        )
        return plan, script

    async def review(self, card: SourceCard, script: ScriptDraft) -> ScriptReview:
        known_fact_ids = {item.id for item in card.verified_facts}
        known_quote_ids = {item.id for item in card.verified_quotes}
        referenced = {
            ref for segment in script.narration_segments for ref in segment.source_refs
        }
        unknown = sorted(referenced - known_fact_ids - known_quote_ids)
        missing_boundaries = [
            item.id
            for item in card.interpretation_boundary
            if item.text not in script.narration_text()
        ]
        blocking = []
        if unknown:
            blocking.append(f"包含未知引用：{unknown}")
        return ScriptReview(
            passed=not blocking,
            claim_checks=[
                {
                    "fact_id": item.id,
                    "status": "referenced" if item.id in referenced else "not_used",
                }
                for item in card.verified_facts
            ],
            quote_checks=[
                {
                    "quote_id": item.id,
                    "status": "referenced" if item.id in referenced else "not_used",
                }
                for item in card.verified_quotes
            ],
            boundary_checks=[
                {
                    "boundary_id": item.id,
                    "status": "included" if item.id not in missing_boundaries else "manual_check",
                }
                for item in card.interpretation_boundary
            ],
            warnings=[
                f"解释边界需人工复核：{item}" for item in missing_boundaries
            ],
            blocking_reasons=blocking,
            model_id=f"{self.name}-reviewer",
            prompt_version="template_review_v1",
            input_hash=content_hash(script),
            reviewed_at=timestamp(),
        )


class TemplateStoryboardProvider:
    """Deterministic director plans for tests and the local demo."""

    name = "template-storyboard-mock"

    async def generate_quality_director_plan(
        self,
        script: ScriptDraft,
        director_instruction: str,
        narration_durations: dict[str, float],
        *,
        director_skill_id: str,
        director_skill_version: str,
        input_hash: str,
        reference_image_url: str = '',
        on_usage=None,
    ) -> tuple[DirectorTreatment, VisualBible, AssetBible, StoryboardPlan]:
        bible, plan = await self.generate_director_plan(
            script,
            director_instruction,
            narration_durations,
            director_skill_id=director_skill_id,
            director_skill_version=director_skill_version,
            input_hash=input_hash,
            on_usage=on_usage,
        )
        is_paper = (
            '纸张' in director_instruction or '纸艺' in director_instruction
        )
        treatment = DirectorTreatment(
            visual_thesis='让同一主体面对一连串可见选择，使视觉结果随论证逐章推进。',
            audience_experience='先看见冲突，再理解机制，最后看到判断落入现实的代价。',
            chapter_progression=[
                '直接进入核心冲突',
                '揭开决定关系的机制',
                '让选择产生可见后果',
            ],
            motif_system=['贯穿全片的核心主体', '状态持续变化的关键物件'],
            rhythm_strategy='关键判断使用稳定构图，转折处以明确动作改变画面状态。',
            edit_pattern='以动作结果或构图方向承接章节，独立论证节点允许克制硬切。',
            style_application=(
                '纸材厚度、纤维、接触阴影和逐格运动共同承担解释。'
                if is_paper
                else '编辑插画用层次、留白和克制运动承担解释。'
            ),
            model_id=self.name,
            input_hash=input_hash,
            created_at=timestamp(),
        )
        references = (
            [
                MultimodalReferenceIR(
                    reference_id='global_reference',
                    roles=['style'],
                    applies_to=['all_shots'],
                    retention_level='strong',
                    preserve=['媒介、色彩、材质和光线'],
                    allow_change=['人物、场景、构图和动作'],
                    forbidden_transfer=['可读文字、Logo、偶然背景和事实主张'],
                )
            ]
            if reference_image_url
            else []
        )
        asset_bible = AssetBible(
            subjects=['核心主体：身份、轮廓、服装与比例在全片保持一致'],
            locations=['统一视觉世界中的主场景及其可辨认空间锚点'],
            props=['一个随论证改变状态的关键物件'],
            identity_locks=['主体身份、年龄感、轮廓和服装结构不得漂移'],
            material_locks=['全片使用同一材质尺度、光线方向和接触阴影'],
            allowed_variations=['表情、姿态、景别和与论证有关的环境状态'],
            motion_grammar=[
                (
                    '纸片沿平面滑动、翻折或逐格转动，保持纸材物理结构'
                    if is_paper
                    else '主体动作清楚，摄影机运动克制且只服务信息变化'
                )
            ],
            review_criteria=['主体与资产连续', '每章事件与旁白语义匹配'],
            references=references,
            model_id=self.name,
            input_hash=input_hash,
            created_at=timestamp(),
        )
        plan.director_review = DirectorReview(
            passed=True,
            quality_scores={
                'script_fidelity': 9,
                'visual_thesis_execution': 8,
                'event_specificity': 8,
                'narrative_progression': 8,
                'continuity': 8,
                'camera_readability': 8,
                'media_discipline': 9,
                'producibility': 9,
            },
            strengths=['确定性测试分镜完整覆盖脚本并满足结构质量门'],
            reviewed_plan_hash=storyboard_review_hash(plan),
            model_id=f'{self.name}-reviewer',
            prompt_version='template_director_review_v1',
            reviewed_at=timestamp(),
        )
        return treatment, bible, asset_bible, plan

    async def generate_director_plan(
        self,
        script: ScriptDraft,
        director_instruction: str,
        narration_durations: dict[str, float],
        *,
        director_skill_id: str,
        director_skill_version: str,
        input_hash: str,
        on_usage=None,
    ) -> tuple[VisualBible, StoryboardPlan]:
        beat_ids = [item.id for item in script.beats]
        target_count = min(6, len(beat_ids))
        base_size, remainder = divmod(len(beat_ids), target_count)
        groups: list[list[str]] = []
        cursor = 0
        for index in range(target_count):
            size = base_size + (1 if index < remainder else 0)
            groups.append(beat_ids[cursor:cursor + size])
            cursor += size
        beats_by_id = {item.id: item for item in script.beats}
        visual_types: list[str] = []
        video_count = 0
        for index, group in enumerate(groups):
            duration = sum(float(narration_durations[item]) for item in group)
            role = beats_by_id[group[0]].role
            needs_motion = index == 0 or role in {'example', 'application'}
            use_video = needs_motion and duration <= 10.0 and video_count < 3
            visual_types.append('video' if use_video else 'image')
            video_count += int(use_video)
        bible, legacy_plan = await self.generate_with_direction(
            script,
            director_instruction,
            groups,
            visual_types,
            director_skill_id=director_skill_id,
            director_skill_version=director_skill_version,
            on_usage=on_usage,
        )
        shots: list[StoryboardShot] = []
        for index, shot in enumerate(legacy_plan.shots, 1):
            context = shot.context
            if context is None:
                raise ValueError('模板 Director 缺少 ShotContextIR')
            context = context.model_copy(update={
                'concrete_event': (
                    f'核心主体在可辨认场景中完成第 {index} 章的关键行动，'
                    '环境或他者立即产生可见反馈，结果改变下一章的理解起点。'
                ),
                'blocking': (
                    '核心主体位于竖屏中景，关键物件位于其行动方向；'
                    '主体完成一次明确位移或操作，反馈对象保持清楚空间关系。'
                ),
                'visual_metaphor': '',
            })
            shots.append(shot.model_copy(update={
                'context': context,
                'visual_intent': context.semantic_goal,
            }))
        return (
            bible.model_copy(update={'input_hash': input_hash}),
            StoryboardPlan(
                schema_version='3.0',
                shots=shots,
                model_id=self.name,
                prompt_version='template_director_concrete_event_v2',
                input_hash=input_hash,
                created_at=timestamp(),
            ),
        )

    async def generate_with_direction(
        self,
        script: ScriptDraft,
        director_instruction: str,
        beat_groups: list[list[str]],
        visual_types: list[str],
        *,
        director_skill_id: str,
        director_skill_version: str,
        on_usage=None,
    ) -> tuple[VisualBible, StoryboardPlan]:
        del on_usage
        has_reference_image = '存在一张全局参考图' in director_instruction
        legacy = await self.generate(
            script,
            director_instruction,
            beat_groups,
            visual_types,
        )
        shots: list[StoryboardShot] = []
        previous_end = '开场直接进入核心关系与变化'
        for index, shot in enumerate(legacy.shots, 1):
            context = ShotContextIR(
                semantic_goal=shot.visual_intent,
                visual_metaphor=f'第 {index} 章独有的主体关系与状态变化',
                subject='延续 VisualBible 中的核心主体与关键物件',
                action=f'落实本章旁白中的一个可观察动作：{shot.visual_intent}',
                environment='延续统一的编辑插画空间与时间光线',
                composition=shot.first_frame_prompt,
                continuity_handoff=previous_end,
                start_state=f'承接第 {max(1, index - 1)} 章结束状态',
                end_state=f'第 {index} 章的信息关系已经清楚可见',
                camera_intent=shot.motion_prompt,
                media_rationale=(
                    '连续动作对理解不可替代'
                    if shot.visual_type == 'video'
                    else '静态关系足以表达本章语义'
                ),
                reference_roles=(['identity'] if has_reference_image else []),
            )
            previous_end = context.end_state
            shots.append(shot.model_copy(update={
                'context': context,
                'visual_intent': context.semantic_goal,
                'first_frame_prompt': '',
                'motion_prompt': '',
            }))
        if '高级编辑纸张拼贴' in director_instruction:
            visual_world = (
                '高级编辑纸张拼贴世界：利落裁切、局部撕边、纸纤维、半色调颗粒、'
                '轻微套色误差与真实叠层接触阴影。'
            )
            color_material_system = (
                '成熟克制的限定配色；所有纸层厚度、纤维、印刷网点与投影方向一致。'
            )
            composition_system = (
                '每章使用 3—6 个有信息作用的裁纸元素，以杂志编辑留白突出一个关系变化。'
            )
        elif '精致手工纸艺定格' in director_instruction:
            visual_world = (
                '真实摄影棚中的精致纸艺微缩舞台：厚纸板、折纸与层叠卡纸具有明确'
                '切边、折痕、连接点和物理距离。'
            )
            color_material_system = (
                '暖色摄影棚光与克制限定色；纸材厚度、接缝、支撑和接触阴影始终一致。'
            )
            composition_system = (
                '前中后景保持清楚物理层次，每章只用纸偶可完成的一个动作表达语义变化。'
            )
        else:
            visual_world = (
                '统一、克制、可读的竖屏现代编辑插画世界，主体与空间关系真实明确。'
            )
            color_material_system = (
                '低饱和限定色与细腻材质，全片保持一致光线、尺度和接触阴影。'
            )
            composition_system = '每章只突出一个主体关系或状态变化，并保留字幕安全区。'
        bible = VisualBible(
            core_visual_idea='用同一主体和关键物件的连续状态变化推进完整论证。',
            visual_world=visual_world,
            recurring_subjects=['核心主体', '一个贯穿全片的关键物件'],
            scene_anchors=['统一空间关系', '稳定时间光线'],
            continuity_rules=['主体身份不变', '后一章承接前一章结束状态'],
            color_material_system=color_material_system,
            composition_system=composition_system,
            reference_strategy='无声明角色时不假定参考素材拥有控制权。',
            forbidden_elements=['可读文字', '重复构图', '无关装饰'],
            director_skill_id=director_skill_id,
            director_skill_version=director_skill_version,
            model_id=self.name,
            input_hash=legacy.input_hash,
            created_at=timestamp(),
        )
        return bible, StoryboardPlan(
            schema_version='2.0',
            shots=shots,
            model_id=self.name,
            prompt_version='template_director_context_v1',
            input_hash=legacy.input_hash,
            created_at=timestamp(),
        )

    async def generate(
        self,
        script: ScriptDraft,
        base_style: str,
        beat_groups: list[list[str]],
        visual_types: list[str],
    ) -> StoryboardPlan:
        shot_count = len(beat_groups)
        expected_ids = [item.id for item in script.beats]
        flat_ids = [beat_id for group in beat_groups for beat_id in group]
        compressed_ids = [
            beat_id
            for index, beat_id in enumerate(flat_ids)
            if index == 0 or beat_id != flat_ids[index - 1]
        ]
        covers_script = (
            flat_ids == expected_ids
            or (
                all(len(group) == 1 for group in beat_groups)
                and compressed_ids == expected_ids
            )
        )
        adaptive_media = not visual_types
        if (
            not 3 <= shot_count <= 12
            or any(not group for group in beat_groups)
            or not covers_script
            or (
                not adaptive_media
                and (
                    len(visual_types) != shot_count
                    or any(item not in {"image", "video"} for item in visual_types)
                )
            )
        ):
            raise ValueError("模板分镜需要 3-12 个有序非空叙事段组")
        segments = {item.id: item for item in script.beats}
        selected_types = list(visual_types)
        if adaptive_media:
            selected_types = []
            video_count = 0
            for index, beat_ids in enumerate(beat_groups):
                role = segments[beat_ids[0]].role
                moving = index == 0 or role in {"example", "application"}
                visual_type = (
                    "video" if moving and video_count < 3 else "image"
                )
                selected_types.append(visual_type)
                video_count += visual_type == "video"
        scenes = [
            (
                "核心主体处于关键变化的动作起点，中景构图，前后状态形成清楚对比",
                "动作从第一帧开始，镜头克制推进并停在关键变化上",
            ),
            (
                "延续同一主体和空间，近景聚焦一个能够解释变化的动作或物件细节",
                "镜头从整体关系缓慢推进到关键细节，保持轻微视差",
            ),
            (
                "用主体、环境和关键物件的空间关系呈现本段机制",
                "镜头在相关主体之间缓慢横移，以景深变化揭示关系",
            ),
            (
                "延续统一视觉锚点，展示变化发生后的下一步动作或影响",
                "主体完成一个明确动作，镜头轻缓跟随并停在新状态",
            ),
            (
                "回到贯穿全片的核心主体或物件，呈现结果和仍待观察的信号",
                "最后一个自然动作完成后，镜头缓慢拉远并保留观察空间",
            ),
        ]
        shots = []
        for index, beat_ids in enumerate(beat_groups, 1):
            scene_index = round(
                (index - 1) * (len(scenes) - 1) / max(1, shot_count - 1)
            )
            scene = scenes[scene_index]
            segment_id = beat_ids[0]
            segment = segments[segment_id]
            shots.append(StoryboardShot(
                shot_id=f"shot_{index:02d}",
                segment_id=segment_id,
                beat_ids=beat_ids,
                narration_excerpt="\n".join(segments[item].text for item in beat_ids),
                visual_type=selected_types[index - 1],
                visual_intent=(
                    f"从本段旁白提炼第 {index} 个具体、可观察的语义变化"
                ),
                first_frame_prompt=(
                    f"{scene[0]}。"
                    "竖屏构图，底部留出字幕安全区，无文字。"
                ),
                motion_prompt=scene[1],
            ))
        input_payload = {
            "script_hash": content_hash(script),
            "base_style": base_style,
            "beat_groups": beat_groups,
        }
        if visual_types:
            input_payload["visual_types"] = visual_types
        return StoryboardPlan(
            shots=shots,
            model_id=self.name,
            prompt_version="template_storyboard_v1",
            input_hash=content_hash(input_payload),
            created_at=timestamp(),
        )


def _write_silence(path: Path, duration_seconds: float, sample_rate: int = 48000):
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, int(round(duration_seconds * sample_rate)))
    silence = b"\x00\x00" * min(sample_rate, frames)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        remaining = frames
        while remaining:
            count = min(remaining, sample_rate)
            output.writeframes(silence[:count * 2])
            remaining -= count


class SilentTtsProvider:
    name = "silent-mock"

    async def synthesize(
        self,
        script: ScriptDraft,
        workspace: Path,
        *,
        voice_id: str | None = None,
        speed_ratio: float = 1.0,
    ) -> tuple[NarrationManifest, list[GeneratedFile]]:
        total_chars = max(1, sum(len(item.narration) for item in script.beats))
        total_duration = float(max(
            45,
            min(55, int(script.estimated_duration_seconds)),
        ))
        cursor = 0.0
        segments: list[NarrationAudioSegment] = []
        consumed_chars = 0
        for index, item in enumerate(script.beats):
            consumed_chars += len(item.narration)
            end = (
                total_duration
                if index == len(script.beats) - 1
                else total_duration * consumed_chars / total_chars
            )
            duration = max(0.001, end - cursor)
            segments.append(NarrationAudioSegment(
                segment_id=item.id,
                text=item.narration,
                asset_id="narration_full",
                start_seconds=round(cursor, 3),
                duration_seconds=round(duration, 3),
            ))
            cursor = end
        full_path = workspace / "audio" / "narration.wav"
        await asyncio.to_thread(_write_silence, full_path, total_duration)
        files = [GeneratedFile(
            "narration_full", full_path, "audio/wav", total_duration
        )]
        return NarrationManifest(
            provider=self.name,
            voice_id=voice_id or "silent-placeholder",
            speed_ratio=speed_ratio,
            total_duration_seconds=total_duration,
            full_audio_asset_id="narration_full",
            segments=segments,
        ), files

    async def synthesize_preview(
        self,
        text: str,
        workspace: Path,
        *,
        voice_id: str,
        speed_ratio: float,
        on_usage=None,
    ) -> GeneratedFile:
        preview_duration = max(1.0, len(str(text or "")) / (4.1 * speed_ratio))
        path = workspace / "audio" / "narration-preview.wav"
        await asyncio.to_thread(_write_silence, path, preview_duration)
        return GeneratedFile(
            "narration_preview",
            path,
            "audio/wav",
            preview_duration,
        )
