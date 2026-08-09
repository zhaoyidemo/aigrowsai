"""跑通链路用的确定性 Provider；不伪装成真实 AI 服务。"""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

from qijia_video.contracts import (
    ContentFormat,
    NarrationAudioSegment,
    NarrationManifest,
    ScriptBeat,
    ScriptDraft,
    ScriptReview,
    SourceCard,
    StoryboardPlan,
    StoryboardShot,
    content_hash,
    timestamp,
)
from qijia_video.ports import GeneratedFile


class TemplateScriptProvider:
    name = "template-mock"

    async def generate(
        self, card: SourceCard, prompt: str | None = None
    ) -> ScriptDraft:
        del prompt
        primary_fact = card.verified_facts[0]
        source_refs = [primary_fact.id]
        subject = card.subject.name
        if card.content_format == ContentFormat.RECENT_NEWS:
            secondary_fact = (
                card.verified_facts[1]
                if len(card.verified_facts) > 1
                else primary_fact
            )
            return ScriptDraft(
                source_card_id=card.id,
                source_card_revision=card.revision,
                video_title=card.title,
                cover_text=f"{subject} 最新变化"[:30],
                hook=f"{subject} 最近发生了什么？先看截至本次检索已经确认的变化。",
                beats=[
                    ScriptBeat(
                        id="n01",
                        narration=f"{subject} 最近发生了什么？先看截至本次检索已经确认的变化。",
                        role="hook",
                        visual_direction="核心主体处于变化发生前后的交界，关键物件开始运动。",
                        source_refs=source_refs,
                    ),
                    ScriptBeat(
                        id="n02",
                        narration=(
                            f"这次只围绕一个问题：{card.core_idea}"
                            "先区分已经发生的事实、官方计划和仍待验证的效果。"
                        ),
                        role="context",
                        visual_direction="同一信息空间里，已确认事实与待观察信号形成清楚层次。",
                        source_refs=source_refs,
                    ),
                    ScriptBeat(
                        id="n03",
                        narration=primary_fact.text,
                        role="explanation",
                        visual_direction="延续同一主体，用具体动作和前后状态呈现第一条证据。",
                        source_refs=source_refs,
                    ),
                    ScriptBeat(
                        id="n04",
                        narration=(
                            secondary_fact.text
                            + " 两个来源共同确认的部分可以转述，效果判断仍要保留边界。"
                        ),
                        role="application",
                        visual_direction="从官方主体转向独立观察视角，保持相同视觉锚点。",
                        source_refs=[secondary_fact.id],
                    ),
                    ScriptBeat(
                        id="n05",
                        narration=(
                            "对普通关注者来说，接下来最值得看的不是宣传词，"
                            "而是功能是否真正可用，以及后续是否出现独立证据。"
                        ),
                        role="closing",
                        visual_direction="镜头回到核心主体并拉远，保留一个明确的后续观察信号。",
                        on_screen_text="继续看可验证的变化",
                        source_refs=[secondary_fact.id],
                    ),
                ],
                closing="把结论留给下一条可验证的公开信息。",
                estimated_duration_seconds=48,
                caption=card.title,
                hashtags=["最新动态", "科技新闻", "商业观察"],
            )
        boundaries = "；".join(item.text for item in card.interpretation_boundary[:2])
        script = ScriptDraft(
            source_card_id=card.id,
            source_card_revision=card.revision,
            video_title=card.title,
            cover_text=card.parent_question[:30],
            hook=f"{card.parent_question} 这可能不是一句简单的是非题。",
            beats=[
                ScriptBeat(
                    id="n01",
                    narration=f"{card.parent_question} 这可能不是一句简单的是非题。",
                    role="hook",
                    visual_direction="家长准备替孩子完成任务，孩子下意识把手收回。",
                    source_refs=source_refs,
                ),
                ScriptBeat(
                    id="n02",
                    narration=f"我们先从{subject}谈起。这个内容真正想解释的是：{card.core_idea}",
                    role="context",
                    visual_direction="亲子隔着餐桌对坐，中间留出一块安静的空间。",
                    source_refs=source_refs,
                ),
                ScriptBeat(
                    id="n03",
                    narration=primary_fact.text,
                    role="explanation",
                    visual_direction="孩子独立尝试，家长伸出的手停在半空并慢慢收回。",
                    source_refs=source_refs,
                ),
                ScriptBeat(
                    id="n04",
                    narration="放回家庭场景里，重要的不是给父母贴标签，而是看见互动方式怎样影响孩子的参与感，并为下一次沟通留出一点调整空间。",
                    role="application",
                    visual_direction="孩子自己完成一个具体动作，家长退后半步安静陪伴。",
                    source_refs=source_refs,
                ),
                ScriptBeat(
                    id="n05",
                    narration=(
                        "今天可以先问自己：我是在替孩子完成，还是在帮助孩子逐渐学会自己完成？"
                        + (f" 需要同时记住这个边界：{boundaries}" if boundaries else "")
                    ),
                    role="closing",
                    visual_direction="亲子一起走向开阔门廊，保持自然的一步距离。",
                    on_screen_text="陪伴，不是接管",
                    source_refs=source_refs,
                ),
            ],
            closing="把答案留给下一次真实的家庭互动。",
            estimated_duration_seconds=48,
            caption=card.title,
            hashtags=["家庭教育", "教育心理学", "亲子沟通"],
        )
        return script

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
        if any(not item.source_refs for item in script.narration_segments):
            blocking.append("存在没有来源引用的旁白段落")
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
    """Deterministic visual metaphors for tests and the local demo."""

    name = "template-storyboard-mock"

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
        if (
            not 5 <= shot_count <= 13
            or any(not group for group in beat_groups)
            or not covers_script
            or len(visual_types) != shot_count
            or visual_types.count("video") != 3
            or visual_types.count("image") != shot_count - 3
        ):
            raise ValueError("模板分镜需要 5-13 个有序非空叙事段组")
        segments = {item.id: item for item in script.beats}
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
                visual_type=visual_types[index - 1],
                visual_intent=(
                    f"用连续视觉叙事的第 {index} 个画面承载语义推进："
                    f"{segment.visual_direction}"
                ),
                first_frame_prompt=(
                    f"{segment.visual_direction}。{scene[0]}。"
                    "竖屏构图，底部留出字幕安全区，无文字。"
                ),
                motion_prompt=scene[1],
            ))
        return StoryboardPlan(
            shots=shots,
            model_id=self.name,
            prompt_version="template_storyboard_v1",
            input_hash=content_hash({
                "script_hash": content_hash(script),
                "base_style": base_style,
                "beat_groups": beat_groups,
                "visual_types": visual_types,
            }),
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
