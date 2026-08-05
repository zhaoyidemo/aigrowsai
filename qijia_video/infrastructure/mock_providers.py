"""跑通链路用的确定性 Provider；不伪装成真实 AI 服务。"""
from __future__ import annotations

import asyncio
import wave
from pathlib import Path

from qijia_video.contracts import (
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
    """Five deterministic visual metaphors for tests and the local demo."""

    name = "template-storyboard-mock"

    async def generate(
        self,
        script: ScriptDraft,
        base_style: str,
        beat_groups: list[list[str]],
        visual_types: list[str],
    ) -> StoryboardPlan:
        if (
            len(beat_groups) != 5
            or any(not group for group in beat_groups)
            or len(visual_types) != 5
            or visual_types.count("video") != 3
            or visual_types.count("image") != 2
        ):
            raise ValueError("模板分镜需要五个非空叙事段组")
        segments = {item.id: item for item in script.beats}
        scenes = [
            (
                "家长站在两条方向相反的柔和色带之间，停下惯性的动作",
                "家长缓慢放下抬起的手，两条色带轻轻分开，镜头微微推进",
            ),
            (
                "餐桌两侧的亲子之间留出一块可呼吸的空白空间",
                "孩子把一个积木移向中央，家长身体稍稍后退，轻微视差",
            ),
            (
                "一株幼苗从成人手掌的阴影边缘朝暖光生长",
                "手掌缓慢移开，幼苗舒展一片新叶，镜头轻柔下移",
            ),
            (
                "孩子独自系鞋带，家长在一步之外安静蹲下陪伴",
                "孩子完成最后一个动作并抬头，家长点头，缓慢推镜",
            ),
            (
                "亲子一起走向开阔门廊，彼此保持自然的一步距离",
                "门外暖光逐渐扩散，两人同步向前，镜头缓慢拉远",
            ),
        ]
        shots = []
        for index, (beat_ids, scene) in enumerate(
            zip(beat_groups, scenes), 1
        ):
            segment_id = beat_ids[0]
            segment = segments[segment_id]
            shots.append(StoryboardShot(
                shot_id=f"shot_{index:02d}",
                segment_id=segment_id,
                beat_ids=beat_ids,
                narration_excerpt="\n".join(segments[item].text for item in beat_ids),
                visual_type=visual_types[index - 1],
                visual_intent=f"用一个独立家庭隐喻承载第 {index} 个语义转折",
                first_frame_prompt=(
                    f"{scene[0]}。竖屏中心构图，底部留出字幕安全区，无文字。"
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
        self, script: ScriptDraft, workspace: Path
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
            voice_id="silent-placeholder",
            total_duration_seconds=total_duration,
            full_audio_asset_id="narration_full",
            segments=segments,
        ), files
