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
