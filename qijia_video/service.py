"""齐家 AI 短视频应用服务。"""
from __future__ import annotations

import asyncio
import itertools
import json
import math
import re
import secrets
import shutil
import tempfile
import time
import uuid
import zipfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from qijia_video import MODULE_VERSION
from qijia_video.contracts import (
    Actor,
    ApprovalRecord,
    Artifact,
    AssetRef,
    ContentFormat,
    ContentSkillSnapshot,
    ContentDomain,
    CreativeInputSnapshot,
    CreativeMaterial,
    CreativeBrief,
    DouyinPerformance,
    DouyinPlaybackSnapshot,
    EditorialPlan,
    FirstFrameCandidate,
    GenerationSettings,
    JobState,
    InterpretationBoundary,
    MultimodalReferenceIR,
    NewsResearchBrief,
    PendingShotMediaEdit,
    PersonResearchBrief,
    PipelineVersion,
    PreGenerationMediaMode,
    PromptAdapterSnapshot,
    PromptWritingProfileSnapshot,
    ProviderTask,
    ProviderTaskState,
    ProviderUsageRecord,
    QualityReport,
    RenderManifest,
    SEEDANCE_BALANCED_MODEL,
    SEEDANCE_EFFICIENT_MODEL,
    SEEDANCE_FLAGSHIP_MODEL,
    ScriptDraft,
    ScriptBeat,
    ScriptReview,
    ScriptSkillSnapshot,
    ShotMediaVersion,
    SourceCard,
    SourceCardInput,
    SourceEntry,
    SourceCardStatus,
    SkillResearchMode,
    StoryboardPlan,
    StoryboardShot,
    StyleFrameCandidate,
    ScreenTextCue,
    SubtitleCue,
    VideoJob,
    VisualBible,
    VisualGenerationRequest,
    VisualShotVersion,
    VisualBlock,
    VerifiedFact,
    VerifiedQuote,
    content_hash,
    storyboard_review_hash,
    timestamp,
)
from qijia_video.cost_analysis import (
    USD_TO_CNY_RATE,
    build_douyin_performance_analysis,
)
from qijia_video.errors import (
    InvalidTransition,
    ProviderUnavailable,
    QualityGateFailed,
    ResourceNotFound,
    RevisionConflict,
)
from qijia_video.ports import (
    AggregateRepository,
    ArtifactStorage,
    DouyinPerformanceProvider,
    ImageProvider,
    QualityChecker,
    Renderer,
    ScriptProvider,
    StoryboardProvider,
    TtsProvider,
    VideoProvider,
    MediaPackager,
)
from qijia_video.prompts import (
    SCRIPT_HARD_MAX_CHARS,
    narration_char_count,
)
from qijia_video.prompt_orchestration import (
    compile_direct_script_prompt,
    compile_legacy_h3_script_prompt,
    compile_quality_script_prompt,
    compile_script_skill_prompt,
)
from qijia_video.prompt_adapter_registry import (
    PromptAdapterRegistry,
    default_prompt_adapter_registry,
)
from qijia_video.director_prompting import (
    DIRECTOR_RUNTIME_PROMPT_VERSION,
    compile_director_instruction,
)
from qijia_video.director_skill_registry import (
    DirectorSkillRegistry,
    DirectorSkillRegistryError,
    default_director_skill_registry,
)
from qijia_video.provider_prompting import (
    compile_image_provider_prompt,
    compile_style_frame_prompt,
    compile_video_provider_prompt,
)
from qijia_video.provider_adapter_registry import (
    ProviderAdapterRegistry,
    ProviderAdapterRegistryError,
    default_provider_adapter_registry,
)
from qijia_video.script_skill_registry import (
    ScriptSkillRegistry,
    ScriptSkillRegistryError,
    default_script_skill_registry,
)
from qijia_video.skill_registry import (
    ContentSkillRegistry,
    SkillRegistryError,
    default_skill_registry,
)
from qijia_video.tts_options import TTS_SCRIPT_CHARACTER_TARGETS
from qijia_video.upload_media import detect_shot_media_format
from qijia_video.visual_prompting import (
    compile_first_frame_prompt,
    compile_storyboard_base_style,
    compile_video_prompt,
)
from qijia_video.visual_style_registry import (
    PromptWritingProfileRegistry,
    VisualStyleRegistry,
    VisualStyleRegistryError,
    default_prompt_writing_profile_registry,
    default_visual_style_registry,
)


FORBIDDEN_PLACEHOLDERS = ("<仅示意", "仅填写已经核验")
FORBIDDEN_BRAND_TEXT = ("齐家AI", "齐家 AI")
REVIEW_BUNDLE_NAMES = (
    "draft.mp4",
    "cover.jpg",
    "caption.md",
    "sources.md",
    "subtitles.srt",
)
REQUIRED_PACKAGE_NAMES = {
    "final.mp4",
    "cover.jpg",
    "caption.md",
    "sources.md",
    "subtitles.srt",
    "quality_report.json",
    "artifact_manifest.json",
    "provenance.json",
}
RELEASE_ARCHIVE_NAME = "qijia-video-release.zip"
LEGACY_STORYBOARD_SHOT_COUNT = 5
SEEDANCE_VIDEO_SHOT_COUNT = 3
SEEDANCE_SHOT_DURATION_SECONDS = 8
VIDEO_OUTPUT_DIMENSIONS = {
    "480p": (480, 854),
    "720p": (720, 1280),
    "1080p": (1080, 1920),
}
SEEDANCE_MAX_NATURAL_CHAPTER_SECONDS = 10.0
MIN_VIDEO_DURATION_SECONDS = 45.0
MAX_VIDEO_DURATION_SECONDS = 75.0
TTS_PREVIEW_MAX_CHARACTERS = 60
DOUYIN_SNAPSHOT_RETENTION = 200
ProgressReporter = Callable[[dict], None]


class QijiaVideoService:
    def __init__(
        self,
        *,
        repository: AggregateRepository,
        script_provider: ScriptProvider,
        storyboard_provider: StoryboardProvider,
        image_provider: ImageProvider,
        tts_provider: TtsProvider,
        video_provider: VideoProvider,
        renderer: Renderer,
        storage: ArtifactStorage,
        quality_checker: QualityChecker,
        media_packager: MediaPackager,
        work_root: Path,
        douyin_performance_provider: DouyinPerformanceProvider | None = None,
        video_poll_interval_seconds: float = 5.0,
        video_timeout_seconds: float = 900.0,
        seedream_price_per_image: float = 0.22,
        seedance_price_per_million_tokens: float = 8.0,
        seedance_model_prices_per_million_tokens: dict[str, float] | None = None,
        tts_price_per_10000_characters: float = 5.0,
        tikhub_price_per_success_usd: float = 0.002,
        skill_registry: ContentSkillRegistry | None = None,
        script_skill_registry: ScriptSkillRegistry | None = None,
        director_skill_registry: DirectorSkillRegistry | None = None,
        visual_style_registry: VisualStyleRegistry | None = None,
        provider_adapter_registry: ProviderAdapterRegistry | None = None,
        prompt_adapter_registry: PromptAdapterRegistry | None = None,
        prompt_writing_profile_registry: (
            PromptWritingProfileRegistry | None
        ) = None,
    ):
        self.repository = repository
        self.script_provider = script_provider
        self.storyboard_provider = storyboard_provider
        self.image_provider = image_provider
        self.tts_provider = tts_provider
        self.video_provider = video_provider
        self.renderer = renderer
        self.storage = storage
        self.quality_checker = quality_checker
        self.media_packager = media_packager
        self.douyin_performance_provider = douyin_performance_provider
        self.skill_registry = skill_registry or default_skill_registry
        self.script_skill_registry = (
            script_skill_registry or default_script_skill_registry
        )
        self.director_skill_registry = (
            director_skill_registry or default_director_skill_registry
        )
        self.visual_style_registry = (
            visual_style_registry or default_visual_style_registry
        )
        self.prompt_writing_profile_registry = (
            prompt_writing_profile_registry
            or default_prompt_writing_profile_registry
        )
        self.provider_adapter_registry = (
            provider_adapter_registry or default_provider_adapter_registry
        )
        self.prompt_adapter_registry = (
            prompt_adapter_registry or default_prompt_adapter_registry
        )
        self.work_root = work_root.resolve()
        self.work_root.mkdir(parents=True, exist_ok=True)
        self.video_poll_interval_seconds = max(
            0.01, float(video_poll_interval_seconds)
        )
        self.video_timeout_seconds = max(30.0, float(video_timeout_seconds))
        self.seedream_price_per_image = max(
            0.0, float(seedream_price_per_image)
        )
        self.seedance_price_per_million_tokens = max(
            0.0, float(seedance_price_per_million_tokens)
        )
        self.seedance_model_prices_per_million_tokens = {
            str(model_id): max(0.0, float(rate))
            for model_id, rate in (
                seedance_model_prices_per_million_tokens or {}
            ).items()
            if str(model_id).strip()
        }
        self.tts_price_per_10000_characters = max(
            0.0, float(tts_price_per_10000_characters)
        )
        self.tikhub_price_per_success_usd = max(
            0.0, float(tikhub_price_per_success_usd)
        )
        self._douyin_performance_lock = asyncio.Lock()

    @staticmethod
    def _report(
        progress: ProgressReporter | None,
        *,
        message: str,
        stage: str,
        percent: int,
        **metadata,
    ) -> None:
        if progress:
            progress({
                "message": message,
                "stage": stage,
                "percent": max(0, min(100, int(percent))),
                **metadata,
            })

    @staticmethod
    def _assert_revision(actual: int, expected: int):
        if int(actual) != int(expected):
            raise RevisionConflict("内容已在其他页面更新，请刷新后重试")

    @staticmethod
    def _remember_usage_record(
        job: VideoJob,
        usage: ProviderUsageRecord,
    ) -> None:
        record = usage.model_copy(deep=True)
        existing = next(
            (
                item
                for item in job.usage_records
                if item.usage_id == record.usage_id
            ),
            None,
        )
        if existing:
            job.usage_records = [
                record if item.usage_id == record.usage_id else item
                for item in job.usage_records
            ]
        else:
            job.usage_records.append(record)

    async def _persist_usage_record(
        self,
        job: VideoJob,
        usage: ProviderUsageRecord,
        actor: Actor,
    ) -> VideoJob:
        record = usage.model_copy(deep=True)
        if (
            record.operation == "douyin_performance"
            and record.provider == "tikhub"
            and record.estimated_cost is None
            and (
                (
                    record.succeeded
                    and self.tikhub_price_per_success_usd > 0
                )
                or "失败响应" in record.note
            )
        ):
            record.estimated_currency = "CNY"
            record.estimated_cost = round(
                (
                    self.tikhub_price_per_success_usd * USD_TO_CNY_RATE
                    if record.succeeded
                    else 0
                ),
                8,
            )
            record.pricing_basis = (
                "TikHub 按 "
                f"¥{self.tikhub_price_per_success_usd * USD_TO_CNY_RATE:g}"
                "/成功请求估算；失败响应按 ¥0 估算；供应商账单优先"
            )
        elif (
            record.operation == "douyin_performance"
            and record.provider == "tikhub"
            and record.succeeded
            and record.estimated_cost is None
        ):
            record.note = "；".join(
                item
                for item in (
                    record.note,
                    "TikHub 成功请求规划价未配置，金额待供应商账单核对",
                )
                if item
            )
        elif (
            record.operation in ("tts_synthesis", "tts_preview")
            and record.provider == "volcengine-seed-tts-2.0"
            and record.succeeded
            and record.estimated_cost is None
            and record.unit == "character"
            and self.tts_price_per_10000_characters > 0
        ):
            # Set the currency first because assignment validation requires it
            # to exist as soon as an amount is present.
            record.estimated_currency = "CNY"
            record.estimated_cost = round(
                record.quantity * self.tts_price_per_10000_characters / 10000,
                8,
            )
            record.pricing_basis = (
                f"豆包语音合成按量刊例价 ¥{self.tts_price_per_10000_characters:g}"
                "/万字符；套餐、赠送额度与供应商账单优先"
            )
        elif (
            record.operation in ("tts_synthesis", "tts_preview")
            and record.provider == "volcengine-seed-tts-2.0"
            and record.succeeded
            and record.estimated_cost is None
        ):
            record.note = "；".join(
                item
                for item in (
                    record.note,
                    "豆包语音单价未配置，金额待供应商账单核对",
                )
                if item
            )
        self._remember_usage_record(job, record)
        return await self._save_job(job, actor)

    def _snapshot_image_cost(
        self, candidate: FirstFrameCandidate | StyleFrameCandidate
    ) -> FirstFrameCandidate | StyleFrameCandidate:
        if (
            self.image_provider.name == "volcengine-seedream"
            and self.seedream_price_per_image > 0
        ):
            candidate.estimated_cost_cny = round(
                self.seedream_price_per_image, 8
            )
            candidate.pricing_basis = (
                f"Seedream 按量刊例价 ¥{self.seedream_price_per_image:g}/张；"
                "套餐、折扣与火山方舟账单优先"
            )
        return candidate

    def _snapshot_video_cost(self, task: ProviderTask) -> ProviderTask:
        if task.provider != "volcengine-seedance":
            return task
        rate = task.pricing_rate_cny_per_million
        if rate is None:
            rate = self.seedance_model_prices_per_million_tokens.get(
                task.model_id,
                self.seedance_price_per_million_tokens,
            )
            if rate > 0:
                task.pricing_rate_cny_per_million = rate
        if rate is not None and rate > 0:
            model_label = {
                SEEDANCE_EFFICIENT_MODEL: "Seedance 1.0 Pro Fast",
                SEEDANCE_BALANCED_MODEL: "Seedance 1.5 Pro",
                SEEDANCE_FLAGSHIP_MODEL: "Seedance 2.0",
            }.get(task.model_id, "Seedance")
            billing_mode = (
                "无声视频"
                if task.model_id in {
                    SEEDANCE_EFFICIENT_MODEL,
                    SEEDANCE_BALANCED_MODEL,
                }
                else "无视频输入"
            )
            task.pricing_basis = (
                f"{model_label} {billing_mode}按量刊例价 "
                f"¥{rate:g}/百万 tokens；"
                "套餐、折扣与火山方舟账单优先"
            )
        if task.usage_total_tokens > 0 and rate is not None and rate > 0:
            task.estimated_cost_cny = round(
                task.usage_total_tokens
                * rate
                / 1_000_000,
                8,
            )
        return task

    @staticmethod
    def _validate_verified_card(card: SourceCard):
        if card.risk_level.value == "high":
            raise QualityGateFailed("当前工作流不处理高风险或危机主题")
        research_first_formats = {
            ContentFormat.PERSON_IDEA,
            ContentFormat.RECENT_NEWS,
        }
        if card.content_format not in research_first_formats:
            if not card.sources:
                raise QualityGateFailed("来源卡至少需要一条真实来源才能核验")
            if not (card.verified_facts or card.verified_quotes):
                raise QualityGateFailed("来源卡至少需要一条可引用事实或引文才能核验")
        serialized = json.dumps(card.model_dump(mode="json"), ensure_ascii=False)
        placeholder = next((item for item in FORBIDDEN_PLACEHOLDERS if item in serialized), "")
        if placeholder:
            raise QualityGateFailed(f"来源卡仍包含占位内容：{placeholder}")
        if any(item.publisher == "出版社" or item.edition == "版本信息" for item in card.sources):
            raise QualityGateFailed("来源卡仍包含出版社或版本占位内容")
        incomplete_books = [
            item.id
            for item in card.sources
            if item.type == "book"
            and not all((item.author, item.publisher, item.edition, item.locator))
        ]
        if incomplete_books:
            raise QualityGateFailed(
                "书籍来源必须填写作者、出版社、版本和页码/章节："
                + "、".join(incomplete_books)
            )
        missing_access_dates = [
            item.id for item in card.sources if item.url and not item.accessed_at
        ]
        if missing_access_dates:
            raise QualityGateFailed(
                "网络来源必须填写访问日期：" + "、".join(missing_access_dates)
            )
        unknown_rights = [item.id for item in card.sources if item.rights_status == "unknown"]
        if unknown_rights:
            raise QualityGateFailed("来源权利状态未确认：" + "、".join(unknown_rights))

    @staticmethod
    def _validate_generated_script_length(script: ScriptDraft):
        char_count = narration_char_count(script.narration_text())
        if char_count > SCRIPT_HARD_MAX_CHARS:
            raise QualityGateFailed(
                f"脚本口播共 {char_count} 字，超过技术安全上限 "
                f"{SCRIPT_HARD_MAX_CHARS} 字；本次结果未进入人工审核"
            )

    @staticmethod
    def _card_with_person_research(
        card: SourceCard,
        brief: PersonResearchBrief | NewsResearchBrief,
    ) -> SourceCard:
        enriched = card.model_copy(deep=True)
        used_source_ids = {item.id for item in enriched.sources}
        used_fact_ids = {item.id for item in enriched.verified_facts}
        source_by_url = {
            item.url.rstrip("/"): item.id
            for item in enriched.sources
            if item.url
        }
        existing_claims = {item.text for item in enriched.verified_facts}
        evidence_source_ids: list[tuple[object, str]] = []

        def next_id(prefix: str, used: set[str]) -> str:
            index = 1
            while f"{prefix}_{index:02d}" in used:
                index += 1
            value = f"{prefix}_{index:02d}"
            used.add(value)
            return value

        for evidence in brief.evidence:
            normalized_url = evidence.source_url.rstrip("/")
            source_id = source_by_url.get(normalized_url)
            if not source_id:
                source_id = next_id("research_source", used_source_ids)
                enriched.sources.append(SourceEntry(
                    id=source_id,
                    type=(
                        "official"
                        if evidence.source_kind == "official"
                        else "article"
                    ),
                    title=evidence.source_title,
                    url=evidence.source_url,
                    accessed_at=brief.generated_at[:10] or timestamp()[:10],
                    rights_status="verified_for_citation",
                ))
                source_by_url[normalized_url] = source_id
            evidence_source_ids.append((evidence, source_id))
            if evidence.claim in existing_claims:
                continue
            enriched.verified_facts.append(VerifiedFact(
                id=next_id("research_fact", used_fact_ids),
                text=evidence.claim,
                source_refs=[source_id],
            ))
            existing_claims.add(evidence.claim)

        if isinstance(brief, NewsResearchBrief):
            # The original news topic is a query, not evidence. Remove its
            # placeholder only after cited research facts have been appended.
            enriched.verified_facts = [
                item
                for item in enriched.verified_facts
                if item.id != "request_context_01"
            ]
            referenced_source_ids = {
                source_id
                for fact in enriched.verified_facts
                for source_id in fact.source_refs
            }
            enriched.sources = [
                item
                for item in enriched.sources
                if (
                    item.id != "request_source_01"
                    or item.id in referenced_source_ids
                )
            ]
            return SourceCard.model_validate(enriched)

        if enriched.subject.type == "topic" and brief.person_name.strip():
            enriched.subject = enriched.subject.model_copy(update={
                "type": "person",
                "name": brief.person_name.strip(),
            })

        attribution_source_id = next(
            (
                source_id
                for evidence, source_id in evidence_source_ids
                if getattr(evidence, "evidence_type", "") == "attribution"
            ),
            "",
        )
        if (
            brief.attribution_status == "verified"
            and brief.verified_wording.strip()
            and attribution_source_id
            and not any(
                item.text == brief.verified_wording.strip()
                for item in enriched.verified_quotes
            )
        ):
            used_quote_ids = {item.id for item in enriched.verified_quotes}
            enriched.verified_quotes.append(VerifiedQuote(
                id=next_id("research_quote", used_quote_ids),
                text=brief.verified_wording.strip(),
                source_id=attribution_source_id,
            ))

        boundary_candidates = {
            (
                "只围绕用户输入的观点展开，不补造人物经历、逐字引语、"
                "研究数据或来源出处。"
            ),
            (
                "输入中的人物归属、逐字表述、出处与语境在联网核验前均视为待确认；"
                "可以解释观点本身，但不得把它写成人物原话或历史事实。"
            ),
            (
                "只围绕用户输入的观点和自动研究中有来源支持的事实展开；不得补造人物经历、"
                "逐字引语、研究数据或来源出处，也不得把用户观点、研究摘要或编辑角度写成人物"
                "原话。资料存在冲突或不确定时，必须保留限定语。"
            ),
            (
                "只有来源卡中的 verified_quote 可以写成人物逐字原话；其他用户输入、研究摘要"
                "和编辑解释只能按其证据强度转述。必须先保留出处与原始语境，再做现实应用。"
            ),
        }
        boundary_text = (
            "只有来源卡中的 verified_quote 可以写成人物逐字原话；其他用户输入、研究摘要"
            "和编辑解释只能按其证据强度转述。必须先保留出处与原始语境，再做现实应用。"
            if brief.attribution_status == "verified"
            and brief.verified_wording.strip()
            and attribution_source_id
            else (
                "只围绕用户原始输入和自动研究中有来源支持的事实展开；当前表述不得写成人物"
                "逐字原话。不得补造人物经历、著作内容、数据、出处或因果关系；资料存在冲突"
                "或不确定时，必须保留限定语。"
            )
        )
        for index, boundary in enumerate(enriched.interpretation_boundary):
            if boundary.text in boundary_candidates or boundary.text.startswith(
                "只围绕用户原始输入和自动研究中有来源支持的事实展开"
            ):
                enriched.interpretation_boundary[index] = boundary.model_copy(
                    update={"text": boundary_text}
                )
        if not any(
            item.text == boundary_text for item in enriched.interpretation_boundary
        ):
            used_boundary_ids = {
                item.id for item in enriched.interpretation_boundary
            }
            enriched.interpretation_boundary.append(InterpretationBoundary(
                id=next_id("research_boundary", used_boundary_ids),
                text=boundary_text,
            ))
        return SourceCard.model_validate(enriched)

    @staticmethod
    def _source_card_from_input_snapshot(
        snapshot: CreativeInputSnapshot,
        *,
        card_id: str = 'direct-input',
        created_by: str = '',
    ) -> SourceCard:
        """Build a neutral validation adapter; it is never persisted as intake."""

        sources = []
        facts = []
        for index, material in enumerate(snapshot.verified_materials, start=1):
            source_id = f'source_{index:02d}'
            material_id = f'material_{index:02d}'
            sources.append(SourceEntry(
                id=source_id,
                type='other',
                title=material.title,
                locator='' if material.url else '用户在创建任务时确认的材料',
                url=material.url,
                rights_status='verified_for_citation',
            ))
            facts.append(VerifiedFact(
                id=material_id,
                text=material.text,
                source_refs=[source_id],
            ))
        now = snapshot.created_at or timestamp()
        return SourceCard(
            content_domain=ContentDomain.GENERAL_KNOWLEDGE,
            content_format=ContentFormat.CONCEPT,
            target_audience='由用户原始创作请求决定',
            subject={'type': 'topic', 'name': snapshot.display_title[:300]},
            title=snapshot.display_title,
            core_idea=snapshot.original_request,
            parent_question=snapshot.original_request[:500],
            sources=sources,
            verified_facts=facts,
            interpretation_boundary=[],
            reference_assets=list(snapshot.reference_assets),
            id=card_id,
            revision=1,
            status=SourceCardStatus.VERIFIED,
            reviewed_by=created_by,
            reviewed_at=now,
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _source_card_for_job(job: VideoJob) -> SourceCard:
        if job.input_snapshot:
            return QijiaVideoService._source_card_from_input_snapshot(
                job.input_snapshot,
                card_id=job.id,
                created_by=job.created_by,
            )
        return SourceCard.model_validate(job.source_card_snapshot)

    @staticmethod
    def _direct_script_prompt_for_settings(
        input_snapshot: CreativeInputSnapshot,
        settings: GenerationSettings,
        prompt_adapter: PromptAdapterSnapshot,
        content_policy: ContentSkillSnapshot,
        script_skill: ScriptSkillSnapshot,
    ) -> str:
        minimum, maximum = TTS_SCRIPT_CHARACTER_TARGETS[
            settings.tts_speed_ratio
        ]
        return compile_direct_script_prompt(
            input_snapshot,
            prompt_adapter=prompt_adapter,
            content_policy=content_policy,
            script_skill=script_skill,
            minimum_characters=minimum,
            maximum_characters=maximum,
        )

    @staticmethod
    def _quality_script_prompt_for_settings(
        input_snapshot: CreativeInputSnapshot,
        settings: GenerationSettings,
        script_skill: ScriptSkillSnapshot,
    ) -> str:
        minimum, maximum = TTS_SCRIPT_CHARACTER_TARGETS[
            settings.tts_speed_ratio
        ]
        return compile_quality_script_prompt(
            input_snapshot,
            script_skill=script_skill,
            minimum_characters=minimum,
            maximum_characters=maximum,
        )

    @staticmethod
    def _legacy_h3_script_prompt_for_settings(
        card: SourceCard,
        settings: GenerationSettings,
        profile: PromptWritingProfileSnapshot | None,
        research_brief: PersonResearchBrief | NewsResearchBrief | None = None,
    ) -> str:
        minimum, maximum = TTS_SCRIPT_CHARACTER_TARGETS[
            settings.tts_speed_ratio
        ]
        return compile_legacy_h3_script_prompt(
            card,
            profile=profile,
            research_brief=research_brief,
            minimum_characters=minimum,
            maximum_characters=maximum,
        )

    @staticmethod
    def _script_skill_prompt_for_settings(
        card: SourceCard,
        settings: GenerationSettings,
        content_policy: ContentSkillSnapshot,
        script_skill: ScriptSkillSnapshot,
        research_brief: PersonResearchBrief | NewsResearchBrief | None = None,
    ) -> str:
        minimum, maximum = TTS_SCRIPT_CHARACTER_TARGETS[
            settings.tts_speed_ratio
        ]
        return compile_script_skill_prompt(
            card,
            content_policy=content_policy,
            script_skill=script_skill,
            research_brief=research_brief,
            minimum_characters=minimum,
            maximum_characters=maximum,
        )

    @staticmethod
    def _legacy_fallback_creative_brief(
        card: SourceCard,
        research_brief: PersonResearchBrief | NewsResearchBrief | None,
    ) -> CreativeBrief:
        """Compatibility brief for deterministic or legacy ScriptProviders."""

        evidence_refs = [
            item.id for item in (*card.verified_facts, *card.verified_quotes)
        ]
        uncertainties = (
            list(research_brief.uncertainties) if research_brief else []
        )
        thesis = (
            research_brief.summary
            if research_brief and research_brief.summary
            else card.core_idea
        )
        payload = {
            "central_question": card.parent_question,
            "core_thesis": thesis,
            "audience_promise": (
                f"帮助{card.target_audience}准确理解这项命题的依据、含义与边界。"
            ),
            "narrative_arc": [
                "直接呈现原始命题中最值得辨析的判断",
                "交代理解它所必需的出处、事实或语境",
                "沿一条连续论证路径解释核心含义",
                "回到中心问题并给出克制结论",
            ],
            "tone": "准确、克制、具体、有思考感，不说教、不制造焦虑",
            "visual_concept": (
                "围绕主题对象、关键关系和状态变化建立一条连续视觉母题，"
                "不伪造史料、人物肖像、界面或抽象文字证据。"
            ),
            "continuity_anchors": [card.subject.name],
            "must_include": [
                item.text for item in card.interpretation_boundary[:6]
            ],
            "must_avoid": uncertainties[:6],
            "evidence_refs": evidence_refs,
            "model_id": "deterministic-compatibility",
            "prompt_version": "h3_creative_brief_compat_v1",
            "input_hash": content_hash({
                "card": card.model_dump(mode="json"),
                "research": (
                    research_brief.model_dump(mode="json")
                    if research_brief else None
                ),
            }),
            "generated_at": timestamp(),
        }
        return CreativeBrief.model_validate(payload)

    @staticmethod
    def _fallback_editorial_plan(
        card: SourceCard,
        research_brief: PersonResearchBrief | NewsResearchBrief | None,
    ) -> EditorialPlan:
        evidence_refs = [
            item.id for item in (*card.verified_facts, *card.verified_quotes)
        ]
        summary = (
            research_brief.summary
            if research_brief and research_brief.summary
            else card.core_idea
        )
        return EditorialPlan.model_validate({
            'objective': f'准确回答：{card.parent_question}',
            'central_question': card.parent_question,
            'candidate_angles': [
                {
                    'angle_id': 'evidence_context',
                    'premise': '从证据、出处与语境解释用户提出的命题。',
                    'audience_value': '区分可核验事实、合理解释与未知部分。',
                    'evidence_refs': evidence_refs,
                    'risk': '证据不足时必须明确降级。',
                },
                {
                    'angle_id': 'meaning_limits',
                    'premise': '从命题的成立条件与边界解释其含义。',
                    'audience_value': '避免把复杂观点简化成口号。',
                    'evidence_refs': evidence_refs,
                    'risk': '不能脱离原始语境强行应用。',
                },
            ],
            'selected_angle_id': 'evidence_context',
            'selection_reason': '该角度与现有证据的匹配度最高。',
            'core_thesis': summary,
            'audience_promise': f'帮助{card.target_audience}理解命题的依据与边界。',
            'narrative_arc': [
                '直接呈现用户命题中的关键判断',
                '交代理解它所必需的证据与语境',
                '解释核心含义与成立边界',
                '回到中心问题给出克制结论',
            ],
            'tone': '准确、克制、具体、有思考感',
            'must_include': [item.text for item in card.interpretation_boundary[:6]],
            'must_avoid': list(research_brief.uncertainties[:6]) if research_brief else [],
            'evidence_refs': evidence_refs,
            'critic_summary': '兼容 Provider 未返回 EditorialPlan；已生成确定性内容边界。',
            'model_id': 'deterministic-compatibility',
            'prompt_version': 'editorial_plan_compat_v1',
            'input_hash': content_hash({
                'card': card.model_dump(mode='json'),
                'research': research_brief.model_dump(mode='json') if research_brief else None,
            }),
            'generated_at': timestamp(),
        })

    @staticmethod
    def _validate_script(script: ScriptDraft, card: SourceCard):
        if script.source_card_id != card.id or script.source_card_revision != card.revision:
            raise QualityGateFailed("脚本没有绑定当前来源卡版本")
        char_count = narration_char_count(script.narration_text())
        if char_count > SCRIPT_HARD_MAX_CHARS:
            raise QualityGateFailed(
                f"纯旁白共 {char_count} 字，超过技术安全上限 {SCRIPT_HARD_MAX_CHARS} 字；"
                "画面说明和屏幕文字不应写入旁白轨"
            )
        allowed_refs = {
            item.id for item in card.verified_facts
        } | {item.id for item in card.verified_quotes}
        for segment in script.beats:
            unknown = set(segment.source_refs) - allowed_refs
            if unknown:
                raise QualityGateFailed(
                    f"脚本段落 {segment.id} 引用了未知事实：{sorted(unknown)}"
                )
            if segment.quote_ref:
                quote = next(
                    (item for item in card.verified_quotes if item.id == segment.quote_ref),
                    None,
                )
                if not quote or quote.id not in segment.source_refs:
                    raise QualityGateFailed(
                        f"脚本段落 {segment.id} 的直接引文引用无效"
                    )
                if quote.text not in segment.text:
                    raise QualityGateFailed(
                        f"脚本段落 {segment.id} 没有逐字保留已核验引文"
                    )
            for known_quote in card.verified_quotes:
                if (
                    known_quote.text in segment.text
                    and segment.quote_ref != known_quote.id
                ):
                    raise QualityGateFailed(
                        f"脚本段落 {segment.id} 使用了直接引文但未绑定 quote_ref"
                    )
        text = "\n".join((
            script.video_title,
            script.cover_text,
            script.hook,
            script.narration_text(),
            script.closing,
            script.caption,
            " ".join(script.hashtags),
        ))
        brand = next((item for item in FORBIDDEN_BRAND_TEXT if item in text), "")
        if brand and card.content_format != ContentFormat.RECENT_NEWS:
            raise QualityGateFailed("初期知识视频不得加入齐家 AI 品牌或产品引导")

    @staticmethod
    def _validate_editorial_plan(plan: EditorialPlan, card: SourceCard):
        allowed_refs = {
            item.id for item in (*card.verified_facts, *card.verified_quotes)
        }
        used_refs = set(plan.evidence_refs)
        for angle in plan.candidate_angles:
            used_refs.update(angle.evidence_refs)
        unknown = sorted(used_refs - allowed_refs)
        if unknown:
            raise QualityGateFailed(f'EditorialPlan 引用了未知证据：{unknown}')

    def _apply_reviewed_script(
        self,
        job: VideoJob,
        script: ScriptDraft,
        review: ScriptReview,
        *,
        allow_visual_reuse: bool,
    ) -> bool:
        """Replace the script and retain paid visuals when its structure is stable."""

        job.script = script
        job.script_hash = content_hash(script)
        job.script_review = review
        target_groups = (
            self._storyboard_expected_groups(job)
            if job.storyboard_plan
            else []
        )
        preserve_paid_visuals = bool(
            allow_visual_reuse
            and job.storyboard_plan
            and job.visual_requests
            and job.video_tasks
            and [item.beat_ids for item in job.storyboard_plan.shots]
            == target_groups
        )
        job.approvals = [item for item in job.approvals if item.kind != "script"]
        job.narration_manifest = None
        if preserve_paid_visuals:
            segments_by_id = {
                item.id: item for item in job.script.beats
            }
            for shot in job.storyboard_plan.shots:
                shot.narration_excerpt = "\n".join(
                    segments_by_id[beat_id].text for beat_id in shot.beat_ids
                )
            rebound_hash = self._storyboard_input_hash(job)
            job.storyboard_plan.input_hash = rebound_hash
            if job.director_treatment:
                job.director_treatment.input_hash = rebound_hash
            if job.asset_bible:
                job.asset_bible.input_hash = rebound_hash
            if job.visual_bible:
                job.visual_bible.input_hash = rebound_hash
        else:
            job.storyboard_plan = None
            job.director_treatment = None
            job.asset_bible = None
            job.visual_bible = None
            job.first_frame_candidates = []
            job.frame_selections = []
            job.frame_selection_warning = ""
            job.visual_requests = []
            job.video_tasks = []
            job.visual_versions = []
            job.render_manifest = None
        job.quality_report = None
        job.artifacts = []
        job.review_bundle_hash = ""
        return preserve_paid_visuals

    async def create_source_card(
        self, payload: SourceCardInput, actor: Actor
    ) -> SourceCard:
        now = timestamp()
        draft = SourceCard(
            **payload.model_dump(mode="json"),
            id="pending",
            status=SourceCardStatus.DRAFT,
            created_by=actor.username,
            created_at=now,
            updated_at=now,
        )
        saved = await self.repository.create(
            "source_card", f"齐家短视频来源卡：{payload.title}", actor,
            draft.model_dump(mode="json"),
        )
        return SourceCard.model_validate(saved)

    async def list_source_cards(self, actor: Actor, *, limit: int = 100) -> list[SourceCard]:
        return [
            SourceCard.model_validate(item)
            for item in await self.repository.list_visible(
                "source_card", actor, limit=limit
            )
        ]

    async def get_source_card(self, card_id: str, actor: Actor) -> SourceCard:
        return SourceCard.model_validate(
            await self.repository.get("source_card", card_id, actor)
        )

    async def view_source_card(self, card_id: str, actor: Actor) -> SourceCard:
        return SourceCard.model_validate(
            await self.repository.get_visible("source_card", card_id, actor)
        )

    async def update_source_card(
        self,
        card_id: str,
        payload: SourceCardInput,
        expected_revision: int,
        actor: Actor,
    ) -> SourceCard:
        card = await self.get_source_card(card_id, actor)
        self._assert_revision(card.revision, expected_revision)
        if card.status != SourceCardStatus.DRAFT:
            raise InvalidTransition("已核验来源卡不可修改，请创建新版本")
        updated = SourceCard(
            **payload.model_dump(mode="json"),
            id=card.id,
            revision=card.revision + 1,
            status=SourceCardStatus.DRAFT,
            created_by=card.created_by,
            created_at=card.created_at,
            updated_at=timestamp(),
        )
        saved = await self.repository.replace(
            "source_card",
            card.id,
            actor,
            updated.model_dump(mode="json"),
            expected_revision=card.revision,
        )
        return SourceCard.model_validate(saved)

    async def verify_source_card(
        self, card_id: str, expected_revision: int, actor: Actor
    ) -> SourceCard:
        card = await self.get_source_card(card_id, actor)
        self._assert_revision(card.revision, expected_revision)
        self._validate_verified_card(card)
        if card.status == SourceCardStatus.VERIFIED:
            return card
        current = card.revision
        card.status = SourceCardStatus.VERIFIED
        card.reviewed_by = actor.username
        card.reviewed_at = timestamp()
        card.updated_at = card.reviewed_at
        card.revision += 1
        saved = await self.repository.replace(
            "source_card", card.id, actor, card.model_dump(mode="json"),
            expected_revision=current,
        )
        return SourceCard.model_validate(saved)

    async def create_job(
        self,
        source_card_id: str,
        actor: Actor,
        generation_settings: GenerationSettings | None = None,
    ) -> VideoJob:
        """Compatibility entry for persisted v2 SourceCard workflows."""

        card = await self.get_source_card(source_card_id, actor)
        if card.status != SourceCardStatus.VERIFIED:
            raise QualityGateFailed("来源卡必须先核验")
        if card.content_format == ContentFormat.RECENT_NEWS:
            raise QualityGateFailed(
                "最新新闻依赖实时检索，当前工作台已停用该入口。"
                "请在统一创作请求中粘贴你已经掌握的事实与材料。"
            )
        # This method is retained for CLI and historical SourceCard callers.
        # The production Web intake uses create_direct_job and enters v4.
        original_request = card.core_idea.strip() or card.title
        snapshot = CreativeInputSnapshot(
            original_request=original_request,
            display_title=card.title,
            verified_materials=[
                CreativeMaterial(
                    title=next(
                        (
                            source.title
                            for source in card.sources
                            if source.id in fact.source_refs
                        ),
                        '用户核对材料',
                    ),
                    text=fact.text,
                    url=next(
                        (
                            source.url
                            for source in card.sources
                            if source.id in fact.source_refs and source.url
                        ),
                        '',
                    ),
                )
                for fact in card.verified_facts
            ],
            reference_assets=list(card.reference_assets),
            created_at=timestamp(),
        )
        return await self._create_frozen_job(
            self._source_card_from_input_snapshot(
                snapshot,
                created_by=actor.username,
            ),
            actor,
            generation_settings,
            pipeline_version=PipelineVersion.DIRECT_SCRIPT,
            input_snapshot=snapshot,
        )

    async def create_direct_job(
        self,
        creative_request: str,
        actor: Actor,
        generation_settings: GenerationSettings | None = None,
        *,
        verified_materials: list[CreativeMaterial] | None = None,
        reference_assets: list[dict] | None = None,
    ) -> VideoJob:
        """Create one v4 job atomically from the creator's original request."""

        request = str(creative_request or '').strip()
        if len(request) < 10:
            raise QualityGateFailed('请用至少 10 个字说明你想做的内容')
        now = timestamp()
        snapshot = CreativeInputSnapshot(
            original_request=request,
            display_title=request[:300],
            verified_materials=list(verified_materials or []),
            reference_assets=list(reference_assets or []),
            created_at=now,
        )
        card = self._source_card_from_input_snapshot(
            snapshot,
            created_by=actor.username,
        )
        return await self._create_frozen_job(
            card,
            actor,
            generation_settings,
            pipeline_version=PipelineVersion.QUALITY_FIRST,
            input_snapshot=snapshot,
        )

    async def _create_frozen_job(
        self,
        card: SourceCard,
        actor: Actor,
        generation_settings: GenerationSettings | None,
        *,
        pipeline_version: PipelineVersion,
        input_snapshot: CreativeInputSnapshot | None = None,
    ) -> VideoJob:
        requested_settings = generation_settings or GenerationSettings()
        script_prompt_explicit = (
            "script_prompt" in requested_settings.model_fields_set
        )
        seedance_prompt_explicit = (
            "seedance_prompt" in requested_settings.model_fields_set
        )
        legacy_profile_explicit = bool(
            requested_settings.prompt_writing_profile_id
            or requested_settings.prompt_writing_profile_version
        )
        fixed_chapter_count_requested = bool(
            requested_settings.image_count or requested_settings.shot_count
        )
        try:
            if pipeline_version == PipelineVersion.QUALITY_FIRST:
                if requested_settings.skill_id or requested_settings.skill_version:
                    raise SkillRegistryError(
                        'v4 脚本入口不再接受 Content Skill；用户原始输入将直达脚本主编'
                    )
                frozen_settings = requested_settings.model_copy(deep=True)
                skill_snapshot = None
                prompt_adapter_snapshot = None
            else:
                frozen_settings, skill_snapshot = self.skill_registry.freeze(
                    card,
                    requested_settings,
                )
                prompt_adapter_snapshot = (
                    self.prompt_adapter_registry.freeze_default()
                )
            frozen_settings, script_skill_snapshot = (
                self.script_skill_registry.freeze(card, frozen_settings)
            )
            frozen_settings, director_skill_snapshot = (
                self.director_skill_registry.freeze(card, frozen_settings)
            )
            frozen_settings, visual_style_snapshot = (
                self.visual_style_registry.freeze(frozen_settings)
            )
            frozen_settings, provider_adapter_snapshot = (
                self.provider_adapter_registry.freeze(frozen_settings)
            )
        except (
            ProviderAdapterRegistryError,
            DirectorSkillRegistryError,
            ScriptSkillRegistryError,
            SkillRegistryError,
            VisualStyleRegistryError,
        ) as exc:
            raise QualityGateFailed(str(exc)) from exc
        if seedance_prompt_explicit:
            raise QualityGateFailed(
                'Director Skill 与 Provider Adapter 已接管视觉生成；'
                '请选择视觉导演，不要再提交 seedance_prompt'
            )
        if script_prompt_explicit:
            raise QualityGateFailed(
                'Script Skill 已接管脚本方法；不要再提交 script_prompt'
            )
        if legacy_profile_explicit:
            raise QualityGateFailed(
                'Prompt Writing Profile 仅用于 Pipeline v1 历史任务；'
                '新任务请选择 Script Skill、Director Skill 与 Provider Adapter'
            )
        if fixed_chapter_count_requested:
            raise QualityGateFailed(
                'Director Skill 会按真实语义变化规划视觉章节；'
                '不要再提交 image_count 或 shot_count'
            )
        # Do not persist dormant v1 prompt text in a new job. Historical jobs
        # still deserialize with the contract defaults and keep their snapshots.
        frozen_settings.script_prompt = ''
        frozen_settings.seedance_prompt = ''
        if pipeline_version == PipelineVersion.QUALITY_FIRST:
            frozen_settings.skill_id = ''
            frozen_settings.skill_version = ''
        frozen_settings.prompt_writing_profile_id = ''
        frozen_settings.prompt_writing_profile_version = ''
        now = timestamp()
        draft = VideoJob(
            pipeline_version=pipeline_version,
            id="pending",
            state=JobState.CARD_VERIFIED,
            source_card_id=card.id if input_snapshot is None else '',
            source_card_revision=card.revision,
            source_card_snapshot=(
                card.model_dump(mode="json") if input_snapshot is None else {}
            ),
            input_snapshot=input_snapshot,
            skill_snapshot=skill_snapshot,
            script_skill_snapshot=script_skill_snapshot,
            director_skill_snapshot=director_skill_snapshot,
            visual_style_snapshot=visual_style_snapshot,
            provider_adapter_snapshot=provider_adapter_snapshot,
            prompt_adapter_snapshot=prompt_adapter_snapshot,
            generation_settings=frozen_settings,
            created_by=actor.username,
            created_at=now,
            updated_at=now,
        )
        saved = await self.repository.create(
            "job", f"AI 短视频：{card.title}", actor,
            draft.model_dump(mode="json"),
        )
        return VideoJob.model_validate(saved)

    def visual_styles(self) -> list[dict]:
        """Visual language catalog, independent from the directing method."""

        return self.visual_style_registry.public_catalog()

    async def list_jobs(
        self,
        actor: Actor,
        *,
        limit: int = 100,
        include_deleted: bool = False,
    ) -> list[VideoJob]:
        rows = [
            VideoJob.model_validate(item)
            for item in await self.repository.list_visible(
                "job", actor, limit=limit if include_deleted else 500
            )
        ]
        if include_deleted:
            return rows[:limit]
        return [item for item in rows if not item.deleted_at][:limit]

    async def get_job(self, job_id: str, actor: Actor) -> VideoJob:
        job = VideoJob.model_validate(
            await self.repository.get("job", job_id, actor)
        )
        if job.deleted_at:
            raise ResourceNotFound("视频任务不存在")
        return job

    async def view_job(self, job_id: str, actor: Actor) -> VideoJob:
        job = VideoJob.model_validate(
            await self.repository.get_visible("job", job_id, actor)
        )
        if job.deleted_at:
            raise ResourceNotFound("视频任务不存在")
        return job

    async def delete_job(
        self,
        job_id: str,
        expected_revision: int,
        actor: Actor,
        *,
        active_run: bool = False,
    ) -> VideoJob:
        """Hide a job without erasing paid usage or generated assets."""

        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if active_run:
            raise InvalidTransition("任务正在处理中，请等待本次运行完成后再删除")
        job.deleted_at = timestamp()
        job.deleted_by = actor.username
        return await self._save_job(job, actor)

    async def _save_job(self, job: VideoJob, actor: Actor) -> VideoJob:
        current = job.revision
        job.revision += 1
        job.updated_at = timestamp()
        saved = await self.repository.replace(
            "job", job.id, actor, job.model_dump(mode="json"),
            expected_revision=current,
        )
        return VideoJob.model_validate(saved)

    def douyin_performance_analysis(self, job: VideoJob) -> dict:
        return build_douyin_performance_analysis(
            job,
            seedream_price_per_image=self.seedream_price_per_image,
            seedance_price_per_million_tokens=(
                self.seedance_price_per_million_tokens
            ),
            seedance_model_prices_per_million_tokens=(
                self.seedance_model_prices_per_million_tokens
            ),
            tts_price_per_10000_characters=(
                self.tts_price_per_10000_characters
            ),
        )

    def _require_douyin_performance_provider(
        self,
    ) -> DouyinPerformanceProvider:
        provider = self.douyin_performance_provider
        if provider is None:
            raise ProviderUnavailable("抖音效果回流 Provider 未装配")
        if not provider.configured:
            raise ProviderUnavailable(
                "抖音效果回流未配置："
                + "、".join(provider.configuration_errors)
            )
        return provider

    async def _collect_douyin_performance(
        self,
        job_id: str,
        expected_revision: int,
        actor: Actor,
        *,
        share_text: str | None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.PACKAGED:
            raise InvalidTransition("只有已完成发布包的视频才能回流抖音播放量")
        provider = self._require_douyin_performance_provider()
        current_performance = job.douyin_performance
        if share_text is None and current_performance is None:
            raise InvalidTransition("请先绑定这条视频发布后的抖音作品链接")

        async def persist_usage(usage: ProviderUsageRecord) -> None:
            nonlocal job
            job = await self._persist_usage_record(job, usage, actor)

        if share_text is None:
            metrics = await provider.fetch_by_video_id(
                current_performance.video_id,
                on_usage=persist_usage,
            )
        else:
            metrics = await provider.fetch_by_share_url(
                share_text,
                on_usage=persist_usage,
            )

        now = timestamp()
        previous = (
            job.douyin_performance
            if (
                job.douyin_performance
                and job.douyin_performance.video_id == metrics.video_id
            )
            else None
        )
        previous_latest = (
            previous.snapshots[-1]
            if previous and previous.snapshots
            else None
        )
        if (
            previous_latest
            and metrics.play_count < previous_latest.play_count
        ):
            raise ProviderUnavailable(
                "TikHub 返回的累计播放量低于已有快照；本次结果未保存，"
                "旧数据已保留"
            )
        snapshots = [
            *(previous.snapshots if previous else []),
            DouyinPlaybackSnapshot(
                play_count=metrics.play_count,
                like_count=metrics.like_count,
                comment_count=metrics.comment_count,
                share_count=metrics.share_count,
                collect_count=metrics.collect_count,
                observed_at=now,
                request_id=metrics.request_id,
            ),
        ][-DOUYIN_SNAPSHOT_RETENTION:]
        job.douyin_performance = DouyinPerformance(
            video_id=metrics.video_id,
            video_url=metrics.video_url,
            video_title=(
                metrics.video_title
                or (previous.video_title if previous else "")
            ),
            author_name=(
                metrics.author_name
                or (previous.author_name if previous else "")
            ),
            bound_at=previous.bound_at if previous else now,
            updated_at=now,
            snapshots=snapshots,
        )
        return await self._save_job(job, actor)

    async def bind_douyin_performance(
        self,
        job_id: str,
        douyin_url: str,
        expected_revision: int,
        actor: Actor,
    ) -> VideoJob:
        async with self._douyin_performance_lock:
            return await self._collect_douyin_performance(
                job_id,
                expected_revision,
                actor,
                share_text=douyin_url,
            )

    async def refresh_douyin_performance(
        self,
        job_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> VideoJob:
        async with self._douyin_performance_lock:
            return await self._collect_douyin_performance(
                job_id,
                expected_revision,
                actor,
                share_text=None,
            )

    async def set_last_run_task(
        self, job_id: str, run_task_id: str, actor: Actor
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        job.last_run_task_id = run_task_id
        return await self._save_job(job, actor)

    async def mark_execution_failed(
        self, job_id: str, stage: str, error: str, actor: Actor
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        job.state = JobState.FAILED
        job.failed_stage = stage
        job.error = str(error or "后台 Worker 启动失败")[:2000]
        return await self._save_job(job, actor)

    async def generate_script(
        self,
        job_id: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        if job.state not in (
            JobState.CARD_VERIFIED, JobState.SCRIPT_GENERATING, JobState.FAILED
        ):
            raise InvalidTransition(f"当前状态不能生成脚本：{job.state.value}")
        if job.state == JobState.FAILED and job.failed_stage not in ("", "script"):
            raise InvalidTransition("该任务失败在生产阶段，请重试生产而不是重写脚本")
        job.state = JobState.SCRIPT_GENERATING
        job.failed_stage = ""
        job.error = ""
        job = await self._save_job(job, actor)
        try:
            card = self._source_card_for_job(job)
            settings = self._generation_settings(job)

            async def persist_script_usage(usage: ProviderUsageRecord) -> None:
                nonlocal job
                job = await self._persist_usage_record(job, usage, actor)

            self._report(
                progress,
                message="正在把你的原始输入直接交给脚本主编…",
                stage="material_confirmed",
                percent=4,
            )
            if job.research_brief:
                # Historical tasks may reuse an already persisted brief, but
                # script generation never starts a new external research call.
                card = self._card_with_person_research(
                    card, job.research_brief
                )
            elif (
                job.skill_snapshot
                and job.skill_snapshot.research_mode
                != SkillResearchMode.NONE
            ):
                raise QualityGateFailed(
                    "该任务由已停用的联网研究模式创建，系统不会继续发起搜索。"
                    "请复制原始创作请求创建一个新的模型知识任务。"
                )

            self._report(
                progress,
                message="正在由脚本模型理解输入并生成口播脚本…",
                stage="script_generation",
                percent=14,
            )
            is_v4 = job.pipeline_version == PipelineVersion.QUALITY_FIRST
            is_v3 = job.pipeline_version == PipelineVersion.DIRECT_SCRIPT
            is_v2 = job.pipeline_version == PipelineVersion.SINGLE_OWNER
            generate_with_usage = getattr(
                self.script_provider, 'generate_with_usage', None
            )
            if is_v4:
                if not job.input_snapshot or not job.script_skill_snapshot:
                    raise QualityGateFailed(
                        'v4 任务缺少冻结的原始输入或 Script Skill'
                    )
                if job.skill_snapshot or job.prompt_adapter_snapshot:
                    raise QualityGateFailed(
                        'v4 脚本入口不得包含 Content Policy、EvidencePolicy '
                        '或 H3 Script Adapter'
                    )
                script_prompt = self._quality_script_prompt_for_settings(
                    job.input_snapshot,
                    settings,
                    job.script_skill_snapshot,
                )
                generate_quality_script = getattr(
                    self.script_provider,
                    'generate_quality_script',
                    None,
                )
                if callable(generate_quality_script):
                    script, review = await generate_quality_script(
                        card,
                        script_prompt,
                        on_usage=persist_script_usage,
                    )
                else:
                    generate_direct_script = getattr(
                        self.script_provider,
                        'generate_direct_script',
                        None,
                    )
                    if callable(generate_direct_script):
                        script = await generate_direct_script(
                            card,
                            script_prompt,
                            on_usage=persist_script_usage,
                        )
                    else:
                        script = await self.script_provider.generate(
                            card,
                            script_prompt,
                        )
                    review = await self.script_provider.review(card, script)
                creative_brief = None
                editorial_plan = None
            elif is_v3:
                if (
                    not job.input_snapshot
                    or not job.skill_snapshot
                    or not job.script_skill_snapshot
                    or not job.prompt_adapter_snapshot
                ):
                    raise QualityGateFailed(
                        'v3 任务缺少冻结的原始输入、H3 Prompt Adapter、'
                        '知识边界或 Script Skill'
                    )
                script_prompt = self._direct_script_prompt_for_settings(
                    job.input_snapshot,
                    settings,
                    job.prompt_adapter_snapshot,
                    job.skill_snapshot,
                    job.script_skill_snapshot,
                )
                generate_direct_script = getattr(
                    self.script_provider, 'generate_direct_script', None
                )
                if callable(generate_direct_script):
                    script = await generate_direct_script(
                        card,
                        script_prompt,
                        on_usage=persist_script_usage,
                    )
                else:
                    script = await self.script_provider.generate(card, script_prompt)
                creative_brief = None
                editorial_plan = None
            elif is_v2:
                if not job.skill_snapshot or not job.script_skill_snapshot:
                    raise QualityGateFailed(
                        'v2 任务缺少冻结的 Input Policy 或 Script Skill'
                    )
                script_prompt = self._script_skill_prompt_for_settings(
                    card,
                    settings,
                    job.skill_snapshot,
                    job.script_skill_snapshot,
                    job.research_brief,
                )
                generate_with_plan = getattr(
                    self.script_provider, 'generate_with_plan', None
                )
                if callable(generate_with_plan):
                    editorial_plan, script = await generate_with_plan(
                        card,
                        script_prompt,
                        on_usage=persist_script_usage,
                    )
                elif callable(generate_with_usage):
                    script = await generate_with_usage(
                        card,
                        script_prompt,
                        on_usage=persist_script_usage,
                    )
                    editorial_plan = self._fallback_editorial_plan(
                        card, job.research_brief
                    )
                else:
                    script = await self.script_provider.generate(card, script_prompt)
                    editorial_plan = self._fallback_editorial_plan(
                        card, job.research_brief
                    )
                creative_brief = None
            else:
                script_prompt = self._legacy_h3_script_prompt_for_settings(
                    card,
                    settings,
                    job.prompt_writing_profile_snapshot,
                    job.research_brief,
                )
                generate_with_brief = getattr(
                    self.script_provider, 'generate_with_brief', None
                )
                if callable(generate_with_brief):
                    creative_brief, script = await generate_with_brief(
                        card,
                        script_prompt,
                        on_usage=persist_script_usage,
                    )
                elif callable(generate_with_usage):
                    script = await generate_with_usage(
                        card,
                        script_prompt,
                        on_usage=persist_script_usage,
                    )
                    creative_brief = self._legacy_fallback_creative_brief(
                        card, job.research_brief
                    )
                else:
                    script = await self.script_provider.generate(card, script_prompt)
                    creative_brief = self._legacy_fallback_creative_brief(
                        card, job.research_brief
                    )
                editorial_plan = None
            provider_script_hash = content_hash(script)
            if editorial_plan:
                self._validate_editorial_plan(editorial_plan, card)
            script.estimated_duration_seconds = max(
                45,
                min(
                    75,
                    round(
                        narration_char_count(script.narration_text())
                        / (4.1 * settings.tts_speed_ratio)
                    ),
                ),
            )
            self._validate_generated_script_length(script)
            self._validate_script(script, card)
            generated_script_hash = content_hash(script)
            if editorial_plan:
                editorial_plan.draft_script_hash = generated_script_hash
            self._report(
                progress,
                message="主编终稿已完成，正在校验必要结构…",
                stage="script_generation",
                percent=24,
            )
            if not is_v4:
                review = await self.script_provider.review(card, script)
            if not review.passed or review.blocking_reasons:
                raise QualityGateFailed("自动脚本审核未通过")
            if is_v4 and review.input_hash == provider_script_hash:
                # The service owns duration metadata because it knows the
                # frozen TTS speed. Rebind only after verifying the exact
                # provider-returned script that the critic/reviewer assessed.
                if (
                    review.reviewed_draft_hash
                    and review.reviewed_draft_hash != provider_script_hash
                ):
                    raise QualityGateFailed(
                        "终稿语义验收没有绑定 Provider 返回的最终脚本"
                    )
                review.input_hash = generated_script_hash
                if review.reviewed_draft_hash:
                    review.reviewed_draft_hash = generated_script_hash
            if review.input_hash != generated_script_hash:
                raise QualityGateFailed("脚本审核结果没有绑定当前脚本")
            job.script = script
            job.creative_brief = creative_brief
            job.editorial_plan = editorial_plan
            job.script_hash = generated_script_hash
            job.script_review = review
            job.approvals = []
            job.state = JobState.SCRIPT_REVIEW_REQUIRED
            job = await self._save_job(job, actor)
            self._report(
                progress,
                message="脚本已就绪，等待你确认…",
                stage="confirm_script",
                percent=28,
            )
            return job
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            latest.state = JobState.FAILED
            latest.failed_stage = "script"
            latest.error = str(exc)
            await self._save_job(latest, actor)
            raise

    async def update_script(
        self,
        job_id: str,
        script: ScriptDraft,
        expected_revision: int,
        actor: Actor,
        tts_voice_id: str | None = None,
        tts_speed_ratio: float | None = None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.SCRIPT_REVIEW_REQUIRED:
            raise InvalidTransition("只有待确认脚本可以编辑")
        card = self._source_card_for_job(job)
        self._validate_script(script, card)
        next_script_hash = content_hash(script)
        script_changed = next_script_hash != job.script_hash
        if script_changed:
            review = await self.script_provider.review(card, script)
            if not review.passed or review.blocking_reasons:
                raise QualityGateFailed("修改后的脚本自动审核未通过")
            if review.input_hash != next_script_hash:
                raise QualityGateFailed("脚本审核结果没有绑定修改后的脚本")
        else:
            review = job.script_review
            if (
                not review
                or not review.passed
                or review.blocking_reasons
                or review.input_hash != next_script_hash
            ):
                raise QualityGateFailed("当前脚本缺少有效的自动审核结果")
        current_settings = self._generation_settings(job)
        tts_voice_changed = (
            tts_voice_id is not None
            and tts_voice_id != current_settings.tts_voice_id
        )
        if tts_voice_id is not None:
            current_settings.tts_voice_id = tts_voice_id
        tts_speed_changed = (
            tts_speed_ratio is not None
            and tts_speed_ratio != current_settings.tts_speed_ratio
        )
        if tts_speed_ratio is not None:
            current_settings.tts_speed_ratio = tts_speed_ratio
        settings_changed = tts_voice_changed or tts_speed_changed
        if not script_changed and not settings_changed:
            return job
        job.generation_settings = current_settings
        self._apply_reviewed_script(
            job,
            script,
            review,
            allow_visual_reuse=True,
        )
        return await self._save_job(job, actor)

    @staticmethod
    def _narration_preview_text(script: ScriptDraft) -> str:
        opening = re.sub(r"\s+", " ", script.beats[0].narration).strip()
        if len(opening) <= TTS_PREVIEW_MAX_CHARACTERS:
            preview = opening
        else:
            window = opening[:TTS_PREVIEW_MAX_CHARACTERS]
            break_at = max(
                (window.rfind(mark) for mark in "，。！？；,.!?;"),
                default=-1,
            )
            preview = (
                window[:break_at + 1].strip()
                if break_at >= TTS_PREVIEW_MAX_CHARACTERS // 2
                else window.strip()
            )
        # The Provider adds a terminal full stop when one is absent. Reserve
        # that character so the billable request still stays within 60 chars.
        if (
            len(preview) >= TTS_PREVIEW_MAX_CHARACTERS
            and not preview.endswith(("。", "！", "？", "…", ".", "!", "?"))
        ):
            preview = preview[:TTS_PREVIEW_MAX_CHARACTERS - 1].rstrip()
        return preview

    async def preview_narration(
        self,
        job_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> tuple[VideoJob, bytes, str, float, str]:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.SCRIPT_REVIEW_REQUIRED or not job.script:
            raise InvalidTransition("只有待确认脚本可以试听配音")
        preview = getattr(self.tts_provider, "synthesize_preview", None)
        if not callable(preview):
            raise ProviderUnavailable("当前配音 Provider 不支持试听")
        settings = self._generation_settings(job)
        text = self._narration_preview_text(job.script)
        workspace = Path(tempfile.mkdtemp(
            prefix=f"{job.id}-tts-preview-",
            dir=self.work_root,
        ))
        try:
            async def persist_tts_usage(usage: ProviderUsageRecord) -> None:
                nonlocal job
                job = await self._persist_usage_record(job, usage, actor)

            generated = await preview(
                text,
                workspace,
                voice_id=settings.tts_voice_id,
                speed_ratio=settings.tts_speed_ratio,
                on_usage=persist_tts_usage,
            )
            audio = await asyncio.to_thread(generated.path.read_bytes)
            if not audio:
                raise ProviderUnavailable("配音试听没有生成有效音频")
            return (
                job,
                audio,
                generated.media_type,
                float(generated.duration_seconds or 0),
                text,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def approve_script(
        self,
        job_id: str,
        expected_revision: int,
        script_hash: str,
        actor: Actor,
        *,
        prepare_media_first: bool = False,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.SCRIPT_REVIEW_REQUIRED or not job.script:
            raise InvalidTransition("当前没有可确认的脚本")
        card = self._source_card_for_job(job)
        self._validate_script(job.script, card)
        actual = content_hash(job.script)
        if script_hash != actual or script_hash != job.script_hash:
            raise RevisionConflict("脚本已发生变化，请重新检查后确认")
        if (
            not job.script_review
            or not job.script_review.passed
            or job.script_review.input_hash != actual
        ):
            raise QualityGateFailed("自动脚本审核未通过")
        job.approvals = [item for item in job.approvals if item.kind != "script"]
        job.approvals.append(ApprovalRecord(
            kind="script",
            actor=actor.username,
            artifact_hash=actual,
            approved_at=timestamp(),
            warnings=list(job.script_review.warnings),
        ))
        job.pre_generation_media_mode = (
            PreGenerationMediaMode.REVIEW_BEFORE_GENERATION
            if (
                prepare_media_first
                or job.pipeline_version == PipelineVersion.QUALITY_FIRST
            )
            else PreGenerationMediaMode.AUTOMATIC
        )
        job.state = JobState.SCRIPT_APPROVED
        return await self._save_job(job, actor)

    async def confirm_pre_generation_media(
        self,
        job_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> VideoJob:
        """Freeze early upload choices before any paid visual generation."""

        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.MEDIA_REVIEW_REQUIRED:
            raise InvalidTransition("当前没有待确认的生成前镜头素材")
        if (
            job.pre_generation_media_mode
            != PreGenerationMediaMode.REVIEW_BEFORE_GENERATION
        ):
            raise InvalidTransition("当前任务没有启用生成前素材安排")
        if not job.narration_manifest or not job.storyboard_plan:
            raise QualityGateFailed("旁白或文字分镜尚未准备完成")
        if job.pipeline_version == PipelineVersion.QUALITY_FIRST:
            available_style_frames = {
                item.candidate_id
                for item in job.style_frame_candidates
                if item.asset
            }
            if (
                len(available_style_frames) != 3
                or job.selected_style_frame_id not in available_style_frames
            ):
                raise QualityGateFailed('请先从三张视觉开发样片中确认一张')
        if job.pending_shot_media_edits:
            raise QualityGateFailed("生成前素材安排不能包含成片后的待应用修改")
        known_media = {
            (item.shot_id, item.media_id) for item in job.shot_media_versions
        }
        for shot in job.storyboard_plan.shots:
            if (
                shot.selected_media_id
                and (shot.shot_id, shot.selected_media_id) not in known_media
            ):
                raise QualityGateFailed(
                    f"分镜 {shot.shot_id} 选择了不存在的上传素材"
                )
        job.pre_generation_media_mode = PreGenerationMediaMode.CONFIRMED
        job.state = JobState.SCRIPT_APPROVED
        job.error = ""
        return await self._save_job(job, actor)

    @staticmethod
    def style_frame_asset(
        job: VideoJob,
        candidate_id: str,
    ) -> AssetRef | None:
        candidate = next(
            (
                item
                for item in job.style_frame_candidates
                if item.candidate_id == candidate_id
            ),
            None,
        )
        return candidate.asset if candidate else None

    async def select_style_frame(
        self,
        job_id: str,
        candidate_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if (
            job.pipeline_version != PipelineVersion.QUALITY_FIRST
            or job.state != JobState.MEDIA_REVIEW_REQUIRED
            or not job.asset_bible
        ):
            raise InvalidTransition('当前没有可确认的视觉开发样片')
        candidate = next(
            (
                item
                for item in job.style_frame_candidates
                if item.candidate_id == candidate_id and item.asset
            ),
            None,
        )
        if not candidate:
            raise ResourceNotFound('视觉开发样片不存在')
        job.selected_style_frame_id = candidate.candidate_id
        job.asset_bible.references = [
            item
            for item in job.asset_bible.references
            if item.reference_id != 'approved_style_frame'
        ] + [
            MultimodalReferenceIR(
                reference_id='approved_style_frame',
                roles=['style'],
                applies_to=['all_shots'],
                retention_level='strong',
                preserve=[
                    '媒介、材质、色彩、光线、造型语言和空间层级',
                ],
                allow_change=['每章的主体动作、环境状态、景别与构图'],
                forbidden_transfer=[
                    '样片中的偶然动作、无关物件、可读文字和一次性构图',
                ],
            )
        ]
        return await self._save_job(job, actor)

    @staticmethod
    def needs_script_revision(job: VideoJob) -> bool:
        error = str(job.error or "")
        recoverable_markers = (
            "纯旁白共",
            "完整口播稿共",
            "完整口播共",
            "narration_duration_range=",
            "duration_range=",
            "旁白实际时长",
        )
        if not job.script:
            return False
        failed_for_narration = (
            job.state == JobState.FAILED
            and any(marker in error for marker in recoverable_markers)
        )
        legacy_production_stall = (
            job.state in (JobState.SCRIPT_APPROVED, JobState.PRODUCING)
            and narration_char_count(job.script.narration_text())
            > SCRIPT_HARD_MAX_CHARS
        )
        return failed_for_narration or legacy_production_stall

    async def reopen_script_review(
        self,
        job_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> VideoJob:
        """Return a narration-related failure to the existing script review."""

        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if not self.needs_script_revision(job):
            raise InvalidTransition("只有口播内容或旁白时长失败可以返回修改脚本")
        job.state = JobState.SCRIPT_REVIEW_REQUIRED
        job.failed_stage = ""
        job.error = ""
        job.approvals = []
        job.narration_manifest = None
        if not job.visual_requests:
            job.render_manifest = None
        job.quality_report = None
        job.artifacts = []
        job.review_bundle_hash = ""
        return await self._save_job(job, actor)

    @staticmethod
    def _split_subtitle_text(text: str, max_chars: int = 20) -> list[str]:
        # A cue is one visual line. Normalize embedded line breaks before
        # splitting so provider text can never force a second rendered line.
        value = re.sub(r"\s+", " ", str(text or "")).strip()
        if not value:
            return []
        sentences = [
            item.strip()
            for item in re.split(r"(?<=[。！？；!?])", value)
            if item.strip()
        ]
        chunks: list[str] = []
        for sentence in sentences:
            remaining = sentence
            while len(remaining) > max_chars:
                window = remaining[:max_chars]
                break_at = max(window.rfind(mark) for mark in ("，", "、", "：", " "))
                # Prefer a slightly shorter, semantically complete cue over
                # cutting a Chinese phrase in the middle. A punctuation mark
                # in the last two-thirds of the window is a useful boundary.
                if break_at < max_chars // 3:
                    break_at = max_chars - 1
                chunk = remaining[:break_at + 1].strip()
                if chunk:
                    chunks.append(chunk)
                remaining = remaining[break_at + 1:].lstrip()
            if remaining:
                chunks.append(remaining.strip())
        return chunks

    @staticmethod
    def _video_resolution(job: VideoJob) -> str:
        """Resolve new settings while preserving pre-settings paid requests."""

        if job.visual_requests:
            return job.visual_requests[0].resolution
        if job.generation_settings:
            return job.generation_settings.video_resolution
        return "480p"

    @staticmethod
    def _generation_settings(job: VideoJob) -> GenerationSettings:
        """Use the former five-shot layout only for jobs predating settings."""

        return job.generation_settings or GenerationSettings(
            image_count=2,
            tts_speed_ratio=1.0,
        )

    @staticmethod
    def _video_dimensions(job: VideoJob) -> tuple[int, int]:
        """Keep an already-rendered legacy size stable across later retries."""

        if job.render_manifest:
            existing = (job.render_manifest.width, job.render_manifest.height)
            if existing in VIDEO_OUTPUT_DIMENSIONS.values():
                return existing
        return VIDEO_OUTPUT_DIMENSIONS[QijiaVideoService._video_resolution(job)]

    @staticmethod
    def _visual_target_indices(
        segment_count: int, shot_count: int = LEGACY_STORYBOARD_SHOT_COUNT
    ) -> list[int]:
        if segment_count <= 0:
            return []
        count = min(max(1, int(shot_count)), segment_count)
        if count == segment_count:
            return list(range(segment_count))
        if count == 1:
            return [0]
        return [
            round(position * (segment_count - 1) / (count - 1))
            for position in range(count)
        ]

    @staticmethod
    def _visual_targets_for_job(job: VideoJob) -> list[int]:
        if not job.script:
            return []
        segment_count = len(job.script.beats)
        if job.generation_settings:
            if job.generation_settings.shot_count <= 0:
                return list(range(segment_count))
            return QijiaVideoService._visual_target_indices(
                segment_count, job.generation_settings.shot_count
            )
        # 兼容上线前已付费生成的两镜头任务，其旧映射是首段和中段。
        if len(job.visual_requests) == 2 and segment_count:
            return list(dict.fromkeys((0, segment_count // 2)))
        inferred_count = len(job.visual_requests) or LEGACY_STORYBOARD_SHOT_COUNT
        return QijiaVideoService._visual_target_indices(
            segment_count, inferred_count
        )

    @staticmethod
    def _persisted_storyboard_groups(
        job: VideoJob,
    ) -> list[list[ScriptBeat]] | None:
        """Keep an already-paid storyboard stable across workflow upgrades."""

        if not job.script or not job.storyboard_plan:
            return None
        expected_ids = [item.id for item in job.script.beats]
        persisted_ids = [
            beat_id
            for shot in job.storyboard_plan.shots
            for beat_id in shot.beat_ids
        ]
        if persisted_ids != expected_ids:
            compressed_ids = [
                beat_id
                for index, beat_id in enumerate(persisted_ids)
                if index == 0 or beat_id != persisted_ids[index - 1]
            ]
            if (
                compressed_ids != expected_ids
                or any(len(shot.beat_ids) != 1 for shot in job.storyboard_plan.shots)
            ):
                return None
        by_id = {item.id: item for item in job.script.beats}
        return [
            [by_id[beat_id] for beat_id in shot.beat_ids]
            for shot in job.storyboard_plan.shots
        ]

    @staticmethod
    def _narration_durations(job: VideoJob) -> dict[str, float]:
        if not job.narration_manifest:
            return {}
        durations = {
            item.segment_id: float(item.duration_seconds)
            for item in job.narration_manifest.segments
            if item.duration_seconds > 0
        }
        if not job.script or any(item.id not in durations for item in job.script.beats):
            return {}
        return durations

    @classmethod
    def _director_timing_map(cls, job: VideoJob) -> dict[str, float]:
        '''Return actual TTS timings, with a deterministic recovery fallback.'''

        if not job.script:
            raise InvalidTransition('缺少已确认脚本')
        actual = cls._narration_durations(job)
        if actual:
            return actual
        weights = {
            item.id: max(1, narration_char_count(item.narration))
            for item in job.script.beats
        }
        total_weight = sum(weights.values())
        total_duration = float(job.script.estimated_duration_seconds)
        return {
            beat_id: max(0.1, total_duration * weight / total_weight)
            for beat_id, weight in weights.items()
        }

    @staticmethod
    def _visual_types_for_durations(
        durations: list[float],
    ) -> tuple[str, ...]:
        shot_count = len(durations)
        if shot_count < SEEDANCE_VIDEO_SHOT_COUNT:
            return tuple("video" for _ in durations)
        # Keep the hook moving, then place two more videos near the behavioral
        # turn and closing. If a nearby chapter is too long for Seedance,
        # prefer the closest shorter chapter instead.
        preferred_indices = (
            0,
            round((shot_count - 1) * 0.72),
            shot_count - 1,
        )
        video_indices = {0}
        for preferred in preferred_indices[1:]:
            candidates = [
                index for index in range(1, shot_count)
                if index not in video_indices
            ]
            selected = min(
                candidates,
                key=lambda index: (
                    max(
                        0.0,
                        float(durations[index])
                        - SEEDANCE_MAX_NATURAL_CHAPTER_SECONDS,
                    ) ** 2,
                    abs(index - preferred),
                    index,
                ),
            )
            video_indices.add(selected)
        return tuple(
            "video" if index in video_indices else "image"
            for index in range(shot_count)
        )

    @classmethod
    def _duration_aware_groups(
        cls,
        beats: list[ScriptBeat],
        durations: dict[str, float],
        shot_count: int,
    ) -> list[list[ScriptBeat]]:
        if len(beats) == shot_count:
            return [[item] for item in beats]
        best: tuple[tuple[float, ...], list[list[ScriptBeat]]] | None = None
        for cuts in itertools.combinations(range(1, len(beats)), shot_count - 1):
            boundaries = (0, *cuts, len(beats))
            groups = [
                beats[boundaries[index]:boundaries[index + 1]]
                for index in range(shot_count)
            ]
            chapter_durations = [
                sum(durations[item.id] for item in group) for group in groups
            ]
            visual_types = cls._visual_types_for_durations(chapter_durations)
            video_overrun = sum(
                max(0.0, duration - SEEDANCE_MAX_NATURAL_CHAPTER_SECONDS) ** 2
                for duration, visual_type in zip(chapter_durations, visual_types)
                if visual_type == "video"
            )
            target = sum(chapter_durations) / shot_count
            imbalance = sum(
                (duration - target) ** 2 for duration in chapter_durations
            )
            score = (
                round(video_overrun, 6),
                round(max(chapter_durations), 6),
                round(imbalance, 6),
                *(float(value) for value in cuts),
            )
            if best is None or score < best[0]:
                best = (score, groups)
        if not best:
            raise QualityGateFailed(
                f"无法把完整口播规划成 {shot_count} 个连续章节"
            )
        return best[1]

    @classmethod
    def _storyboard_beat_groups(cls, job: VideoJob) -> list[list[ScriptBeat]]:
        if not job.script:
            raise InvalidTransition("缺少已确认脚本")
        persisted = cls._persisted_storyboard_groups(job)
        if persisted:
            return persisted
        settings = cls._generation_settings(job)
        beats = job.script.beats
        durations = cls._narration_durations(job)
        # Semantic mode and oversized legacy requests both map one meaningful
        # script beat to one visual chapter. Never duplicate a beat to hit a
        # quota.
        if settings.shot_count <= 0 or settings.shot_count >= len(beats):
            return [[item] for item in beats]
        if durations:
            return cls._duration_aware_groups(
                beats, durations, settings.shot_count
            )
        base_size, remainder = divmod(len(beats), settings.shot_count)
        groups: list[list[ScriptBeat]] = []
        cursor = 0
        for index in range(settings.shot_count):
            size = base_size + (1 if index < remainder else 0)
            groups.append(beats[cursor:cursor + size])
            cursor += size
        return groups

    @classmethod
    def _storyboard_visual_types(
        cls,
        job: VideoJob,
        groups: list[list[ScriptBeat]] | None = None,
    ) -> tuple[str, ...]:
        persisted = cls._persisted_storyboard_groups(job)
        if persisted and job.storyboard_plan:
            return tuple(item.visual_type for item in job.storyboard_plan.shots)
        groups = groups or cls._storyboard_beat_groups(job)
        settings = cls._generation_settings(job)
        if (
            settings.shot_count <= 0
            or (job.script and job.script.schema_version == "3.0")
        ):
            # Empty means the frozen Director Skill chooses per chapter.
            return ()
        durations = cls._narration_durations(job)
        if not durations:
            return cls._visual_types_for_durations([0.0] * len(groups))
        coverage = Counter(
            beat.id for group in groups for beat in group
        )
        return cls._visual_types_for_durations([
            sum(
                durations[item.id] / max(1, coverage[item.id])
                for item in group
            )
            for group in groups
        ])

    @staticmethod
    def _legacy_storyboard_segments(job: VideoJob) -> list[ScriptBeat]:
        if not job.script:
            raise InvalidTransition("缺少已确认脚本")
        settings = QijiaVideoService._generation_settings(job)
        indices = QijiaVideoService._visual_target_indices(
            len(job.script.beats), settings.shot_count
        )
        return [job.script.beats[index] for index in indices]

    @staticmethod
    def _has_legacy_storyboard(job: VideoJob) -> bool:
        return bool(
            job.storyboard_plan
            and job.script
            and job.script.schema_version == "1.0"
            and len(job.storyboard_plan.shots) == LEGACY_STORYBOARD_SHOT_COUNT
            and all(len(item.beat_ids) == 1 for item in job.storyboard_plan.shots)
        )

    @staticmethod
    def _storyboard_expected_groups(job: VideoJob) -> list[list[str]]:
        if QijiaVideoService._has_legacy_storyboard(job):
            return [
                [item.id]
                for item in QijiaVideoService._legacy_storyboard_segments(job)
            ]
        return [
            [item.id for item in group]
            for group in QijiaVideoService._storyboard_beat_groups(job)
        ]

    @staticmethod
    def _has_reference_image(job: VideoJob) -> bool:
        source_card = QijiaVideoService._source_card_for_job(job)
        return bool(source_card.reference_assets)

    @staticmethod
    def _uses_single_owner_director(job: VideoJob) -> bool:
        return job.pipeline_version in {
            PipelineVersion.SINGLE_OWNER,
            PipelineVersion.DIRECT_SCRIPT,
            PipelineVersion.QUALITY_FIRST,
        }

    @classmethod
    def _storyboard_base_style(cls, job: VideoJob) -> str:
        if cls._uses_single_owner_director(job):
            if not job.director_skill_snapshot:
                raise InvalidTransition('v2 任务缺少冻结的 Director Skill')
            return compile_director_instruction(
                job.director_skill_snapshot,
                visual_style=job.visual_style_snapshot,
                has_reference_image=cls._has_reference_image(job),
            )
        settings = cls._generation_settings(job)
        return compile_storyboard_base_style(
            settings.seedance_prompt,
            job.visual_style_snapshot,
            job.prompt_writing_profile_snapshot,
            has_reference_image=cls._has_reference_image(job),
            creative_brief=job.creative_brief,
        )

    @staticmethod
    def _storyboard_input_hash_for_style(
        job: VideoJob,
        base_style: str,
        *,
        include_visual_types: bool = True,
    ) -> str:
        if not job.script:
            raise InvalidTransition("缺少已确认脚本")
        if QijiaVideoService._has_legacy_storyboard(job):
            targets = QijiaVideoService._legacy_storyboard_segments(job)
            return content_hash({
                "script_hash": content_hash(job.script),
                "base_style": base_style,
                "target_segment_ids": [item.id for item in targets],
            })
        beat_groups = QijiaVideoService._storyboard_expected_groups(job)
        payload = {
            "script_hash": content_hash(job.script),
            "base_style": base_style,
            "beat_groups": beat_groups,
        }
        if include_visual_types:
            groups = QijiaVideoService._storyboard_beat_groups(job)
            visual_types = list(
                QijiaVideoService._storyboard_visual_types(job, groups)
            )
            if visual_types:
                payload["visual_types"] = visual_types
        return content_hash(payload)

    @classmethod
    def _storyboard_input_hash(cls, job: VideoJob) -> str:
        if (
            cls._uses_single_owner_director(job)
            and job.director_skill_snapshot
            and job.director_skill_snapshot.mode != 'legacy-style-director'
        ):
            if not job.script:
                raise InvalidTransition('缺少已确认脚本')
            timings = cls._director_timing_map(job)
            visual_style = job.visual_style_snapshot
            return content_hash({
                'script_hash': content_hash(job.script),
                'director_runtime_prompt_version': DIRECTOR_RUNTIME_PROMPT_VERSION,
                'director_method': {
                    'skill_id': job.director_skill_snapshot.skill_id,
                    'version': job.director_skill_snapshot.version,
                    'manifest_hash': job.director_skill_snapshot.manifest_hash,
                },
                'visual_style': {
                    'style_id': visual_style.style_id if visual_style else '',
                    'version': visual_style.version if visual_style else '',
                    'manifest_hash': visual_style.manifest_hash if visual_style else '',
                },
                'has_reference_image': cls._has_reference_image(job),
                'narration_timings': [
                    {
                        'beat_id': item.id,
                        'duration_seconds': round(float(timings[item.id]), 3),
                    }
                    for item in job.script.beats
                ],
            })
        return cls._storyboard_input_hash_for_style(
            job,
            cls._storyboard_base_style(job),
            # In v2 the Director chooses each chapter's media type. Those
            # choices are outputs, so they must never mutate the input
            # fingerprint after the plan has been persisted.
            include_visual_types=(
                not cls._uses_single_owner_director(job)
            ),
        )

    async def _ensure_storyboard_plan(
        self,
        job: VideoJob,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        director_v3 = bool(
            self._uses_single_owner_director(job)
            and job.director_skill_snapshot
            and job.director_skill_snapshot.mode != 'legacy-style-director'
        )
        director_v4 = job.pipeline_version == PipelineVersion.QUALITY_FIRST
        if director_v3:
            if not job.script or not job.director_skill_snapshot:
                raise InvalidTransition('Director v3 任务缺少确认脚本或导演快照')
            expected_ids = [item.id for item in job.script.beats]
            timings = self._director_timing_map(job)
            if job.storyboard_plan:
                artifact_hash = job.storyboard_plan.input_hash
                director_review = job.storyboard_plan.director_review
                reviewed_plan_hash = storyboard_review_hash(
                    job.storyboard_plan
                )
                returned_ids = [
                    beat_id
                    for shot in job.storyboard_plan.shots
                    for beat_id in shot.beat_ids
                ]
                if (
                    job.storyboard_plan.schema_version != '3.0'
                    or not artifact_hash
                    or returned_ids != expected_ids
                    or not job.visual_bible
                    or job.visual_bible.input_hash != artifact_hash
                    or (
                        director_v4
                        and (
                            not job.director_treatment
                            or job.director_treatment.input_hash != artifact_hash
                            or not job.asset_bible
                            or job.asset_bible.input_hash != artifact_hash
                            or (
                                director_review is not None
                                and (
                                    not director_review.passed
                                    or director_review.reviewed_plan_hash
                                    != reviewed_plan_hash
                                )
                            )
                        )
                    )
                    or job.visual_bible.director_skill_id
                    != job.director_skill_snapshot.skill_id
                    or job.visual_bible.director_skill_version
                    != job.director_skill_snapshot.version
                ):
                    raise QualityGateFailed(
                        '已保存导演方案的脚本覆盖、方法版本或产物绑定不一致'
                    )
                return job
            expected_hash = self._storyboard_input_hash(job)
            self._report(
                progress,
                message=(
                    '导演正在先建立视觉方案与资产圣经，再规划正式镜头…'
                    if director_v4
                    else '导演正在依据完整脚本和真实旁白时长规划视觉章节…'
                ),
                stage='storyboard',
                percent=44,
            )

            async def persist_director_usage(
                usage: ProviderUsageRecord,
            ) -> None:
                nonlocal job
                job = await self._persist_usage_record(job, usage, actor)

            if director_v4:
                generate_quality_director_plan = getattr(
                    self.storyboard_provider,
                    'generate_quality_director_plan',
                    None,
                )
                if not callable(generate_quality_director_plan):
                    raise ProviderUnavailable(
                        '当前分镜 Provider 不支持 v4 两阶段导演契约'
                    )
                source_card = self._source_card_for_job(job)
                reference_asset = (
                    AssetRef.model_validate(source_card.reference_assets[0])
                    if source_card.reference_assets
                    else None
                )
                reference_image_url = (
                    await self.storage.signed_get_url(
                        reference_asset,
                        expires=21600,
                    )
                    if reference_asset
                    else ''
                )
                (
                    director_treatment,
                    visual_bible,
                    asset_bible,
                    plan,
                ) = await generate_quality_director_plan(
                    job.script,
                    self._storyboard_base_style(job),
                    timings,
                    director_skill_id=job.director_skill_snapshot.skill_id,
                    director_skill_version=job.director_skill_snapshot.version,
                    input_hash=expected_hash,
                    reference_image_url=reference_image_url,
                    on_usage=persist_director_usage,
                )
            else:
                generate_director_plan = getattr(
                    self.storyboard_provider, 'generate_director_plan', None
                )
                if not callable(generate_director_plan):
                    raise ProviderUnavailable(
                        '当前分镜 Provider 不支持 Director v3 具体事件契约'
                    )
                visual_bible, plan = await generate_director_plan(
                    job.script,
                    self._storyboard_base_style(job),
                    timings,
                    director_skill_id=job.director_skill_snapshot.skill_id,
                    director_skill_version=job.director_skill_snapshot.version,
                    input_hash=expected_hash,
                    on_usage=persist_director_usage,
                )
                director_treatment = None
                asset_bible = None
            returned_ids = [
                beat_id for shot in plan.shots for beat_id in shot.beat_ids
            ]
            contexts = [shot.context for shot in plan.shots]
            event_keys = [
                re.sub(r'\s+', '', context.concrete_event).casefold()
                for context in contexts
                if context
            ]
            allowed_reference_roles = {
                'identity',
                'wardrobe',
                'object',
                'location',
                'style',
                'composition',
            }
            reference_roles = [
                role
                for context in contexts
                if context
                for role in context.reference_roles
            ]
            director_review = plan.director_review
            reviewed_plan_hash = storyboard_review_hash(plan)
            if (
                plan.schema_version != '3.0'
                or plan.input_hash != expected_hash
                or visual_bible.input_hash != expected_hash
                or returned_ids != expected_ids
                or any(context is None for context in contexts)
                or len(event_keys) != len(plan.shots)
                or len(set(event_keys)) != len(event_keys)
                or sum(shot.visual_type == 'video' for shot in plan.shots) > 3
                or any(role not in allowed_reference_roles for role in reference_roles)
                or (not self._has_reference_image(job) and reference_roles)
                or visual_bible.director_skill_id
                != job.director_skill_snapshot.skill_id
                or visual_bible.director_skill_version
                != job.director_skill_snapshot.version
                or (
                    director_v4
                    and (
                        not director_treatment
                        or director_treatment.input_hash != expected_hash
                        or not asset_bible
                        or asset_bible.input_hash != expected_hash
                        or not director_review
                        or not director_review.passed
                        or director_review.reviewed_plan_hash
                        != reviewed_plan_hash
                    )
                )
            ):
                raise ProviderUnavailable(
                    'Director 未交付完整、唯一且可执行的具体事件方案'
                )
            for shot in plan.shots:
                if shot.visual_type == 'video' and sum(
                    float(timings[beat_id]) for beat_id in shot.beat_ids
                ) > SEEDANCE_MAX_NATURAL_CHAPTER_SECONDS:
                    raise ProviderUnavailable(
                        'Director 把超过十秒的旁白章节错误分配为视频'
                    )
            job.storyboard_plan = plan
            job.director_treatment = director_treatment
            job.asset_bible = asset_bible
            job.visual_bible = visual_bible
            job = await self._save_job(job, actor)
            self._report(
                progress,
                message=(
                    f'{len(plan.shots)} 个具体事件章节已就绪，'
                    '正在确定每个镜头的素材来源…'
                ),
                stage='storyboard',
                percent=46,
            )
            return job

        beat_groups = self._storyboard_expected_groups(job)
        grouped_beats = self._storyboard_beat_groups(job)
        visual_types = list(self._storyboard_visual_types(job, grouped_beats))
        expected_hash = self._storyboard_input_hash(job)
        if job.storyboard_plan:
            accepted_hashes = {
                expected_hash,
                self._storyboard_input_hash_for_style(
                    job,
                    self._storyboard_base_style(job),
                    include_visual_types=False,
                ),
            }
            if (
                self._has_reference_image(job)
                and not self._uses_single_owner_director(job)
            ):
                # Storyboards created before reference-first styling used the
                # editable global style in their input hash. Accept that exact
                # persisted plan so an in-flight paid job can resume unchanged.
                settings = self._generation_settings(job)
                accepted_hashes.add(self._storyboard_input_hash_for_style(
                    job, settings.seedance_prompt
                ))
                accepted_hashes.add(self._storyboard_input_hash_for_style(
                    job,
                    settings.seedance_prompt,
                    include_visual_types=False,
                ))
            if (
                job.storyboard_plan.input_hash not in accepted_hashes
                or [item.beat_ids for item in job.storyboard_plan.shots]
                != beat_groups
            ):
                raise QualityGateFailed("已保存分镜与当前确认脚本不一致")
            if self._uses_single_owner_director(job) and (
                job.storyboard_plan.schema_version != '2.0'
                or not job.visual_bible
                or job.visual_bible.input_hash != expected_hash
                or not job.director_skill_snapshot
                or job.visual_bible.director_skill_id
                != job.director_skill_snapshot.skill_id
                or job.visual_bible.director_skill_version
                != job.director_skill_snapshot.version
            ):
                raise QualityGateFailed('v2 已保存分镜缺少 VisualBible/ShotContextIR')
            return job
        self._report(
            progress,
            message=f"正在把脚本规划成 {len(beat_groups)} 个连续视觉章节…",
            stage="storyboard",
            percent=44,
        )
        base_style = self._storyboard_base_style(job)
        async def persist_storyboard_usage(usage: ProviderUsageRecord) -> None:
            nonlocal job
            job = await self._persist_usage_record(job, usage, actor)

        if self._uses_single_owner_director(job):
            if not job.director_skill_snapshot:
                raise InvalidTransition('v2 任务缺少冻结的 Director Skill')
            generate_with_direction = getattr(
                self.storyboard_provider, 'generate_with_direction', None
            )
            if not callable(generate_with_direction):
                raise ProviderUnavailable(
                    '当前分镜 Provider 不支持 VisualBible/ShotContextIR v2 契约'
                )
            visual_bible, plan = await generate_with_direction(
                job.script,
                base_style,
                beat_groups,
                visual_types,
                director_skill_id=job.director_skill_snapshot.skill_id,
                director_skill_version=job.director_skill_snapshot.version,
                on_usage=persist_storyboard_usage,
            )
            if visual_bible.input_hash != expected_hash:
                raise ProviderUnavailable('VisualBible 返回了错误的输入指纹')
            if (
                visual_bible.director_skill_id
                != job.director_skill_snapshot.skill_id
                or visual_bible.director_skill_version
                != job.director_skill_snapshot.version
            ):
                raise ProviderUnavailable('VisualBible 返回了错误的 Director Skill 版本')
            if plan.schema_version != '2.0' or any(
                shot.context is None for shot in plan.shots
            ):
                raise ProviderUnavailable('Director 未交付完整 ShotContextIR')
            metaphors = [
                shot.context.visual_metaphor.strip() for shot in plan.shots
            ]
            if len(set(metaphors)) != len(metaphors):
                raise ProviderUnavailable('Director 返回了重复的视觉隐喻')
        else:
            generate_with_usage = getattr(
                self.storyboard_provider, 'generate_with_usage', None
            )
            if callable(generate_with_usage):
                plan = await generate_with_usage(
                    job.script,
                    base_style,
                    beat_groups,
                    visual_types,
                    on_usage=persist_storyboard_usage,
                )
            else:
                plan = await self.storyboard_provider.generate(
                    job.script,
                    base_style,
                    beat_groups,
                    visual_types,
                )
            visual_bible = None
        if plan.input_hash != expected_hash:
            raise ProviderUnavailable("分镜 Provider 返回了错误的输入指纹")
        if [item.beat_ids for item in plan.shots] != beat_groups:
            raise ProviderUnavailable("分镜 Provider 返回了错误的段落映射")
        if visual_types and [item.visual_type for item in plan.shots] != visual_types:
            raise ProviderUnavailable("分镜 Provider 返回了错误的图像/视频分配")
        if sum(item.visual_type == "video" for item in plan.shots) > 3:
            raise ProviderUnavailable('全片最多允许三段 AI 视频')
        job.storyboard_plan = plan
        job.visual_bible = visual_bible
        job = await self._save_job(job, actor)
        self._report(
            progress,
            message=(
                f"{len(plan.shots)} 章节分镜已就绪，"
                "正在确定每个镜头的素材来源…"
            ),
            stage="storyboard",
            percent=46,
        )
        return job

    @staticmethod
    def _first_frame_prompt(
        job: VideoJob,
        shot: StoryboardShot,
        *,
        has_reference_image: bool = False,
        available_reference_ids: set[str] | None = None,
        reference_order: list[str] | None = None,
    ) -> str:
        if QijiaVideoService._uses_single_owner_director(job):
            if (
                not job.provider_adapter_snapshot
                or not job.visual_bible
            ):
                raise InvalidTransition(
                    'v2 媒体编译缺少 Provider Adapter 或 VisualBible'
                )
            return compile_image_provider_prompt(
                job.provider_adapter_snapshot,
                job.visual_bible,
                shot,
                has_reference_image=has_reference_image,
                asset_bible=job.asset_bible,
                available_reference_ids=(
                    available_reference_ids
                    if available_reference_ids is not None
                    else (
                        {'approved_style_frame'}
                        if job.pipeline_version == PipelineVersion.QUALITY_FIRST
                        else {'global_reference'}
                    )
                ),
                reference_order=reference_order,
            )
        settings = QijiaVideoService._generation_settings(job)
        return compile_first_frame_prompt(
            settings.seedance_prompt,
            job.visual_style_snapshot,
            job.prompt_writing_profile_snapshot,
            shot,
            has_reference_image=has_reference_image,
        )

    @staticmethod
    def _opening_direction_for_shot(job: VideoJob, shot_id: str) -> str:
        first_shot_id = (
            job.storyboard_plan.shots[0].shot_id
            if job.storyboard_plan and job.storyboard_plan.shots
            else (
                job.visual_requests[0].request_id
                if job.visual_requests
                else ""
            )
        )
        if shot_id != first_shot_id:
            return ""
        return (
            "【抖音开场执行】这是全片第一个镜头。首帧已经处在冲突、反差或关键选择中；"
            "人物动作从第一帧立即发生，不要空镜、缓慢入场或先建立环境。前 2 秒让关系"
            "与矛盾清楚，前 5 秒通过自然反应或构图变化提供第二层信息。\n"
        )

    @classmethod
    def _compile_shot_revision_prompt(
        cls,
        job: VideoJob,
        shot_id: str,
        revision_intent: str,
        duration_seconds: int = SEEDANCE_SHOT_DURATION_SECONDS,
    ) -> str:
        storyboard_shot = (
            next(
                (
                    item
                    for item in job.storyboard_plan.shots
                    if item.shot_id == shot_id
                ),
                None,
            )
            if job.storyboard_plan
            else None
        )
        if storyboard_shot is None:
            if cls._uses_single_owner_director(job):
                raise InvalidTransition('v2 镜头缺少冻结的 ShotContextIR')
            # Historical jobs may not have a structured storyboard. They still
            # go through the compiler and safety boundaries instead of treating
            # editor text as a provider-ready prompt.
            storyboard_shot = StoryboardShot(
                shot_id=shot_id,
                segment_id=shot_id,
                narration_excerpt="沿用已确认脚本与当前首帧。",
                visual_type="video",
                visual_intent=revision_intent,
                first_frame_prompt="沿用当前已选择的首帧。",
                motion_prompt="从当前首帧自然延展。",
            )
        settings = cls._generation_settings(job)
        if cls._uses_single_owner_director(job):
            if (
                not job.provider_adapter_snapshot
                or not job.visual_bible
            ):
                raise InvalidTransition(
                    'v2 媒体编译缺少 Provider Adapter 或 VisualBible'
                )
            return compile_video_provider_prompt(
                job.provider_adapter_snapshot,
                job.visual_bible,
                storyboard_shot,
                opening_direction=cls._opening_direction_for_shot(job, shot_id),
                revision_intent=revision_intent,
                asset_bible=job.asset_bible,
                duration_seconds=duration_seconds,
            )
        return compile_video_prompt(
            settings.seedance_prompt,
            job.visual_style_snapshot,
            job.prompt_writing_profile_snapshot,
            storyboard_shot,
            has_reference_image=cls._has_reference_image(job),
            opening_direction=cls._opening_direction_for_shot(job, shot_id),
            revision_intent=revision_intent,
        )

    @staticmethod
    def _image_format(path: Path) -> tuple[str, str]:
        with path.open("rb") as handle:
            header = handle.read(16)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png", "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return ".jpg", "image/jpeg"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return ".webp", "image/webp"
        raise ProviderUnavailable("Seedream 返回的首帧格式无法识别")

    @staticmethod
    def _replace_first_frame_candidate(
        job: VideoJob, candidate: FirstFrameCandidate
    ) -> None:
        job.first_frame_candidates = [
            item
            for item in job.first_frame_candidates
            if item.candidate_id != candidate.candidate_id
        ] + [candidate]
        job.first_frame_candidates.sort(
            key=lambda item: (item.shot_id, item.variant)
        )

    @classmethod
    def first_frame_asset_for_shot(
        cls,
        job: VideoJob,
        shot_id: str,
        candidate_id: str,
    ) -> AssetRef | None:
        candidate = next(
            (
                item
                for item in job.first_frame_candidates
                if item.shot_id == shot_id and item.candidate_id == candidate_id
            ),
            None,
        )
        return candidate.asset if candidate else None

    @staticmethod
    def _replace_style_frame_candidate(
        job: VideoJob,
        candidate: StyleFrameCandidate,
    ) -> None:
        job.style_frame_candidates = [
            item
            for item in job.style_frame_candidates
            if item.candidate_id != candidate.candidate_id
        ] + [candidate]
        job.style_frame_candidates.sort(key=lambda item: item.variant)

    async def _ensure_style_frames(
        self,
        job: VideoJob,
        actor: Actor,
        workspace: Path,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        if job.pipeline_version != PipelineVersion.QUALITY_FIRST:
            return job
        if not job.director_treatment or not job.visual_bible or not job.asset_bible:
            raise InvalidTransition('v4 视觉开发样片缺少导演方案或 AssetBible')
        source_card = self._source_card_for_job(job)
        reference_asset = (
            AssetRef.model_validate(source_card.reference_assets[0])
            if source_card.reference_assets
            else None
        )
        reference_image_url = (
            await self.storage.signed_get_url(reference_asset, expires=21600)
            if reference_asset
            else ''
        )
        if reference_image_url and not any(
            item.reference_id == 'global_reference'
            for item in job.asset_bible.references
        ):
            raise QualityGateFailed('Director 未声明上传参考图的具体职责')
        comparison_seed = int(content_hash({
            'job_id': job.id,
            'stage': 'style_frame_comparison',
        })[:8], 16) & 0x7FFFFFFF
        for variant in range(1, 4):
            candidate_id = f'style_frame_{variant:02d}'
            candidate = next(
                (
                    item
                    for item in job.style_frame_candidates
                    if item.candidate_id == candidate_id
                ),
                None,
            )
            if candidate and candidate.asset:
                continue
            if not candidate:
                prompt = compile_style_frame_prompt(
                    job.director_treatment,
                    job.visual_bible,
                    job.asset_bible,
                    variant=variant,
                    has_reference_image=bool(reference_image_url),
                )
                # A/B/C share one event and one seed so the editor compares
                # visual treatment rather than three unrelated compositions.
                seed = comparison_seed
                self._report(
                    progress,
                    message=f'正在生成视觉开发样片 {variant}/3…',
                    stage='style_development',
                    percent=46 + variant,
                )
                try:
                    if reference_image_url:
                        generated = await self.image_provider.generate(
                            prompt,
                            seed=seed,
                            reference_image_url=reference_image_url,
                        )
                    else:
                        generated = await self.image_provider.generate(
                            prompt,
                            seed=seed,
                        )
                except ProviderUnavailable:
                    job = await self._persist_usage_record(
                        job,
                        ProviderUsageRecord(
                            usage_id=(
                                f'usage_seedream_style_attempt_{uuid.uuid4().hex}'
                            ),
                            operation='seedream_style_frame',
                            provider=self.image_provider.name,
                            model_id=str(
                                getattr(self.image_provider, 'model', '') or ''
                            ),
                            request_id=candidate_id,
                            succeeded=False,
                            quantity=1,
                            unit='image',
                            note='视觉开发样片请求失败或结果未知，是否计费需对账',
                            occurred_at=timestamp(),
                        ),
                        actor,
                    )
                    raise
                candidate = self._snapshot_image_cost(StyleFrameCandidate(
                    candidate_id=candidate_id,
                    variant=variant,
                    prompt=prompt,
                    seed=seed,
                    model_id=generated.model_id,
                    source_url=generated.url,
                    size=generated.size,
                    usage_total_tokens=generated.usage_total_tokens,
                    created_at=timestamp(),
                ))
                self._remember_usage_record(job, ProviderUsageRecord(
                    usage_id=(
                        'usage_seedream_style_'
                        + content_hash({
                            'job_id': job.id,
                            'candidate_id': candidate_id,
                        })[:36]
                    ),
                    operation='seedream_style_frame',
                    provider=self.image_provider.name,
                    model_id=candidate.model_id,
                    request_id=candidate_id,
                    succeeded=True,
                    total_tokens=candidate.usage_total_tokens,
                    quantity=1,
                    unit='image',
                    estimated_cost=candidate.estimated_cost_cny,
                    estimated_currency=(
                        'CNY'
                        if candidate.estimated_cost_cny is not None
                        else None
                    ),
                    pricing_basis=candidate.pricing_basis,
                    note=(
                        '测试 Provider 不计入生产费用'
                        if self.image_provider.name != 'volcengine-seedream'
                        else ''
                    ),
                    occurred_at=candidate.created_at,
                ))
                self._replace_style_frame_candidate(job, candidate)
                job = await self._save_job(job, actor)
            if not candidate.source_url:
                raise ProviderUnavailable(
                    f'视觉开发样片 {candidate.candidate_id} 缺少下载地址'
                )
            local_path = (
                workspace / 'style-frames' / f'{candidate.candidate_id}.image'
            )
            await self.image_provider.download(candidate.source_url, local_path)
            extension, media_type = self._image_format(local_path)
            asset = await self.storage.put_file(
                object_key=(
                    f'qijia-video/{job.id}/style-frames/'
                    f'{candidate.candidate_id}{extension}'
                ),
                path=local_path,
                asset_id=candidate.candidate_id,
                media_type=media_type,
            )
            candidate.asset = asset
            self._replace_style_frame_candidate(job, candidate)
            job = await self._save_job(job, actor)
        return job

    async def _ensure_first_frames(
        self,
        job: VideoJob,
        actor: Actor,
        workspace: Path,
        progress: ProgressReporter | None = None,
        *,
        target_shot_ids: set[str] | None = None,
        progress_stage: str = "first_frames",
        progress_start: int = 46,
        progress_end: int = 53,
        progress_label: str = "分镜",
    ) -> VideoJob:
        if not job.storyboard_plan:
            raise InvalidTransition("缺少镜头分镜")
        source_card = self._source_card_for_job(job)
        reference_asset = (
            AssetRef.model_validate(source_card.reference_assets[0])
            if source_card.reference_assets
            else None
        )
        reference_image_url = (
            await self.storage.signed_get_url(reference_asset, expires=21600)
            if reference_asset
            else ""
        )
        selected_style_frame = next(
            (
                item
                for item in job.style_frame_candidates
                if (
                    item.candidate_id == job.selected_style_frame_id
                    and item.asset
                )
            ),
            None,
        )
        style_frame_url = (
            await self.storage.signed_get_url(
                selected_style_frame.asset,
                expires=21600,
            )
            if selected_style_frame and selected_style_frame.asset
            else ''
        )
        reference_ids: list[str] = []
        reference_urls: list[str] = []
        if reference_image_url:
            reference_ids.append('global_reference')
            reference_urls.append(reference_image_url)
        if job.pipeline_version == PipelineVersion.QUALITY_FIRST:
            if not style_frame_url:
                raise QualityGateFailed('请先确认一张视觉开发样片')
            reference_ids.append('approved_style_frame')
            reference_urls.append(style_frame_url)
        target_indices = [
            index
            for index, shot in enumerate(job.storyboard_plan.shots)
            if target_shot_ids is None or shot.shot_id in target_shot_ids
        ]
        if not target_indices:
            return job
        total = len(target_indices)
        required_candidate_ids = {
            f"frame_{job.storyboard_plan.shots[index].shot_id}_01"
            for index in target_indices
        }
        completed = sum(
            1
            for item in job.first_frame_candidates
            if item.candidate_id in required_candidate_ids and item.asset is not None
        )
        progress_span = max(0, progress_end - progress_start)
        for target_position, shot_index in enumerate(target_indices, 1):
            # Saving returns a newly validated aggregate, so resolve the current
            # shot on each pass instead of retaining a stale nested model.
            shot = job.storyboard_plan.shots[shot_index]
            variant = 1
            candidate_id = f"frame_{shot.shot_id}_{variant:02d}"
            candidate = next(
                (
                    item
                    for item in job.first_frame_candidates
                    if item.candidate_id == candidate_id
                ),
                None,
            )
            if not candidate or not candidate.asset:
                prompt = self._first_frame_prompt(
                    job,
                    shot,
                    has_reference_image=bool(reference_urls),
                    available_reference_ids=set(reference_ids),
                    reference_order=reference_ids,
                )
                if not candidate:
                    # Ark ImageGenerations defines Seedream seed as signed int32.
                    seed = secrets.randbits(31)
                    self._report(
                        progress,
                        message=(
                            f"正在生成{progress_label} {target_position}/{total} "
                            "的首帧…"
                        ),
                        stage=progress_stage,
                        percent=progress_start + round(
                            (completed * progress_span) / max(1, total)
                        ),
                        shot_id=shot.shot_id,
                        frame=target_position,
                        frame_count=total,
                    )

                    try:
                        if len(reference_urls) > 1:
                            generated = await self.image_provider.generate(
                                prompt,
                                seed=seed,
                                reference_image_urls=reference_urls,
                            )
                        elif reference_urls:
                            generated = await self.image_provider.generate(
                                prompt,
                                seed=seed,
                                reference_image_url=reference_urls[0],
                            )
                        else:
                            generated = await self.image_provider.generate(
                                prompt,
                                seed=seed,
                            )
                    except ProviderUnavailable:
                        job = await self._persist_usage_record(
                            job,
                            ProviderUsageRecord(
                                usage_id=f"usage_seedream_attempt_{uuid.uuid4().hex}",
                                operation="seedream_image",
                                provider=self.image_provider.name,
                                model_id=str(
                                    getattr(self.image_provider, "model", "") or ""
                                ),
                                request_id=candidate_id,
                                succeeded=False,
                                quantity=1,
                                unit="image",
                                note=(
                                    "图片生成请求失败或结果未知，是否计费需与"
                                    "火山方舟账单核对"
                                ),
                                occurred_at=timestamp(),
                            ),
                            actor,
                        )
                        raise
                    candidate = self._snapshot_image_cost(FirstFrameCandidate(
                        candidate_id=candidate_id,
                        shot_id=shot.shot_id,
                        variant=variant,
                        prompt=prompt,
                        seed=seed,
                        model_id=generated.model_id,
                        source_url=generated.url,
                        size=generated.size,
                        usage_total_tokens=generated.usage_total_tokens,
                        created_at=timestamp(),
                    ))
                    self._remember_usage_record(job, ProviderUsageRecord(
                        usage_id=(
                            "usage_seedream_"
                            + content_hash({
                                "job_id": job.id,
                                "candidate_id": candidate.candidate_id,
                            })[:40]
                        ),
                        operation="seedream_image",
                        provider=self.image_provider.name,
                        model_id=candidate.model_id,
                        request_id=candidate.candidate_id,
                        succeeded=True,
                        total_tokens=candidate.usage_total_tokens,
                        quantity=1,
                        unit="image",
                        estimated_cost=candidate.estimated_cost_cny,
                        estimated_currency=(
                            "CNY"
                            if candidate.estimated_cost_cny is not None
                            else None
                        ),
                        pricing_basis=candidate.pricing_basis,
                        note=(
                            "测试 Provider 不计入生产费用"
                            if self.image_provider.name != "volcengine-seedream"
                            else (
                                "Seedream 单价未配置，金额待火山方舟账单核对"
                                if candidate.estimated_cost_cny is None
                                else ""
                            )
                        ),
                        occurred_at=candidate.created_at,
                    ))
                    self._replace_first_frame_candidate(job, candidate)
                    # Persist the paid response before downloading so a retry
                    # never submits the same Seedream request again.
                    job = await self._save_job(job, actor)
                if not candidate.source_url:
                    raise ProviderUnavailable(
                        f"首帧 {candidate.candidate_id} 缺少下载地址"
                    )
                local_path = (
                    workspace / "first-frames" / f"{candidate.candidate_id}.image"
                )
                await self.image_provider.download(candidate.source_url, local_path)
                extension, media_type = self._image_format(local_path)
                asset = await self.storage.put_file(
                    object_key=(
                        f"qijia-video/{job.id}/first-frames/{shot.shot_id}/"
                        f"{candidate.candidate_id}{extension}"
                    ),
                    path=local_path,
                    asset_id=f"first_frame_{candidate.candidate_id}",
                    media_type=media_type,
                )
                candidate.asset = asset
                self._replace_first_frame_candidate(job, candidate)
                job = await self._save_job(job, actor)
                completed += 1
            current_shot = job.storyboard_plan.shots[shot_index]
            available = {
                item.candidate_id: item
                for item in job.first_frame_candidates
                if item.shot_id == current_shot.shot_id and item.asset
            }
            legacy_selection = next(
                (
                    item
                    for item in job.frame_selections
                    if item.shot_id == current_shot.shot_id
                ),
                None,
            )
            selected_candidate_id = next(
                (
                    candidate_id
                    for candidate_id in (
                        current_shot.selected_candidate_id,
                        legacy_selection.recommended_candidate_id
                        if legacy_selection
                        else "",
                        f"frame_{current_shot.shot_id}_01",
                    )
                    if candidate_id in available
                ),
                "",
            )
            if not selected_candidate_id:
                raise QualityGateFailed(f"分镜 {current_shot.shot_id} 缺少可用首帧")
            if current_shot.selected_candidate_id != selected_candidate_id:
                current_shot.selected_candidate_id = selected_candidate_id
                job = await self._save_job(job, actor)
        return job

    @classmethod
    def _build_visual_requests(
        cls, job: VideoJob
    ) -> list[VisualGenerationRequest]:
        if not job.script:
            raise InvalidTransition("缺少已确认脚本")
        # 一旦请求已经冻结，就始终复用原规格和指纹。这样 720p/5 秒的旧任务
        # 在升级后重试也不会被改写或再次产生视频生成费用。
        if job.visual_requests:
            return list(job.visual_requests)
        settings = cls._generation_settings(job)
        if job.storyboard_plan:
            candidates_by_id = {
                item.candidate_id: item for item in job.first_frame_candidates
            }
            narration_timing = {
                item.segment_id: item
                for item in (
                    job.narration_manifest.segments
                    if job.narration_manifest
                    else []
                )
            }
            requests: list[VisualGenerationRequest] = []
            for shot in job.storyboard_plan.shots:
                if shot.visual_type != "video" or shot.selected_media_id:
                    continue
                candidate = candidates_by_id.get(shot.selected_candidate_id)
                if not candidate or not candidate.asset:
                    raise QualityGateFailed(
                        f"分镜 {shot.shot_id} 缺少已选中的首帧资产"
                    )
                shot_timings = [
                    narration_timing[beat_id]
                    for beat_id in shot.beat_ids
                    if beat_id in narration_timing
                ]
                narration_seconds = sum(
                    item.duration_seconds for item in shot_timings
                )
                duration_seconds = max(
                    SEEDANCE_SHOT_DURATION_SECONDS,
                    min(
                        10,
                        int(round(narration_seconds))
                        if shot_timings
                        else SEEDANCE_SHOT_DURATION_SECONDS,
                    ),
                )
                prompt = cls._compile_shot_revision_prompt(
                    job,
                    shot.shot_id,
                    '',
                    duration_seconds=duration_seconds,
                )
                requests.append(VisualGenerationRequest(
                    request_id=shot.shot_id,
                    prompt=prompt,
                    model_id=settings.seedance_model,
                    resolution=settings.video_resolution,
                    duration_seconds=duration_seconds,
                    generate_audio=False,
                    first_frame_asset_id=candidate.asset.asset_id,
                ))
            return requests
        segments = job.script.beats
        targets = QijiaVideoService._visual_target_indices(
            len(segments), settings.shot_count
        )
        requests: list[VisualGenerationRequest] = []
        for shot_number, segment_index in enumerate(targets, 1):
            segment = segments[segment_index]
            prompt = (
                f"{settings.seedance_prompt.strip()}\n"
                f"本镜头对应第 {shot_number}/{len(targets)} 段"
                f"（{segment.segment_type}）。\n"
                "下面的口播原文只用于理解画面语义，不是需要展示的台词。"
                "画面只表现主体、动作、空间、关系和必要的象征物；不得把原文排版、书写或拼成"
                "任何可读文字，书本、纸张、屏幕和标牌也不要出现可读内容。\n"
                f"【画面语义参考】{segment.text[:500]}"
            )
            requests.append(VisualGenerationRequest(
                request_id=f"shot_{shot_number:02d}",
                prompt=prompt,
                model_id=settings.seedance_model,
                resolution=settings.video_resolution,
                duration_seconds=SEEDANCE_SHOT_DURATION_SECONDS,
                generate_audio=False,
            ))
        return requests

    def _record_video_task_usage(
        self,
        job: VideoJob,
        task: ProviderTask,
        previous: ProviderTask | None = None,
    ) -> None:
        if previous:
            task.created_at = previous.created_at or task.created_at
            if previous.model_id:
                # Poll responses do not always repeat the model. The submitted
                # task is authoritative, especially for a 2.0 shot upgrade.
                task.model_id = previous.model_id
            task.pricing_rate_cny_per_million = (
                previous.pricing_rate_cny_per_million
            )
            if (
                task.pricing_rate_cny_per_million is None
                and previous.estimated_cost_cny is not None
                and previous.usage_total_tokens > 0
            ):
                task.pricing_rate_cny_per_million = round(
                    previous.estimated_cost_cny
                    * 1_000_000
                    / previous.usage_total_tokens,
                    8,
                )
            if (
                task.usage_total_tokens == 0
                and previous.estimated_cost_cny is not None
            ):
                task.estimated_cost_cny = previous.estimated_cost_cny
            task.pricing_basis = previous.pricing_basis
        elif not task.created_at:
            task.created_at = timestamp()
        self._snapshot_video_cost(task)
        self._remember_usage_record(job, ProviderUsageRecord(
            usage_id=(
                "usage_seedance_"
                + content_hash({
                    "provider": task.provider,
                    "provider_task_id": task.provider_task_id,
                })[:40]
            ),
            operation="seedance_video",
            provider=task.provider,
            model_id=task.model_id,
            request_id=task.provider_task_id,
            succeeded=task.state == ProviderTaskState.SUCCEEDED,
            total_tokens=task.usage_total_tokens,
            quantity=1,
            unit="video",
            estimated_cost=task.estimated_cost_cny,
            estimated_currency=(
                "CNY" if task.estimated_cost_cny is not None else None
            ),
            pricing_basis=task.pricing_basis,
            note=(
                "测试 Provider 不计入生产费用"
                if task.provider != "volcengine-seedance"
                else (
                    "供应商尚未回传 usage.total_tokens，金额待状态查询或账单核对"
                    if task.usage_total_tokens == 0
                    else (
                        "Seedance 单价未配置，金额待火山方舟账单核对"
                        if task.estimated_cost_cny is None
                        else ""
                    )
                )
            ),
            occurred_at=task.created_at,
        ))

    def _replace_video_task(self, job: VideoJob, task: ProviderTask) -> None:
        previous = next(
            (
                item
                for item in [
                    *job.video_tasks,
                    *(version.task for version in job.visual_versions),
                ]
                if item.request_fingerprint == task.request_fingerprint
                or (
                    task.provider_task_id
                    and item.provider_task_id == task.provider_task_id
                )
            ),
            None,
        )
        self._record_video_task_usage(job, task, previous)
        job.video_tasks = [
            item
            for item in job.video_tasks
            if item.request_fingerprint != task.request_fingerprint
            and (not task.request_id or item.request_id != task.request_id)
        ] + [task]

    @staticmethod
    def _all_video_tasks(job: VideoJob) -> list[ProviderTask]:
        """Return every paid attempt once, including versions no longer selected."""

        unique: dict[str, ProviderTask] = {}
        for task in [
            *job.video_tasks,
            *(version.task for version in job.visual_versions),
        ]:
            key = (
                f"{task.provider}:{task.provider_task_id}"
                if task.provider_task_id
                else f"{task.provider}:{task.request_fingerprint}"
            )
            unique[key] = task
        return list(unique.values())

    async def _ensure_video_task_submissions(
        self,
        job: VideoJob,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        desired = self._build_visual_requests(job)
        desired_fingerprints = [item.fingerprint() for item in desired]
        previous_fingerprints = [item.fingerprint() for item in job.visual_requests]
        if job.video_tasks and previous_fingerprints != desired_fingerprints:
            raise QualityGateFailed(
                "已付费的视频请求与当前脚本不一致；请修改并重新确认脚本后再生成"
            )
        if previous_fingerprints != desired_fingerprints:
            job.visual_requests = desired
            job.video_tasks = []
            job = await self._save_job(job, actor)
        existing = {
            item.request_fingerprint: item for item in job.video_tasks
        }
        for shot_index, request in enumerate(desired, 1):
            fingerprint = request.fingerprint()
            if fingerprint in existing:
                continue
            self._report(
                progress,
                message=f"正在提交 Seedance 视频 {shot_index}/{len(desired)}…",
                stage=f"seedance_shot_{shot_index}",
                percent=54 + round(
                    ((shot_index - 1) * 4) / max(1, len(desired))
                ),
                shot=shot_index,
                shot_count=len(desired),
            )
            first_frame_url = ""
            if request.first_frame_asset_id:
                candidate = next(
                    (
                        item
                        for item in job.first_frame_candidates
                        if item.asset
                        and item.asset.asset_id == request.first_frame_asset_id
                    ),
                    None,
                )
                if not candidate or not candidate.asset:
                    raise QualityGateFailed(
                        f"AI 镜头 {request.request_id} 的首帧资产不存在"
                    )
                first_frame_url = await self.storage.signed_get_url(
                    candidate.asset, expires=3600
                )
            try:
                task = await self.video_provider.submit(
                    request, first_frame_url=first_frame_url
                )
            except ProviderUnavailable:
                job = await self._persist_usage_record(
                    job,
                    ProviderUsageRecord(
                        usage_id=f"usage_seedance_attempt_{uuid.uuid4().hex}",
                        operation="seedance_video",
                        provider=self.video_provider.name,
                        model_id=(
                            request.model_id
                            or str(getattr(self.video_provider, "model", "") or "")
                        ),
                        request_id=request.request_id,
                        succeeded=False,
                        quantity=1,
                        unit="video",
                        note=(
                            "视频生成提交失败或结果未知，是否计费需与"
                            "火山方舟账单核对"
                        ),
                        occurred_at=timestamp(),
                    ),
                    actor,
                )
                raise
            if task.request_fingerprint != fingerprint:
                raise ProviderUnavailable("视频 Provider 返回了错误的请求指纹")
            task.request_id = request.request_id
            self._replace_video_task(job, task)
            # Provider Task ID 一收到就持久化；后续恢复只查询该 ID，绝不重提。
            job = await self._save_job(job, actor)
            existing[fingerprint] = task

        self._report(
            progress,
            message=(
                f"{len(desired)} 段 Seedance 已在后台生成，"
                "同时补齐动态图片…"
            ),
            stage="seedance_parallel",
            percent=58,
            shot_count=len(desired),
        )
        return job

    async def _wait_for_video_tasks(
        self,
        job: VideoJob,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        desired = self._build_visual_requests(job)
        for shot_index, request in enumerate(desired, 1):
            fingerprint = request.fingerprint()
            task = next(
                item
                for item in job.video_tasks
                if item.request_fingerprint == fingerprint
            )
            # 旧任务是在用量字段上线前持久化的。重试生产时免费查询一次状态，
            # 尽量补回真实 token；查询失败不阻断已保存资产的恢复流程。
            if (
                task.state == ProviderTaskState.SUCCEEDED
                and task.usage_total_tokens == 0
            ):
                try:
                    refreshed = await self.video_provider.get_status(
                        task.provider_task_id, fingerprint
                    )
                except ProviderUnavailable:
                    refreshed = None
                if refreshed and refreshed.state == ProviderTaskState.SUCCEEDED:
                    refreshed.request_id = request.request_id
                    task = refreshed
                    self._replace_video_task(job, task)
                    job = await self._save_job(job, actor)
            stage = f"seedance_shot_{shot_index}"
            start_percent = 65 + round(
                ((shot_index - 1) * 9) / len(desired)
            )
            completed_percent = 65 + round(
                (shot_index * 9) / len(desired)
            )
            deadline = time.monotonic() + self.video_timeout_seconds
            while task.state != ProviderTaskState.SUCCEEDED:
                if task.state in (
                    ProviderTaskState.FAILED,
                    ProviderTaskState.CANCELLED,
                ):
                    detail = task.error_message or task.raw_status or task.state.value
                    raise ProviderUnavailable(
                        f"Seedance 镜头 {request.request_id} 生成失败：{detail}"
                    )
                if time.monotonic() >= deadline:
                    raise ProviderUnavailable(
                        f"Seedance 镜头 {request.request_id} 等待超时；"
                        "任务 ID 已保存，重试只会继续查询"
                    )
                self._report(
                    progress,
                    message=(
                        f"Seedance 视频 {shot_index}/{len(desired)} 生成中…"
                    ),
                    stage=stage,
                    percent=min(completed_percent - 1, start_percent + 2),
                    shot=shot_index,
                    shot_count=len(desired),
                    provider_status=task.raw_status or task.state.value,
                )
                await asyncio.sleep(self.video_poll_interval_seconds)
                task = await self.video_provider.get_status(
                    task.provider_task_id, fingerprint
                )
                task.request_id = request.request_id
                self._replace_video_task(job, task)
                job = await self._save_job(job, actor)
            self._report(
                progress,
                message=f"Seedance 视频 {shot_index}/{len(desired)} 已生成",
                stage=stage,
                percent=completed_percent,
                shot=shot_index,
                shot_count=len(desired),
                provider_status=task.raw_status or task.state.value,
                usage_total_tokens=task.usage_total_tokens,
            )
        return job

    async def _ensure_video_tasks(
        self,
        job: VideoJob,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        job = await self._ensure_video_task_submissions(job, actor, progress)
        return await self._wait_for_video_tasks(job, actor, progress)

    @staticmethod
    def _narration_assets(job: VideoJob) -> list[AssetRef]:
        if not job.narration_manifest or not job.render_manifest:
            return []
        expected = {job.narration_manifest.full_audio_asset_id} | {
            item.asset_id for item in job.narration_manifest.segments
        }
        assets = [
            item for item in job.render_manifest.assets if item.asset_id in expected
        ]
        return assets if {item.asset_id for item in assets} == expected else []

    @staticmethod
    def _visual_asset_id(request: VisualGenerationRequest) -> str:
        return f"visual_{request.request_id}"

    @staticmethod
    def _shot_chapter_duration_seconds(
        job: VideoJob,
        shot_id: str,
        fallback: float,
    ) -> float:
        if not job.storyboard_plan or not job.narration_manifest:
            return max(0.001, float(fallback))
        timings = {
            item.segment_id: item for item in job.narration_manifest.segments
        }
        shots = job.storyboard_plan.shots
        shot_index = next(
            (index for index, item in enumerate(shots) if item.shot_id == shot_id),
            None,
        )
        if shot_index is None:
            return max(0.001, float(fallback))
        coverage = Counter(
            beat_id for shot in shots for beat_id in shot.beat_ids
        )
        target = shots[shot_index]
        if any(beat_id not in timings for beat_id in target.beat_ids):
            return max(0.001, float(fallback))
        duration = sum(
            float(timings[beat_id].duration_seconds)
            / max(1, coverage[beat_id])
            for beat_id in target.beat_ids
        )
        return max(0.001, duration)

    @staticmethod
    def _task_for_request(
        job: VideoJob, request: VisualGenerationRequest
    ) -> ProviderTask | None:
        fingerprint = request.fingerprint()
        return next(
            (
                item
                for item in job.video_tasks
                if item.request_fingerprint == fingerprint
            ),
            None,
        )

    @staticmethod
    def _version_for_request(
        job: VideoJob, request: VisualGenerationRequest
    ) -> VisualShotVersion | None:
        fingerprint = request.fingerprint()
        return next(
            (
                item
                for item in job.visual_versions
                if item.shot_id == request.request_id
                and item.request.fingerprint() == fingerprint
            ),
            None,
        )

    @staticmethod
    def _render_visual_assets(job: VideoJob) -> list[AssetRef]:
        if not job.render_manifest:
            return []
        return [
            item
            for item in job.render_manifest.assets
            if item.media_type.startswith("video/")
        ]

    @staticmethod
    def shot_media_for_shot(
        job: VideoJob,
        shot_id: str,
        *,
        media_id: str = "",
    ) -> ShotMediaVersion | None:
        if media_id:
            return next(
                (
                    item
                    for item in job.shot_media_versions
                    if item.shot_id == shot_id and item.media_id == media_id
                ),
                None,
            )
        if not job.storyboard_plan:
            return None
        shot = next(
            (item for item in job.storyboard_plan.shots if item.shot_id == shot_id),
            None,
        )
        if not shot or not shot.selected_media_id:
            return None
        return next(
            (
                item
                for item in job.shot_media_versions
                if item.shot_id == shot_id
                and item.media_id == shot.selected_media_id
            ),
            None,
        )

    @staticmethod
    def pending_shot_media_edit_for_shot(
        job: VideoJob,
        shot_id: str,
    ) -> PendingShotMediaEdit | None:
        return next(
            (
                item
                for item in job.pending_shot_media_edits
                if item.shot_id == shot_id
            ),
            None,
        )

    @staticmethod
    def pending_shot_media_fingerprint(job: VideoJob) -> str:
        return content_hash([
            {"shot_id": item.shot_id, "media_id": item.media_id}
            for item in sorted(
                job.pending_shot_media_edits,
                key=lambda edit: edit.shot_id,
            )
        ])

    @staticmethod
    def _stage_pending_shot_media_edit(
        job: VideoJob,
        shot_id: str,
        media_id: str,
        actor: Actor,
    ) -> None:
        pending = PendingShotMediaEdit(
            shot_id=shot_id,
            media_id=media_id,
            staged_by=actor.username,
            staged_at=timestamp(),
        )
        for index, item in enumerate(job.pending_shot_media_edits):
            if item.shot_id == shot_id:
                job.pending_shot_media_edits[index] = pending
                break
        else:
            job.pending_shot_media_edits.append(pending)

    @classmethod
    def _generated_visual_asset_for_shot(
        cls,
        job: VideoJob,
        shot_id: str,
        *,
        version_id: str = "",
    ) -> AssetRef | None:
        if version_id:
            version = next(
                (
                    item
                    for item in job.visual_versions
                    if item.shot_id == shot_id
                    and item.version_id == version_id
                ),
                None,
            )
            return version.asset if version else None
        request = next(
            (item for item in job.visual_requests if item.request_id == shot_id),
            None,
        )
        if not request:
            shot = next(
                (
                    item
                    for item in (
                        job.storyboard_plan.shots
                        if job.storyboard_plan else []
                    )
                    if item.shot_id == shot_id
                ),
                None,
            )
            if not shot:
                return None
            candidate = next(
                (
                    item
                    for item in job.first_frame_candidates
                    if item.shot_id == shot_id
                    and item.candidate_id == shot.selected_candidate_id
                ),
                None,
            )
            return candidate.asset if candidate else None
        version = cls._version_for_request(job, request)
        if version and version.asset:
            return version.asset
        try:
            request_index = next(
                index
                for index, item in enumerate(job.visual_requests)
                if item.request_id == shot_id
            )
        except StopIteration:
            return None
        assets = cls._render_visual_assets(job)
        return assets[request_index] if request_index < len(assets) else None

    @classmethod
    def visual_asset_for_shot(
        cls,
        job: VideoJob,
        shot_id: str,
        *,
        version_id: str = "",
    ) -> AssetRef | None:
        """Resolve a private preview asset without exposing storage details."""

        if version_id:
            return cls._generated_visual_asset_for_shot(
                job, shot_id, version_id=version_id
            )
        uploaded = cls.shot_media_for_shot(job, shot_id)
        if uploaded:
            return uploaded.asset
        return cls._generated_visual_asset_for_shot(job, shot_id)

    @staticmethod
    def _remember_visual_version(
        job: VideoJob,
        request: VisualGenerationRequest,
        task: ProviderTask,
        asset: AssetRef | None,
        actor: Actor,
    ) -> VisualShotVersion:
        fingerprint = request.fingerprint()
        existing = next(
            (
                item
                for item in job.visual_versions
                if item.shot_id == request.request_id
                and item.request.fingerprint() == fingerprint
            ),
            None,
        )
        if existing:
            existing.task = task
            if asset:
                existing.asset = asset
            return existing
        version_number = 1 + max(
            (
                item.version
                for item in job.visual_versions
                if item.shot_id == request.request_id
            ),
            default=0,
        )
        version = VisualShotVersion(
            version_id=f"{request.request_id}_v{version_number:02d}",
            shot_id=request.request_id,
            version=version_number,
            request=request,
            task=task,
            asset=asset,
            created_by=actor.username,
            created_at=timestamp(),
        )
        job.visual_versions.append(version)
        return version

    @classmethod
    def _remember_current_visuals(cls, job: VideoJob, actor: Actor) -> None:
        """Lazily give pre-versioning jobs a v1 history without a migration."""

        render_assets = cls._render_visual_assets(job)
        for index, request in enumerate(job.visual_requests):
            task = cls._task_for_request(job, request)
            if not task:
                continue
            existing = cls._version_for_request(job, request)
            asset = (
                existing.asset
                if existing and existing.asset
                else render_assets[index] if index < len(render_assets) else None
            )
            cls._remember_visual_version(job, request, task, asset, actor)

    @staticmethod
    def _seedance_model_for_request(
        job: VideoJob,
        request: VisualGenerationRequest,
    ) -> str:
        """Resolve legacy requests without changing their paid fingerprint."""

        if request.model_id:
            return request.model_id
        fingerprint = request.fingerprint()
        for task in [
            *job.video_tasks,
            *(item.task for item in job.visual_versions),
        ]:
            if (
                task.request_fingerprint == fingerprint
                and task.model_id in {
                    SEEDANCE_EFFICIENT_MODEL,
                    SEEDANCE_BALANCED_MODEL,
                    SEEDANCE_FLAGSHIP_MODEL,
                }
            ):
                return task.model_id
        if job.generation_settings:
            return job.generation_settings.seedance_model
        # Jobs old enough to have no frozen generation settings were produced
        # before model selection was frozen on each request.
        return SEEDANCE_FLAGSHIP_MODEL

    @classmethod
    def _selected_visual_assets(cls, job: VideoJob) -> list[AssetRef]:
        assets: list[AssetRef] = []
        for request in job.visual_requests:
            asset = cls.visual_asset_for_shot(job, request.request_id)
            if not asset:
                raise QualityGateFailed(
                    f"AI 镜头 {request.request_id} 缺少可用视频资产"
                )
            assets.append(asset)
        return assets

    async def _ensure_visual_assets(
        self,
        job: VideoJob,
        actor: Actor,
        workspace: Path,
        audio_assets: list[AssetRef],
    ) -> tuple[VideoJob, list[AssetRef]]:
        existing_by_id = {
            item.asset_id: item
            for item in (job.render_manifest.assets if job.render_manifest else [])
            if item.media_type.startswith("video/")
        }
        visual_assets: list[AssetRef] = []
        tasks = {
            item.request_fingerprint: item for item in job.video_tasks
        }
        for request in job.visual_requests:
            asset_id = self._visual_asset_id(request)
            known_version = self._version_for_request(job, request)
            if known_version and known_version.asset:
                visual_assets.append(known_version.asset)
                continue
            if asset_id in existing_by_id:
                asset = existing_by_id[asset_id]
                visual_assets.append(asset)
                task = tasks.get(request.fingerprint())
                if task:
                    self._remember_visual_version(
                        job, request, task, asset, actor
                    )
                    job = await self._save_job(job, actor)
                continue
            task = tasks.get(request.fingerprint())
            if not task or task.state != ProviderTaskState.SUCCEEDED:
                raise ProviderUnavailable(
                    f"Seedance 镜头 {request.request_id} 尚未生成成功"
                )
            local_path = workspace / "video" / f"{request.request_id}.mp4"
            await self.video_provider.download(task.provider_task_id, local_path)
            prepared_path, actual_duration = (
                await self.media_packager.prepare_video_for_timeline(
                    local_path,
                    workspace / "video" / f"{request.request_id}-timeline.mp4",
                    minimum_duration_seconds=self._shot_chapter_duration_seconds(
                        job,
                        request.request_id,
                        request.duration_seconds,
                    ),
                )
            )
            asset = await self.storage.put_file(
                object_key=(
                    f"qijia-video/{job.id}/video/{request.request_id}.mp4"
                ),
                path=prepared_path,
                asset_id=asset_id,
                media_type="video/mp4",
                duration_seconds=round(actual_duration, 3),
            )
            visual_assets.append(asset)
            self._remember_visual_version(job, request, task, asset, actor)
            job.render_manifest = self._build_render_manifest(
                job, audio_assets, visual_assets
            )
            # 下载和转存不产生第二次视频生成费用，但仍逐镜头持久化，便于恢复。
            job = await self._save_job(job, actor)
        return job, visual_assets

    @staticmethod
    def _build_render_manifest(
        job: VideoJob,
        audio_assets: list[AssetRef],
        visual_assets: list[AssetRef] | None = None,
    ) -> RenderManifest:
        if not job.script or not job.narration_manifest:
            raise InvalidTransition("缺少脚本或旁白清单")
        fps = 30
        width, height = QijiaVideoService._video_dimensions(job)
        total_frames = int(math.ceil(
            job.narration_manifest.total_duration_seconds * fps
        ))
        segments = job.script.beats
        visual_assets = list(visual_assets or [])
        timing_by_id = {
            item.segment_id: item for item in job.narration_manifest.segments
        }

        def segment_range(index: int) -> tuple[int, int]:
            segment = segments[index]
            timing = timing_by_id.get(segment.id)
            if timing:
                start_frame = max(0, int(round(timing.start_seconds * fps)))
                end_frame = min(
                    total_frames,
                    int(round(
                        (timing.start_seconds + timing.duration_seconds) * fps
                    )),
                )
            else:
                start_frame = round(total_frames * index / len(segments))
                end_frame = round(total_frames * (index + 1) / len(segments))
            if index == len(segments) - 1:
                end_frame = total_frames
            start_frame = min(start_frame, total_frames - 1)
            return start_frame, max(start_frame + 1, end_frame)

        blocks: list[VisualBlock] = []
        selected_frames: dict[str, AssetRef] = {}
        selected_media: dict[str, ShotMediaVersion] = {}
        if job.storyboard_plan:
            candidates_by_id = {
                item.candidate_id: item for item in job.first_frame_candidates
            }
            for shot in job.storyboard_plan.shots:
                candidate = candidates_by_id.get(shot.selected_candidate_id)
                if candidate and candidate.asset:
                    selected_frames[shot.shot_id] = candidate.asset
                uploaded = QijiaVideoService.shot_media_for_shot(
                    job, shot.shot_id
                )
                if uploaded:
                    selected_media[shot.shot_id] = uploaded

        video_by_shot = {
            request.request_id: asset
            for request, asset in zip(job.visual_requests, visual_assets)
        }
        if job.storyboard_plan:
            segment_index_by_id = {
                item.id: index for index, item in enumerate(segments)
            }
            planned_shots = [
                shot
                for shot in job.storyboard_plan.shots
                if all(beat_id in segment_index_by_id for beat_id in shot.beat_ids)
            ]
            coverage = Counter(
                beat_id for shot in planned_shots for beat_id in shot.beat_ids
            )
            duration_weights = [
                sum(
                    float(timing_by_id[beat_id].duration_seconds)
                    / max(1, coverage[beat_id])
                    for beat_id in shot.beat_ids
                )
                for shot in planned_shots
            ]
            total_weight = sum(duration_weights)
            boundaries = [0]
            consumed_weight = 0.0
            for weight in duration_weights[:-1]:
                consumed_weight += weight
                boundaries.append(round(
                    total_frames * consumed_weight / max(0.001, total_weight)
                ))
            boundaries.append(total_frames)
            # Rounding must never create a zero-length block.
            for index in range(1, len(boundaries)):
                boundaries[index] = max(boundaries[index], boundaries[index - 1] + 1)
            boundaries[-1] = total_frames
            for position, shot in enumerate(planned_shots):
                start_frame = boundaries[position]
                end_frame = boundaries[position + 1]
                frame_asset = selected_frames.get(shot.shot_id)
                video_asset = video_by_shot.get(shot.shot_id)
                uploaded = selected_media.get(shot.shot_id)
                if uploaded:
                    block_type = (
                        "generated_video"
                        if uploaded.media_kind == "video"
                        else "generated_image"
                    )
                    visual = uploaded.asset
                elif shot.visual_type == "video" and video_asset:
                    block_type = "generated_video"
                    visual = video_asset
                else:
                    # Image chapters deliberately stay as stills. A video chapter
                    # also falls back to its selected first frame while a paid
                    # result is being downloaded, so partial state remains valid.
                    block_type = "generated_image"
                    visual = frame_asset
                group_segments = [
                    segments[segment_index_by_id[beat_id]]
                    for beat_id in shot.beat_ids
                ]
                source_refs = list(dict.fromkeys(
                    ref for segment in group_segments for ref in segment.source_refs
                ))
                blocks.append(VisualBlock(
                    id=f"v{position + 1:02d}",
                    type=block_type,
                    shot_id=shot.shot_id,
                    start_frame=start_frame,
                    duration_in_frames=end_frame - start_frame,
                    asset_id=visual.asset_id if visual else None,
                    playback_rate=(
                        1.0
                        if block_type == "generated_video"
                        and visual
                        and visual.duration_seconds is not None
                        and visual.duration_seconds + (1 / fps)
                        >= (end_frame - start_frame) / fps
                        else None
                    ),
                    source_refs=source_refs,
                ))
        elif visual_assets:
            # Compatibility path for paid requests frozen before storyboard v1.
            target_indices = QijiaVideoService._visual_targets_for_job(job)
            available_targets = target_indices[:len(visual_assets)]
            anchors: list[int] = []
            for index in available_targets:
                start_frame, end_frame = segment_range(index)
                anchors.append((start_frame + end_frame) // 2)
            boundaries = [0]
            for left, right in zip(anchors, anchors[1:]):
                boundaries.append((left + right) // 2)
            boundaries.append(total_frames)
            for index in range(1, len(boundaries)):
                boundaries[index] = max(boundaries[index], boundaries[index - 1] + 1)
            boundaries[-1] = total_frames
            for position, (visual, segment_index) in enumerate(
                zip(visual_assets, available_targets)
            ):
                blocks.append(VisualBlock(
                    id=f"v{position + 1:02d}",
                    type="generated_video",
                    shot_id=f"shot_{position + 1:02d}",
                    start_frame=boundaries[position],
                    duration_in_frames=boundaries[position + 1] - boundaries[position],
                    asset_id=visual.asset_id,
                    source_refs=list(segments[segment_index].source_refs),
                ))
        else:
            # 旁白刚完成而 Seedance 资产尚未下载时，只持久化一个无文字占位块；
            # 该中间态不会送入最终渲染，后续每个真实资产都会替换它。
            blocks.append(VisualBlock(
                id="v01",
                type="generated_video",
                start_frame=0,
                duration_in_frames=total_frames,
            ))
        subtitles: list[SubtitleCue] = []
        for item in job.narration_manifest.segments:
            start_frame = max(0, int(round(item.start_seconds * fps)))
            end_frame = min(
                total_frames,
                int(round((item.start_seconds + item.duration_seconds) * fps)),
            )
            if start_frame >= total_frames:
                continue
            chunks = QijiaVideoService._split_subtitle_text(item.text)
            weights = [max(1, len(chunk)) for chunk in chunks]
            total_weight = sum(weights)
            span = max(1, end_frame - start_frame)
            boundaries = [start_frame]
            consumed = 0
            for weight in weights[:-1]:
                consumed += weight
                boundaries.append(start_frame + round(span * consumed / total_weight))
            boundaries.append(end_frame)
            for index, chunk in enumerate(chunks):
                cue_start = min(boundaries[index], total_frames - 1)
                cue_end = min(total_frames, max(cue_start + 1, boundaries[index + 1]))
                subtitles.append(SubtitleCue(
                    id=f"sub_{item.segment_id}_{index + 1:02d}",
                    start_frame=cue_start,
                    duration_in_frames=cue_end - cue_start,
                    text=chunk,
                ))
        screen_text_cues: list[ScreenTextCue] = []
        for index, beat in enumerate(segments, 1):
            if not beat.on_screen_text:
                continue
            segment_index = index - 1
            start_frame, end_frame = segment_range(segment_index)
            cue_start = min(total_frames - 1, start_frame + min(6, max(0, end_frame - start_frame - 1)))
            cue_end = min(end_frame, cue_start + 90)
            screen_text_cues.append(ScreenTextCue(
                id=f"screen_{beat.id}",
                start_frame=cue_start,
                duration_in_frames=max(1, cue_end - cue_start),
                text=beat.on_screen_text,
                kind=(
                    "headline" if beat.role == "hook"
                    else "closing" if beat.role == "closing"
                    else "emphasis"
                ),
            ))
        cover_asset = None
        if job.storyboard_plan and job.storyboard_plan.shots:
            first_shot_id = job.storyboard_plan.shots[0].shot_id
            first_upload = selected_media.get(first_shot_id)
            cover_asset = (
                first_upload.asset
                if first_upload and first_upload.media_kind == "image"
                else selected_frames.get(first_shot_id)
            )
        required_visual_asset_ids = {
            block.asset_id for block in blocks if block.asset_id
        }
        if cover_asset:
            required_visual_asset_ids.add(cover_asset.asset_id)
        render_visual_assets = [
            item
            for item in [
                *visual_assets,
                *selected_frames.values(),
                *(version.asset for version in selected_media.values()),
            ]
            if item.asset_id in required_visual_asset_ids
        ]
        assets_by_id = {
            item.asset_id: item
            for item in [*audio_assets, *render_visual_assets]
        }
        return RenderManifest(
            job_id=job.id,
            width=width,
            height=height,
            duration_in_frames=total_frames,
            video_title=job.script.video_title,
            cover_text=job.script.cover_text,
            assets=list(assets_by_id.values()),
            cover_asset_id=cover_asset.asset_id if cover_asset else "",
            audio_asset_id=job.narration_manifest.full_audio_asset_id,
            visual_blocks=blocks,
            subtitle_cues=subtitles,
            screen_text_cues=screen_text_cues,
        )

    async def validate_shot_action(
        self,
        job_id: str,
        shot_id: str,
        expected_revision: int,
        actor: Actor,
        *,
        version_id: str = "",
        first_frame_candidate_id: str = "",
    ) -> str:
        """Validate the user-visible revision before a background shot edit."""

        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.FINAL_REVIEW_REQUIRED:
            raise InvalidTransition("只有待确认成片可以调整单个 AI 镜头")
        request = next(
            (item for item in job.visual_requests if item.request_id == shot_id),
            None,
        )
        if not request:
            raise InvalidTransition("指定的 AI 镜头不存在")
        if first_frame_candidate_id:
            candidate = next(
                (
                    item
                    for item in job.first_frame_candidates
                    if item.shot_id == shot_id
                    and item.candidate_id == first_frame_candidate_id
                    and item.asset
                ),
                None,
            )
            if not candidate:
                raise InvalidTransition("指定的首帧候选不存在或尚未就绪")
        if version_id:
            version = next(
                (
                    item
                    for item in job.visual_versions
                    if item.shot_id == shot_id
                    and item.version_id == version_id
                ),
                None,
            )
            if (
                not version
                or not version.asset
                or version.task.state != ProviderTaskState.SUCCEEDED
            ):
                raise InvalidTransition("该镜头版本尚不可用于成片")
            if version.request.fingerprint() == request.fingerprint():
                raise InvalidTransition("该镜头版本已经用于当前成片")
        return request.fingerprint()

    async def validate_shot_media_action(
        self,
        job_id: str,
        shot_id: str,
        expected_revision: int,
        actor: Actor,
        *,
        media_id: str = "",
        restore_generated: bool = False,
    ) -> str:
        """Validate an upload/restore request before accepting a large file."""

        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state not in {
            JobState.MEDIA_REVIEW_REQUIRED,
            JobState.FINAL_REVIEW_REQUIRED,
        }:
            raise InvalidTransition("只有素材安排或成片确认阶段可以调整镜头素材")
        pre_generation = job.state == JobState.MEDIA_REVIEW_REQUIRED
        shot = next(
            (
                item
                for item in (job.storyboard_plan.shots if job.storyboard_plan else [])
                if item.shot_id == shot_id
            ),
            None,
        )
        if not shot:
            raise InvalidTransition("指定的分镜不存在")
        current_media_id = shot.selected_media_id
        if media_id:
            version = self.shot_media_for_shot(
                job, shot_id, media_id=media_id
            )
            if not version:
                raise InvalidTransition("指定的上传素材版本不存在")
            if current_media_id == media_id:
                raise InvalidTransition("该上传素材已经用于当前镜头")
        if restore_generated:
            if not current_media_id:
                raise InvalidTransition("当前镜头已经在使用 AI 素材")
            if pre_generation:
                return current_media_id
            if shot.visual_type == "video":
                generated = self._generated_visual_asset_for_shot(job, shot_id)
            else:
                generated = next(
                    (
                        item.asset
                        for item in job.first_frame_candidates
                        if item.shot_id == shot_id
                        and item.candidate_id == shot.selected_candidate_id
                        and item.asset
                    ),
                    None,
                )
            if not generated:
                raise InvalidTransition("这个镜头没有可恢复的 AI 素材")
        return current_media_id

    async def stage_shot_media_selection(
        self,
        job_id: str,
        shot_id: str,
        media_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> VideoJob:
        """Stage one existing upload or AI restore without rendering the film."""

        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state not in {
            JobState.MEDIA_REVIEW_REQUIRED,
            JobState.FINAL_REVIEW_REQUIRED,
        }:
            raise InvalidTransition("只有素材安排或成片确认阶段可以选择镜头素材")
        pre_generation = job.state == JobState.MEDIA_REVIEW_REQUIRED
        shot = next(
            (
                item
                for item in (job.storyboard_plan.shots if job.storyboard_plan else [])
                if item.shot_id == shot_id
            ),
            None,
        )
        if not shot:
            raise InvalidTransition("指定的分镜不存在")
        existing_pending = self.pending_shot_media_edit_for_shot(job, shot_id)
        if media_id:
            if not self.shot_media_for_shot(job, shot_id, media_id=media_id):
                raise InvalidTransition("指定的上传素材版本不存在")
        elif not pre_generation:
            generated_candidate = job.model_copy(deep=True)
            generated_shot = next(
                item
                for item in generated_candidate.storyboard_plan.shots
                if item.shot_id == shot_id
            )
            generated_shot.selected_media_id = ""
            if not self.visual_asset_for_shot(generated_candidate, shot_id):
                raise InvalidTransition("这个镜头没有可恢复的 AI 素材")

        if pre_generation:
            if media_id == shot.selected_media_id:
                raise InvalidTransition(
                    "该上传素材已经选中"
                    if media_id
                    else "当前镜头已经设置为使用 AI 生成"
                )
            shot.selected_media_id = media_id
            job.pending_shot_media_edits = [
                item
                for item in job.pending_shot_media_edits
                if item.shot_id != shot_id
            ]
            job.error = ""
            return await self._save_job(job, actor)

        if media_id == shot.selected_media_id:
            if not existing_pending:
                raise InvalidTransition("该素材已经用于当前成片")
            job.pending_shot_media_edits = [
                item
                for item in job.pending_shot_media_edits
                if item.shot_id != shot_id
            ]
        elif existing_pending and existing_pending.media_id == media_id:
            raise InvalidTransition("该素材已经加入待应用修改")
        else:
            self._stage_pending_shot_media_edit(job, shot_id, media_id, actor)
        job.error = ""
        return await self._save_job(job, actor)

    async def discard_pending_shot_media_edits(
        self,
        job_id: str,
        expected_revision: int,
        actor: Actor,
        *,
        shot_id: str = "",
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.FINAL_REVIEW_REQUIRED:
            raise InvalidTransition("只有待确认成片可以撤销待应用修改")
        original_count = len(job.pending_shot_media_edits)
        job.pending_shot_media_edits = [
            item
            for item in job.pending_shot_media_edits
            if shot_id and item.shot_id != shot_id
        ] if shot_id else []
        if len(job.pending_shot_media_edits) == original_count:
            raise InvalidTransition("没有可撤销的待应用素材修改")
        job.error = ""
        return await self._save_job(job, actor)

    async def validate_pending_shot_media_action(
        self,
        job_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> str:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.FINAL_REVIEW_REQUIRED:
            raise InvalidTransition("只有待确认成片可以应用镜头素材修改")
        if not job.pending_shot_media_edits:
            raise InvalidTransition("当前没有待应用的镜头素材修改")
        shot_ids = [item.shot_id for item in job.pending_shot_media_edits]
        if len(shot_ids) != len(set(shot_ids)):
            raise QualityGateFailed("待应用镜头素材存在重复记录")
        known_shots = {
            item.shot_id
            for item in (job.storyboard_plan.shots if job.storyboard_plan else [])
        }
        for item in job.pending_shot_media_edits:
            if item.shot_id not in known_shots:
                raise QualityGateFailed("待应用镜头素材引用了不存在的分镜")
            if item.media_id and not self.shot_media_for_shot(
                job,
                item.shot_id,
                media_id=item.media_id,
            ):
                raise QualityGateFailed("待应用镜头素材版本不存在")
        return self.pending_shot_media_fingerprint(job)

    async def mark_shot_edit_failed(
        self,
        job_id: str,
        error: str,
        actor: Actor,
    ) -> VideoJob:
        """Restore the last reviewable draft after an isolated worker crash."""

        job = await self.get_job(job_id, actor)
        if job.state == JobState.PRODUCING and job.review_bundle_hash:
            job.state = JobState.FINAL_REVIEW_REQUIRED
        job.error = str(error or "单镜头调整失败，原成片未受影响")[:2000]
        return await self._save_job(job, actor)

    async def _render_shot_edit_candidate(
        self,
        original_job: VideoJob,
        candidate: VideoJob,
        actor: Actor,
        *,
        version_id: str,
        shot_id: str,
        workspace: Path,
        progress: ProgressReporter | None,
        ready_message: str,
    ) -> VideoJob:
        if not candidate.render_manifest or not candidate.narration_manifest:
            raise QualityGateFailed("更新后的镜头缺少可渲染时间线")
        candidate.review_bundle_hash = ""
        candidate.approvals = [
            item for item in candidate.approvals if item.kind != "final"
        ]
        self._report(
            progress,
            message=ready_message,
            stage="remotion",
            percent=78,
            workflow="shot_edit",
            shot_id=shot_id,
        )
        raw_draft_path = await self.renderer.render(
            candidate.render_manifest, self.storage, workspace
        )
        draft_path = await self.media_packager.normalize(
            raw_draft_path, workspace / "draft.normalized.mp4"
        )
        self._report(
            progress,
            message="成片已更新，正在检查文件完整性…",
            stage="quality",
            percent=86,
            workflow="shot_edit",
            shot_id=shot_id,
        )
        report = await self.quality_checker.inspect(
            draft_path, candidate.render_manifest
        )
        candidate.quality_report = report
        if report.automatic_status != "review_ready":
            failed_checks = [
                f"{item.get('id') or 'unknown'}={item.get('detail', '')}"
                for item in report.checks
                if not bool(item.get("passed"))
            ]
            raise QualityGateFailed(
                "更新后的成片检查未通过：" + "、".join(failed_checks)
            )

        object_prefix = (
            f"qijia-video/{original_job.id}/renders/shot-edits/"
            f"r{original_job.revision + 1}-{version_id}"
        )
        draft_asset = await self.storage.put_file(
            object_key=f"{object_prefix}/draft.mp4",
            path=draft_path,
            asset_id=(
                f"draft_video_{version_id}_r{original_job.revision + 1}"
            ),
            media_type="video/mp4",
            duration_seconds=candidate.narration_manifest.total_duration_seconds,
        )
        candidate.artifacts = [Artifact(name="draft.mp4", asset=draft_asset)]
        cover_path = await self.renderer.render_cover(
            candidate.render_manifest, self.storage, workspace
        )
        cover_asset = await self.storage.put_file(
            object_key=f"{object_prefix}/cover.jpg",
            path=cover_path,
            asset_id=f"cover_{version_id}_r{original_job.revision + 1}",
            media_type="image/jpeg",
        )
        candidate.artifacts.append(Artifact(name="cover.jpg", asset=cover_asset))
        for name, path, media_type in await self._write_support_files(
            candidate, workspace
        ):
            support_asset = await self.storage.put_file(
                object_key=f"{object_prefix}/{name}",
                path=path,
                asset_id=(
                    f"{name.replace('.', '_')}_{version_id}_"
                    f"r{original_job.revision + 1}"
                ),
                media_type=media_type,
            )
            candidate.artifacts.append(Artifact(name=name, asset=support_asset))
        candidate.review_bundle_hash = self._review_hash(candidate.artifacts)
        candidate.state = JobState.FINAL_REVIEW_REQUIRED
        candidate.failed_stage = ""
        candidate.error = ""
        saved = await self._save_job(candidate, actor)
        self._report(
            progress,
            message="镜头素材和成片已更新，等待你确认…",
            stage="confirm_final",
            percent=90,
            workflow="shot_edit",
            shot_id=shot_id,
        )
        return saved

    async def prepare_shot_media(
        self,
        job_id: str,
        shot_id: str,
        raw_asset: AssetRef,
        media_kind: str,
        media_id: str,
        original_filename: str,
        expected_selected_media_id: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        """Validate and normalize one upload without rebuilding the film."""

        job = await self.get_job(job_id, actor)
        if job.state not in {
            JobState.MEDIA_REVIEW_REQUIRED,
            JobState.FINAL_REVIEW_REQUIRED,
        }:
            raise InvalidTransition("只有素材安排或成片确认阶段可以准备镜头素材")
        pre_generation = job.state == JobState.MEDIA_REVIEW_REQUIRED
        shot = next(
            (
                item
                for item in (job.storyboard_plan.shots if job.storyboard_plan else [])
                if item.shot_id == shot_id
            ),
            None,
        )
        if not shot:
            raise InvalidTransition("指定的分镜不存在")
        if shot.selected_media_id != expected_selected_media_id:
            raise RevisionConflict("该镜头素材已被其他操作更新，请刷新后重试")
        if media_kind not in {"image", "video"}:
            raise QualityGateFailed("上传素材类型不受支持")
        if not raw_asset.media_type.startswith(f"{media_kind}/"):
            raise QualityGateFailed("上传素材类型与文件内容不一致")
        existing = self.shot_media_for_shot(job, shot_id, media_id=media_id)
        if existing:
            if pre_generation and shot.selected_media_id == media_id:
                return job
            pending = self.pending_shot_media_edit_for_shot(job, shot_id)
            if pending and pending.media_id == media_id:
                return job
            raise RevisionConflict("该上传素材版本已经存在")

        workspace = Path(tempfile.mkdtemp(
            prefix=f"{job.id}-upload-prepare-", dir=self.work_root
        ))
        try:
            self._report(
                progress,
                message=(
                    "正在标准化上传视频并匹配镜头时长…"
                    if media_kind == "video"
                    else "正在检查上传图片并准备暂存…"
                ),
                stage="media_prepare",
                percent=64,
                workflow="shot_edit",
                shot_id=shot_id,
            )
            source_path = await self.storage.materialize(
                raw_asset, workspace / "source.upload"
            )
            if source_path.stat().st_size != raw_asset.size_bytes:
                raise QualityGateFailed("上传素材大小与确认信息不一致")
            try:
                actual_kind, _, actual_media_type = detect_shot_media_format(
                    source_path
                )
            except ValueError as exc:
                raise QualityGateFailed(str(exc)) from exc
            iso_video_types = {"video/mp4", "video/quicktime"}
            compatible_media_type = (
                actual_media_type == raw_asset.media_type
                or {actual_media_type, raw_asset.media_type} <= iso_video_types
            )
            if actual_kind != media_kind or not compatible_media_type:
                raise QualityGateFailed("上传素材类型与文件内容不一致")

            if media_kind == "video":
                destination = workspace / "uploaded.timeline.mp4"
                prepare_upload = getattr(
                    self.media_packager,
                    "prepare_uploaded_video_for_timeline",
                    None,
                )
                chapter_duration = self._shot_chapter_duration_seconds(
                    job, shot_id, 8
                )
                if callable(prepare_upload):
                    prepared_path, actual_duration = await prepare_upload(
                        source_path,
                        destination,
                        chapter_duration_seconds=chapter_duration,
                    )
                else:
                    prepared_path, actual_duration = (
                        await self.media_packager.prepare_video_for_timeline(
                            source_path,
                            destination,
                            minimum_duration_seconds=chapter_duration,
                        )
                    )
                media_type = "video/mp4"
                extension = ".mp4"
                duration_seconds = round(actual_duration, 3)
            else:
                prepared_path = source_path
                media_type = raw_asset.media_type
                extension = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                }.get(media_type, ".image")
                duration_seconds = None

            asset = await self.storage.put_file(
                object_key=(
                    f"qijia-video/{job.id}/uploads/{shot_id}/"
                    f"{media_id}{extension}"
                ),
                path=prepared_path,
                asset_id=f"shot_media_{media_id}",
                media_type=media_type,
                duration_seconds=duration_seconds,
            )
            candidate = job.model_copy(deep=True)
            version_number = 1 + max(
                (
                    item.version
                    for item in candidate.shot_media_versions
                    if item.shot_id == shot_id
                ),
                default=0,
            )
            candidate.shot_media_versions.append(ShotMediaVersion(
                media_id=media_id,
                shot_id=shot_id,
                version=version_number,
                media_kind=media_kind,
                asset=asset,
                original_filename=str(original_filename or "")[:255],
                created_by=actor.username,
                created_at=timestamp(),
            ))
            if pre_generation:
                candidate_shot = next(
                    item
                    for item in candidate.storyboard_plan.shots
                    if item.shot_id == shot_id
                )
                candidate_shot.selected_media_id = media_id
                candidate.pending_shot_media_edits = [
                    item
                    for item in candidate.pending_shot_media_edits
                    if item.shot_id != shot_id
                ]
            else:
                self._stage_pending_shot_media_edit(
                    candidate,
                    shot_id,
                    media_id,
                    actor,
                )
            candidate.error = ""
            saved = await self._save_job(candidate, actor)
            self._report(
                progress,
                message=(
                    "素材已加入生成前安排，可继续处理其他镜头"
                    if pre_generation
                    else "素材已暂存，可继续替换其他镜头或一次应用全部修改"
                ),
                stage="media_staged",
                percent=72,
                workflow="shot_edit",
                shot_id=shot_id,
                pending_count=len(saved.pending_shot_media_edits),
            )
            return saved
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            latest.error = (
                (
                    "上传素材未能加入生成前安排，尚未产生 AI 画面："
                    if pre_generation
                    else "上传素材未能暂存，当前成片未受影响："
                )
                + str(exc)
            )[:2000]
            await self._save_job(latest, actor)
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def apply_pending_shot_media(
        self,
        job_id: str,
        expected_pending_fingerprint: str,
        batch_id: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        """Atomically apply every staged media edit and render the film once."""

        if not re.fullmatch(r"[A-Za-z0-9_-]{1,96}", str(batch_id or "")):
            raise QualityGateFailed("批量镜头修改 ID 无效")
        job = await self.get_job(job_id, actor)
        batch_marker = f"-{batch_id}/"
        if (
            job.state == JobState.FINAL_REVIEW_REQUIRED
            and any(batch_marker in item.asset.object_key for item in job.artifacts)
        ):
            return job
        resuming = job.state == JobState.PRODUCING and bool(job.review_bundle_hash)
        if job.state != JobState.FINAL_REVIEW_REQUIRED and not resuming:
            raise InvalidTransition("只有待确认成片可以应用镜头素材修改")
        if not job.pending_shot_media_edits:
            raise InvalidTransition("当前没有待应用的镜头素材修改")
        if self.pending_shot_media_fingerprint(job) != expected_pending_fingerprint:
            raise RevisionConflict("待应用镜头素材已经变化，请刷新后重试")

        edits = list(job.pending_shot_media_edits)
        workspace = Path(tempfile.mkdtemp(
            prefix=f"{job.id}-upload-batch-", dir=self.work_root
        ))
        try:
            if not resuming:
                self._remember_current_visuals(job, actor)
                job.state = JobState.PRODUCING
                job.error = ""
                job = await self._save_job(job, actor)
            candidate = job.model_copy(deep=True)
            shots_by_id = {
                item.shot_id: item
                for item in (
                    candidate.storyboard_plan.shots
                    if candidate.storyboard_plan
                    else []
                )
            }
            for edit in edits:
                shot = shots_by_id.get(edit.shot_id)
                if not shot:
                    raise QualityGateFailed("待应用镜头素材引用了不存在的分镜")
                if edit.media_id and not self.shot_media_for_shot(
                    candidate,
                    edit.shot_id,
                    media_id=edit.media_id,
                ):
                    raise QualityGateFailed("待应用镜头素材版本不存在")
                shot.selected_media_id = edit.media_id

            audio_assets = self._narration_assets(candidate)
            if not audio_assets:
                raise QualityGateFailed("原成片缺少可复用的旁白资产")
            selected_assets: list[AssetRef] = []
            for request in candidate.visual_requests:
                selected = self.visual_asset_for_shot(
                    candidate,
                    request.request_id,
                )
                if not selected:
                    raise QualityGateFailed(
                        f"镜头 {request.request_id} 缺少可复用素材"
                    )
                selected_assets.append(selected)
            candidate.render_manifest = self._build_render_manifest(
                candidate,
                audio_assets,
                selected_assets,
            )
            candidate.pending_shot_media_edits = []
            return await self._render_shot_edit_candidate(
                job,
                candidate,
                actor,
                version_id=batch_id,
                shot_id=edits[0].shot_id,
                workspace=workspace,
                progress=progress,
                ready_message=(
                    f"{len(edits)} 处镜头修改已准备，正在用 Remotion 一次更新成片…"
                ),
            )
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            if latest.state == JobState.PRODUCING and latest.review_bundle_hash:
                latest.state = JobState.FINAL_REVIEW_REQUIRED
            latest.error = (
                f"批量镜头修改未应用，原成片和待应用修改均已保留：{exc}"
            )[:2000]
            await self._save_job(latest, actor)
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def replace_shot_media(
        self,
        job_id: str,
        shot_id: str,
        raw_asset: AssetRef,
        media_kind: str,
        media_id: str,
        original_filename: str,
        expected_selected_media_id: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        resuming = job.state == JobState.PRODUCING and bool(job.review_bundle_hash)
        if job.state != JobState.FINAL_REVIEW_REQUIRED and not resuming:
            raise InvalidTransition("只有待确认成片可以替换单个镜头素材")
        shot = next(
            (
                item
                for item in (job.storyboard_plan.shots if job.storyboard_plan else [])
                if item.shot_id == shot_id
            ),
            None,
        )
        if not shot:
            raise InvalidTransition("指定的分镜不存在")
        if shot.selected_media_id != expected_selected_media_id:
            raise RevisionConflict("该镜头素材已被其他操作更新，请刷新后重试")
        if media_kind not in {"image", "video"}:
            raise QualityGateFailed("上传素材类型不受支持")
        if not raw_asset.media_type.startswith(f"{media_kind}/"):
            raise QualityGateFailed("上传素材类型与文件内容不一致")
        if self.shot_media_for_shot(job, shot_id, media_id=media_id):
            raise RevisionConflict("该上传素材版本已经存在")

        workspace = Path(tempfile.mkdtemp(
            prefix=f"{job.id}-upload-", dir=self.work_root
        ))
        try:
            if not resuming:
                self._remember_current_visuals(job, actor)
                job.state = JobState.PRODUCING
                job.error = ""
                job = await self._save_job(job, actor)
            self._report(
                progress,
                message=(
                    "正在标准化上传视频并匹配镜头时长…"
                    if media_kind == "video"
                    else "正在检查上传图片并准备镜头…"
                ),
                stage="media_prepare",
                percent=68,
                workflow="shot_edit",
                shot_id=shot_id,
            )
            source_path = await self.storage.materialize(
                raw_asset, workspace / "source.upload"
            )
            if source_path.stat().st_size != raw_asset.size_bytes:
                raise QualityGateFailed("上传素材大小与确认信息不一致")
            try:
                actual_kind, _, actual_media_type = detect_shot_media_format(
                    source_path
                )
            except ValueError as exc:
                raise QualityGateFailed(str(exc)) from exc
            iso_video_types = {"video/mp4", "video/quicktime"}
            compatible_media_type = (
                actual_media_type == raw_asset.media_type
                or {
                    actual_media_type,
                    raw_asset.media_type,
                } <= iso_video_types
            )
            if actual_kind != media_kind or not compatible_media_type:
                raise QualityGateFailed("上传素材类型与文件内容不一致")
            if media_kind == "video":
                destination = workspace / "uploaded.timeline.mp4"
                prepare_upload = getattr(
                    self.media_packager,
                    "prepare_uploaded_video_for_timeline",
                    None,
                )
                chapter_duration = self._shot_chapter_duration_seconds(
                    job, shot_id, 8
                )
                if callable(prepare_upload):
                    prepared_path, actual_duration = await prepare_upload(
                        source_path,
                        destination,
                        chapter_duration_seconds=chapter_duration,
                    )
                else:
                    prepared_path, actual_duration = (
                        await self.media_packager.prepare_video_for_timeline(
                            source_path,
                            destination,
                            minimum_duration_seconds=chapter_duration,
                        )
                    )
                media_type = "video/mp4"
                extension = ".mp4"
                duration_seconds = round(actual_duration, 3)
            else:
                prepared_path = source_path
                media_type = raw_asset.media_type
                extension = {
                    "image/jpeg": ".jpg",
                    "image/png": ".png",
                    "image/webp": ".webp",
                }.get(media_type, ".image")
                duration_seconds = None
            asset = await self.storage.put_file(
                object_key=(
                    f"qijia-video/{job.id}/uploads/{shot_id}/"
                    f"{media_id}{extension}"
                ),
                path=prepared_path,
                asset_id=f"shot_media_{media_id}",
                media_type=media_type,
                duration_seconds=duration_seconds,
            )

            candidate = job.model_copy(deep=True)
            version_number = 1 + max(
                (
                    item.version
                    for item in candidate.shot_media_versions
                    if item.shot_id == shot_id
                ),
                default=0,
            )
            candidate.shot_media_versions.append(ShotMediaVersion(
                media_id=media_id,
                shot_id=shot_id,
                version=version_number,
                media_kind=media_kind,
                asset=asset,
                original_filename=str(original_filename or "")[:255],
                created_by=actor.username,
                created_at=timestamp(),
            ))
            candidate_shot = next(
                item
                for item in candidate.storyboard_plan.shots
                if item.shot_id == shot_id
            )
            candidate_shot.selected_media_id = media_id
            audio_assets = self._narration_assets(job)
            if not audio_assets:
                raise QualityGateFailed("原成片缺少可复用的旁白资产")
            selected_assets: list[AssetRef] = []
            for request in candidate.visual_requests:
                selected = self.visual_asset_for_shot(
                    job, request.request_id
                )
                if not selected:
                    raise QualityGateFailed(
                        f"AI 镜头 {request.request_id} 缺少可复用视频资产"
                    )
                selected_assets.append(selected)
            candidate.render_manifest = self._build_render_manifest(
                candidate, audio_assets, selected_assets
            )
            return await self._render_shot_edit_candidate(
                job,
                candidate,
                actor,
                version_id=media_id,
                shot_id=shot_id,
                workspace=workspace,
                progress=progress,
                ready_message="上传素材已就绪，正在用 Remotion 更新成片…",
            )
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            if latest.state == JobState.PRODUCING and latest.review_bundle_hash:
                latest.state = JobState.FINAL_REVIEW_REQUIRED
            latest.error = (
                f"上传素材未替换成片，原成片仍然保留：{exc}"
            )[:2000]
            await self._save_job(latest, actor)
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def select_shot_media(
        self,
        job_id: str,
        shot_id: str,
        media_id: str,
        expected_selected_media_id: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        resuming = job.state == JobState.PRODUCING and bool(job.review_bundle_hash)
        if job.state != JobState.FINAL_REVIEW_REQUIRED and not resuming:
            raise InvalidTransition("只有待确认成片可以切换镜头素材")
        shot = next(
            (
                item
                for item in (job.storyboard_plan.shots if job.storyboard_plan else [])
                if item.shot_id == shot_id
            ),
            None,
        )
        if not shot:
            raise InvalidTransition("指定的分镜不存在")
        if shot.selected_media_id != expected_selected_media_id:
            raise RevisionConflict("该镜头素材已被其他操作更新，请刷新后重试")
        selected_upload = (
            self.shot_media_for_shot(job, shot_id, media_id=media_id)
            if media_id
            else None
        )
        if media_id and not selected_upload:
            raise InvalidTransition("指定的上传素材版本不存在")
        if media_id == shot.selected_media_id:
            raise InvalidTransition("该素材已经用于当前成片")

        workspace = Path(tempfile.mkdtemp(
            prefix=f"{job.id}-media-select-", dir=self.work_root
        ))
        try:
            if not resuming:
                self._remember_current_visuals(job, actor)
                job.state = JobState.PRODUCING
                job.error = ""
                job = await self._save_job(job, actor)
            candidate = job.model_copy(deep=True)
            candidate_shot = next(
                item
                for item in candidate.storyboard_plan.shots
                if item.shot_id == shot_id
            )
            candidate_shot.selected_media_id = media_id
            audio_assets = self._narration_assets(job)
            if not audio_assets:
                raise QualityGateFailed("原成片缺少可复用的旁白资产")
            selected_assets: list[AssetRef] = []
            for request in candidate.visual_requests:
                selected = (
                    self._generated_visual_asset_for_shot(
                        job, request.request_id
                    )
                    if not media_id and request.request_id == shot_id
                    else self.visual_asset_for_shot(job, request.request_id)
                )
                if not selected:
                    raise QualityGateFailed(
                        f"AI 镜头 {request.request_id} 缺少可复用视频资产"
                    )
                selected_assets.append(selected)
            candidate.render_manifest = self._build_render_manifest(
                candidate, audio_assets, selected_assets
            )
            label = (
                f"上传素材 v{selected_upload.version}"
                if selected_upload
                else "AI 素材"
            )
            return await self._render_shot_edit_candidate(
                job,
                candidate,
                actor,
                version_id=media_id or f"generated_{shot_id}",
                shot_id=shot_id,
                workspace=workspace,
                progress=progress,
                ready_message=f"正在把{label}应用到成片…",
            )
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            if latest.state == JobState.PRODUCING and latest.review_bundle_hash:
                latest.state = JobState.FINAL_REVIEW_REQUIRED
            latest.error = (
                f"镜头素材未切换，原成片仍然保留：{exc}"
            )[:2000]
            await self._save_job(latest, actor)
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def _render_shot_selection(
        self,
        job: VideoJob,
        actor: Actor,
        *,
        request: VisualGenerationRequest,
        task: ProviderTask,
        asset: AssetRef,
        version_id: str,
        workspace: Path,
        progress: ProgressReporter | None,
    ) -> VideoJob:
        audio_assets = self._narration_assets(job)
        if not audio_assets:
            raise QualityGateFailed("原成片缺少可复用的旁白资产")

        candidate = job.model_copy(deep=True)
        candidate.visual_requests = [
            request if item.request_id == request.request_id else item
            for item in candidate.visual_requests
        ]
        storyboard_shot = (
            next(
                (
                    item
                    for item in candidate.storyboard_plan.shots
                    if item.shot_id == request.request_id
                ),
                None,
            )
            if candidate.storyboard_plan
            else None
        )
        if storyboard_shot:
            storyboard_shot.selected_media_id = ""
        if candidate.storyboard_plan and request.first_frame_asset_id:
            selected_frame = next(
                (
                    item
                    for item in candidate.first_frame_candidates
                    if item.shot_id == request.request_id
                    and item.asset
                    and item.asset.asset_id == request.first_frame_asset_id
                ),
                None,
            )
            if selected_frame:
                if storyboard_shot:
                    storyboard_shot.selected_candidate_id = (
                        selected_frame.candidate_id
                    )
        self._replace_video_task(candidate, task)
        selected_assets: list[AssetRef] = []
        for item in candidate.visual_requests:
            selected = (
                asset
                if item.request_id == request.request_id
                else self.visual_asset_for_shot(job, item.request_id)
            )
            if not selected:
                raise QualityGateFailed(
                    f"AI 镜头 {item.request_id} 缺少可复用视频资产"
                )
            selected_assets.append(selected)
        candidate.render_manifest = self._build_render_manifest(
            candidate, audio_assets, selected_assets
        )
        return await self._render_shot_edit_candidate(
            job,
            candidate,
            actor,
            version_id=version_id,
            shot_id=request.request_id,
            workspace=workspace,
            progress=progress,
            ready_message="新镜头已就绪，正在用 Remotion 更新成片…",
        )

    async def regenerate_shot(
        self,
        job_id: str,
        shot_id: str,
        revision_intent: str,
        expected_selected_fingerprint: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
        first_frame_candidate_id: str = "",
        seedance_model: str = "",
        _legacy_compiled_prompt: str = "",
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        resuming = job.state == JobState.PRODUCING and bool(job.review_bundle_hash)
        if job.state != JobState.FINAL_REVIEW_REQUIRED and not resuming:
            raise InvalidTransition("只有待确认成片可以重新生成单个镜头")
        current = next(
            (item for item in job.visual_requests if item.request_id == shot_id),
            None,
        )
        if not current:
            raise InvalidTransition("指定的 AI 镜头不存在")
        if current.fingerprint() != expected_selected_fingerprint:
            raise RevisionConflict("该镜头已切换版本，请刷新后重试")
        legacy_prompt = str(_legacy_compiled_prompt or "").strip()
        if legacy_prompt:
            if len(legacy_prompt) > 4000:
                raise QualityGateFailed("历史镜头提示词不能超过 4000 个字符")
            cleaned_intent = ""
            compiled_prompt = legacy_prompt
        else:
            cleaned_intent = str(revision_intent or "").strip()
            if not cleaned_intent:
                raise QualityGateFailed("镜头修改意图不能为空")
            if len(cleaned_intent) > 600:
                raise QualityGateFailed("镜头修改意图不能超过 600 个字符")
            compiled_prompt = self._compile_shot_revision_prompt(
                job,
                shot_id,
                cleaned_intent,
                duration_seconds=current.duration_seconds,
            )
        requested_model = str(seedance_model or "").strip()
        if not requested_model:
            requested_model = self._seedance_model_for_request(job, current)
        if requested_model not in {
            SEEDANCE_EFFICIENT_MODEL,
            SEEDANCE_BALANCED_MODEL,
            SEEDANCE_FLAGSHIP_MODEL,
        }:
            raise QualityGateFailed("不支持的 Seedance 生成模型")
        requested_frame = None
        if first_frame_candidate_id:
            requested_frame = next(
                (
                    item
                    for item in job.first_frame_candidates
                    if item.shot_id == shot_id
                    and item.candidate_id == first_frame_candidate_id
                    and item.asset
                ),
                None,
            )
            if not requested_frame or not requested_frame.asset:
                raise InvalidTransition("指定的首帧候选不存在或尚未就绪")
        requested_frame_asset_id = (
            requested_frame.asset.asset_id
            if requested_frame and requested_frame.asset
            else current.first_frame_asset_id
        )

        shot_number = next(
            index
            for index, item in enumerate(job.visual_requests, 1)
            if item.request_id == shot_id
        )
        stage = f"seedance_shot_{shot_number}"
        workspace = Path(tempfile.mkdtemp(prefix=f"{job.id}-shot-", dir=self.work_root))
        try:
            if resuming:
                version = max(
                    (
                        item
                        for item in job.visual_versions
                        if item.shot_id == shot_id
                        and item.request.prompt == compiled_prompt
                        and item.request.revision_intent == cleaned_intent
                        and item.request.first_frame_asset_id
                        == requested_frame_asset_id
                        and self._seedance_model_for_request(job, item.request)
                        == requested_model
                        and item.request.fingerprint() != current.fingerprint()
                        and item.task.state not in (
                            ProviderTaskState.FAILED,
                            ProviderTaskState.CANCELLED,
                        )
                    ),
                    key=lambda item: item.version,
                    default=None,
                )
                if not version:
                    raise ProviderUnavailable(
                        "无法确认上次单镜头提交结果；为避免重复扣费，本次不会自动重提"
                    )
                request = version.request
                task = version.task
                self._record_video_task_usage(job, task, version.task)
                version = self._remember_visual_version(
                    job, request, task, version.asset, actor
                )
                job = await self._save_job(job, actor)
            else:
                self._remember_current_visuals(job, actor)
                job.state = JobState.PRODUCING
                job.error = ""
                job = await self._save_job(job, actor)
                known_fingerprints = {
                    item.request.fingerprint()
                    for item in job.visual_versions
                    if item.shot_id == shot_id
                }
                while True:
                    request = current.model_copy(update={
                        "prompt": compiled_prompt,
                        "revision_intent": cleaned_intent,
                        "model_id": requested_model,
                        "seed": secrets.randbits(32),
                        "first_frame_asset_id": requested_frame_asset_id,
                    })
                    if request.fingerprint() not in known_fingerprints:
                        break
                self._report(
                    progress,
                    message=(
                        f"正在提交 AI 镜头 {shot_number}/"
                        f"{len(job.visual_requests)} 的新版本…"
                    ),
                    stage=stage,
                    percent=48,
                    workflow="shot_edit",
                    shot=shot_number,
                    shot_count=len(job.visual_requests),
                    shot_id=shot_id,
                )
                first_frame_url = ""
                if request.first_frame_asset_id:
                    frame_candidate = next(
                        (
                            item
                            for item in job.first_frame_candidates
                            if item.asset
                            and item.asset.asset_id == request.first_frame_asset_id
                        ),
                        None,
                    )
                    if not frame_candidate or not frame_candidate.asset:
                        raise QualityGateFailed("镜头首帧资产不存在")
                    first_frame_url = await self.storage.signed_get_url(
                        frame_candidate.asset, expires=3600
                    )
                try:
                    task = await self.video_provider.submit(
                        request, first_frame_url=first_frame_url
                    )
                except ProviderUnavailable:
                    job = await self._persist_usage_record(
                        job,
                        ProviderUsageRecord(
                            usage_id=(
                                f"usage_seedance_attempt_{uuid.uuid4().hex}"
                            ),
                            operation="seedance_video",
                            provider=self.video_provider.name,
                            model_id=(
                                request.model_id
                                or str(
                                    getattr(self.video_provider, "model", "") or ""
                                )
                            ),
                            request_id=request.request_id,
                            succeeded=False,
                            quantity=1,
                            unit="video",
                            note=(
                                "视频生成提交失败或结果未知，是否计费需与"
                                "火山方舟账单核对"
                            ),
                            occurred_at=timestamp(),
                        ),
                        actor,
                    )
                    raise
                if task.request_fingerprint != request.fingerprint():
                    raise ProviderUnavailable("视频 Provider 返回了错误的请求指纹")
                task.request_id = shot_id
                self._record_video_task_usage(job, task)
                version = self._remember_visual_version(
                    job, request, task, None, actor
                )
                job = await self._save_job(job, actor)
            deadline = time.monotonic() + self.video_timeout_seconds
            while task.state != ProviderTaskState.SUCCEEDED:
                if task.state in (
                    ProviderTaskState.FAILED,
                    ProviderTaskState.CANCELLED,
                ):
                    detail = task.error_message or task.raw_status or task.state.value
                    raise ProviderUnavailable(
                        f"Seedance 镜头 {shot_id} 新版本生成失败：{detail}"
                    )
                if time.monotonic() >= deadline:
                    raise ProviderUnavailable(
                        f"Seedance 镜头 {shot_id} 新版本等待超时；"
                        "任务 ID 已保存，再次操作前可先刷新状态"
                    )
                self._report(
                    progress,
                    message=(
                        f"AI 镜头 {shot_number}/{len(job.visual_requests)} "
                        "的新版本生成中…"
                    ),
                    stage=stage,
                    percent=58,
                    workflow="shot_edit",
                    shot=shot_number,
                    shot_count=len(job.visual_requests),
                    shot_id=shot_id,
                    provider_status=task.raw_status or task.state.value,
                )
                await asyncio.sleep(self.video_poll_interval_seconds)
                task = await self.video_provider.get_status(
                    task.provider_task_id, request.fingerprint()
                )
                task.request_id = shot_id
                self._record_video_task_usage(job, task, version.task)
                version = self._remember_visual_version(
                    job, request, task, None, actor
                )
                job = await self._save_job(job, actor)

            asset = version.asset
            if not asset:
                local_path = workspace / "video" / f"{version.version_id}.mp4"
                await self.video_provider.download(task.provider_task_id, local_path)
                prepared_path, actual_duration = (
                    await self.media_packager.prepare_video_for_timeline(
                        local_path,
                        workspace / "video" / f"{version.version_id}-timeline.mp4",
                        minimum_duration_seconds=self._shot_chapter_duration_seconds(
                            job,
                            shot_id,
                            request.duration_seconds,
                        ),
                    )
                )
                asset = await self.storage.put_file(
                    object_key=(
                        f"qijia-video/{job.id}/video/{shot_id}/"
                        f"{version.version_id}.mp4"
                    ),
                    path=prepared_path,
                    asset_id=f"visual_{version.version_id}",
                    media_type="video/mp4",
                    duration_seconds=round(actual_duration, 3),
                )
                self._remember_visual_version(job, request, task, asset, actor)
                job = await self._save_job(job, actor)
            return await self._render_shot_selection(
                job,
                actor,
                request=request,
                task=task,
                asset=asset,
                version_id=version.version_id,
                workspace=workspace,
                progress=progress,
            )
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            if latest.state == JobState.PRODUCING and latest.review_bundle_hash:
                latest.state = JobState.FINAL_REVIEW_REQUIRED
            latest.error = (
                f"镜头 {shot_number} 的新版本未替换成片，原成片仍然保留：{exc}"
            )[:2000]
            await self._save_job(latest, actor)
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def select_shot_version(
        self,
        job_id: str,
        shot_id: str,
        version_id: str,
        expected_selected_fingerprint: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        resuming = job.state == JobState.PRODUCING and bool(job.review_bundle_hash)
        if job.state != JobState.FINAL_REVIEW_REQUIRED and not resuming:
            raise InvalidTransition("只有待确认成片可以切换镜头版本")
        current = next(
            (item for item in job.visual_requests if item.request_id == shot_id),
            None,
        )
        if not current or current.fingerprint() != expected_selected_fingerprint:
            raise RevisionConflict("该镜头已切换版本，请刷新后重试")
        version = next(
            (
                item
                for item in job.visual_versions
                if item.shot_id == shot_id and item.version_id == version_id
            ),
            None,
        )
        if (
            not version
            or not version.asset
            or version.task.state != ProviderTaskState.SUCCEEDED
        ):
            raise InvalidTransition("该镜头版本尚不可用于成片")
        if version.request.fingerprint() == current.fingerprint():
            raise InvalidTransition("该镜头版本已经用于当前成片")

        workspace = Path(tempfile.mkdtemp(prefix=f"{job.id}-select-", dir=self.work_root))
        try:
            if not resuming:
                self._remember_current_visuals(job, actor)
                job.state = JobState.PRODUCING
                job.error = ""
                job = await self._save_job(job, actor)
            self._report(
                progress,
                message=f"正在把镜头版本 v{version.version} 应用到成片…",
                stage="remotion",
                percent=74,
                workflow="shot_edit",
                shot_id=shot_id,
            )
            return await self._render_shot_selection(
                job,
                actor,
                request=version.request,
                task=version.task,
                asset=version.asset,
                version_id=version.version_id,
                workspace=workspace,
                progress=progress,
            )
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            if latest.state == JobState.PRODUCING and latest.review_bundle_hash:
                latest.state = JobState.FINAL_REVIEW_REQUIRED
            latest.error = (
                f"镜头版本未切换，原成片仍然保留：{exc}"
            )[:2000]
            await self._save_job(latest, actor)
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def _write_support_files(
        self, job: VideoJob, workspace: Path
    ) -> list[tuple[str, Path, str]]:
        if (
            not job.script
            or not job.narration_manifest
            or not job.render_manifest
            or not job.quality_report
        ):
            return []
        support = workspace / "support"
        support.mkdir(parents=True, exist_ok=True)
        script_path = support / "script.json"
        script_path.write_text(
            json.dumps(job.script.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        render_path = support / "render_manifest.json"
        render_path.write_text(
            json.dumps(job.render_manifest.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        quality_path = support / "quality_report.json"
        quality_path.write_text(
            json.dumps(job.quality_report.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        caption_path = support / "caption.md"
        caption_text = job.script.caption.strip()
        hashtags = " ".join(
            item if item.startswith("#") else f"#{item}"
            for item in job.script.hashtags
        )
        caption_path.write_text(
            "\n\n".join(item for item in (caption_text, hashtags) if item),
            encoding="utf-8",
        )
        sources_path = support / "sources.md"
        card = self._source_card_for_job(job)
        sources_path.write_text(
            "\n".join(
                f"- {item.title} — {item.author or '作者未填'}；{item.locator or item.url}"
                for item in card.sources
            ),
            encoding="utf-8",
        )
        srt_path = support / "subtitles.srt"
        srt_lines: list[str] = []

        def srt_time(seconds: float) -> str:
            millis = max(0, int(round(seconds * 1000)))
            hours, millis = divmod(millis, 3600000)
            minutes, millis = divmod(millis, 60000)
            secs, millis = divmod(millis, 1000)
            return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

        for index, cue in enumerate(job.render_manifest.subtitle_cues, 1):
            start = cue.start_frame / job.render_manifest.fps
            end = (cue.start_frame + cue.duration_in_frames) / job.render_manifest.fps
            srt_lines.extend([
                str(index), f"{srt_time(start)} --> {srt_time(end)}", cue.text, ""
            ])
        srt_path.write_text("\n".join(srt_lines), encoding="utf-8")
        intake_name = "input_snapshot.json" if job.input_snapshot else "source_card.json"
        intake_path = support / intake_name
        intake_payload = (
            job.input_snapshot.model_dump(mode="json")
            if job.input_snapshot
            else card.model_dump(mode="json")
        )
        intake_path.write_text(
            json.dumps(intake_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        narration_path = support / "narration_manifest.json"
        narration_path.write_text(
            json.dumps(
                job.narration_manifest.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        video_requests_path = support / "visual_requests.json"
        video_requests_path.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in job.visual_requests],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        video_tasks_path = support / "video_tasks.json"
        video_tasks_path.write_text(
            json.dumps(
                [item.model_dump(mode="json") for item in job.video_tasks],
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        storyboard_path = support / "storyboard_plan.json"
        storyboard_path.write_text(
            json.dumps(
                job.storyboard_plan.model_dump(mode="json")
                if job.storyboard_plan
                else None,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        first_frames_path = support / "first_frame_manifest.json"
        first_frames_path.write_text(
            json.dumps({
                "candidates": [
                    item.model_dump(mode="json", exclude={"source_url"})
                    for item in job.first_frame_candidates
                ],
                "selections": [
                    item.model_dump(mode="json") for item in job.frame_selections
                ],
                "selection_warning": job.frame_selection_warning,
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        skill_path = support / "skill_snapshot.json"
        skill_path.write_text(
            json.dumps(
                job.skill_snapshot.model_dump(mode="json")
                if job.skill_snapshot
                else None,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        pipeline_path = support / 'pipeline_snapshot.json'
        pipeline_path.write_text(
            json.dumps({
                'pipeline_version': job.pipeline_version.value,
                'content_policy': (
                    job.skill_snapshot.model_dump(mode='json')
                    if job.skill_snapshot else None
                ),
                'script_skill': (
                    job.script_skill_snapshot.model_dump(mode='json')
                    if job.script_skill_snapshot else None
                ),
                'prompt_adapter': (
                    job.prompt_adapter_snapshot.model_dump(mode='json')
                    if job.prompt_adapter_snapshot else None
                ),
                'director_skill': (
                    job.director_skill_snapshot.model_dump(mode='json')
                    if job.director_skill_snapshot else None
                ),
                'provider_adapter': (
                    job.provider_adapter_snapshot.model_dump(mode='json')
                    if job.provider_adapter_snapshot else None
                ),
                **({
                    'editorial_plan': (
                        job.editorial_plan.model_dump(mode='json')
                        if job.editorial_plan else None
                    ),
                } if job.pipeline_version in {
                    PipelineVersion.LEGACY,
                    PipelineVersion.SINGLE_OWNER,
                } else {}),
                'director_treatment': (
                    job.director_treatment.model_dump(mode='json')
                    if job.director_treatment else None
                ),
                'visual_bible': (
                    job.visual_bible.model_dump(mode='json')
                    if job.visual_bible else None
                ),
                'asset_bible': (
                    job.asset_bible.model_dump(mode='json')
                    if job.asset_bible else None
                ),
                'style_development': {
                    'selected_style_frame_id': job.selected_style_frame_id,
                    'candidates': [
                        item.model_dump(mode='json', exclude={'source_url'})
                        for item in job.style_frame_candidates
                    ],
                },
            }, ensure_ascii=False, indent=2),
            encoding='utf-8',
        )
        return [
            ("script.json", script_path, "application/json"),
            ("render_manifest.json", render_path, "application/json"),
            ("quality_report.json", quality_path, "application/json"),
            ("caption.md", caption_path, "text/markdown"),
            ("sources.md", sources_path, "text/markdown"),
            ("subtitles.srt", srt_path, "application/x-subrip"),
            (intake_name, intake_path, "application/json"),
            ("narration_manifest.json", narration_path, "application/json"),
            ("visual_requests.json", video_requests_path, "application/json"),
            ("video_tasks.json", video_tasks_path, "application/json"),
            ("storyboard_plan.json", storyboard_path, "application/json"),
            ("first_frame_manifest.json", first_frames_path, "application/json"),
            ("skill_snapshot.json", skill_path, "application/json"),
            ('pipeline_snapshot.json', pipeline_path, 'application/json'),
        ]

    @staticmethod
    def _review_hash(artifacts: list[Artifact]) -> str:
        by_name = {item.name: item.asset.sha256 for item in artifacts}
        missing = [name for name in REVIEW_BUNDLE_NAMES if name not in by_name]
        if missing:
            raise QualityGateFailed("成片确认包缺少产物：" + "、".join(missing))
        return content_hash({name: by_name[name] for name in REVIEW_BUNDLE_NAMES})

    async def _write_package_files(
        self, job: VideoJob, workspace: Path
    ) -> list[tuple[str, Path, str]]:
        package_dir = workspace / "package"
        package_dir.mkdir(parents=True, exist_ok=True)
        draft = next(item for item in job.artifacts if item.name == "draft.mp4")
        renderer_metadata = getattr(self.renderer, "metadata", lambda: {
            "name": self.renderer.name,
        })()
        generation_tasks = self._all_video_tasks(job)
        provenance = {
            "schema_version": "1.0",
            "module": "qijia_video",
            "module_version": MODULE_VERSION,
            "job_id": job.id,
            'pipeline_version': job.pipeline_version.value,
            "source_card_id": job.source_card_id,
            "source_card_revision": job.source_card_revision,
            "content_skill": (
                job.skill_snapshot.model_dump(mode="json")
                if job.skill_snapshot
                else None
            ),
            'script_skill': (
                job.script_skill_snapshot.model_dump(mode='json')
                if job.script_skill_snapshot else None
            ),
            'director_skill': (
                job.director_skill_snapshot.model_dump(mode='json')
                if job.director_skill_snapshot else None
            ),
            'provider_adapter': (
                job.provider_adapter_snapshot.model_dump(mode='json')
                if job.provider_adapter_snapshot else None
            ),
            "generation_settings_hash": (
                content_hash(job.generation_settings)
                if job.generation_settings
                else ""
            ),
            "script_provider": self.script_provider.name,
            "storyboard_provider": self.storyboard_provider.name,
            "tts_provider": self.tts_provider.name,
            "image_provider": self.image_provider.name,
            "first_frame_strategy": "one_generated_frame_per_shot",
            "style_frame_generation_count": len(job.style_frame_candidates),
            "selected_style_frame_id": job.selected_style_frame_id,
            "video_provider": self.video_provider.name,
            "storyboard_input_hash": (
                job.storyboard_plan.input_hash if job.storyboard_plan else ""
            ),
            "first_frame_generation_count": len(job.first_frame_candidates),
            "first_frame_usage_total_tokens": sum(
                item.usage_total_tokens for item in job.first_frame_candidates
            ),
            "first_frame_selections": [
                {
                    "shot_id": item.shot_id,
                    "candidate_id": item.selected_candidate_id,
                }
                for item in (job.storyboard_plan.shots if job.storyboard_plan else [])
            ],
            "video_request_fingerprints": [
                item.fingerprint() for item in job.visual_requests
            ],
            "video_provider_task_ids": [
                item.provider_task_id for item in job.video_tasks
            ],
            "seedance_usage_total_tokens": sum(
                item.usage_total_tokens for item in generation_tasks
            ),
            "seedance_generation_attempts": [
                {
                    "request_id": item.request_id,
                    "provider_task_id": item.provider_task_id,
                    "state": item.state.value,
                    "usage_total_tokens": item.usage_total_tokens,
                    "selected": any(
                        current.provider == item.provider
                        and current.provider_task_id == item.provider_task_id
                        for current in job.video_tasks
                    ),
                }
                for item in generation_tasks
            ],
            "renderer": renderer_metadata,
            "media_packager": self.media_packager.name,
            "quality_checker": self.quality_checker.name,
            "storage": self.storage.name,
            "script_hash": job.script_hash,
            "render_manifest_hash": content_hash(job.render_manifest),
            "quality_report_hash": content_hash(job.quality_report),
            "review_bundle_hash": job.review_bundle_hash,
            "approved_draft_sha256": draft.asset.sha256,
            "approvals": [item.model_dump(mode="json") for item in job.approvals],
            "generated_at": timestamp(),
        }
        provenance_path = package_dir / "provenance.json"
        provenance_path.write_text(
            json.dumps(provenance, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return [("provenance.json", provenance_path, "application/json")]

    async def _write_artifact_manifest(
        self, job: VideoJob, workspace: Path
    ) -> tuple[str, Path, str]:
        package_dir = workspace / "package"
        package_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": "1.0",
            "job_id": job.id,
            "review_bundle_hash": job.review_bundle_hash,
            "manual_approval": job.approval("final").model_dump(mode="json")
            if job.approval("final")
            else None,
            "artifacts": [
                item.model_dump(mode="json")
                for item in sorted(job.artifacts, key=lambda value: value.name)
                if item.name != "artifact_manifest.json"
            ],
            "generated_at": timestamp(),
        }
        path = package_dir / "artifact_manifest.json"
        path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return "artifact_manifest.json", path, "application/json"

    async def produce(
        self,
        job_id: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        if (
            job.state == JobState.MEDIA_REVIEW_REQUIRED
            and job.pre_generation_media_mode
            == PreGenerationMediaMode.REVIEW_BEFORE_GENERATION
        ):
            return job
        if job.state not in (
            JobState.SCRIPT_APPROVED,
            JobState.PRODUCING,
            JobState.QUALITY_CHECKING,
            JobState.FAILED,
        ):
            raise InvalidTransition(f"当前状态不能生产成片：{job.state.value}")
        if not job.approval("script") or not job.script:
            raise QualityGateFailed("脚本尚未人工确认")
        if job.state == JobState.FAILED and job.failed_stage not in ("production", "quality"):
            raise InvalidTransition("当前失败状态不能从生产阶段重试")
        job.state = JobState.PRODUCING
        job.failed_stage = ""
        job.error = ""
        job = await self._save_job(job, actor)
        workspace = Path(tempfile.mkdtemp(prefix=f"{job.id}-", dir=self.work_root))
        try:
            card = self._source_card_for_job(job)
            self._validate_script(job.script, card)
            settings = self._generation_settings(job)
            self._report(
                progress,
                message="正在生成旁白…",
                stage="tts",
                percent=34,
            )
            audio_assets = (
                self._narration_assets(job)
                if job.narration_manifest
                and job.narration_manifest.provider == self.tts_provider.name
                else []
            )
            if not audio_assets:
                rebind_reused_director_plan = bool(
                    job.storyboard_plan
                    and job.visual_bible
                    and job.visual_requests
                    and job.video_tasks
                    and not job.narration_manifest
                )
                previous_visual_assets = [
                    item
                    for item in (
                        job.render_manifest.assets if job.render_manifest else []
                    )
                    if item.media_type.startswith("video/")
                ]
                async def persist_tts_usage(usage: ProviderUsageRecord) -> None:
                    nonlocal job
                    job = await self._persist_usage_record(job, usage, actor)

                synthesize_with_usage = getattr(
                    self.tts_provider, "synthesize_with_usage", None
                )
                if callable(synthesize_with_usage):
                    narration, generated_files = await synthesize_with_usage(
                        job.script,
                        workspace,
                        voice_id=settings.tts_voice_id,
                        speed_ratio=settings.tts_speed_ratio,
                        on_usage=persist_tts_usage,
                    )
                else:
                    narration, generated_files = (
                        await self.tts_provider.synthesize(
                            job.script,
                            workspace,
                            voice_id=settings.tts_voice_id,
                            speed_ratio=settings.tts_speed_ratio,
                        )
                    )
                audio_assets = []
                for generated in generated_files:
                    key = f"qijia-video/{job.id}/audio/{generated.path.name}"
                    audio_assets.append(await self.storage.put_file(
                        object_key=key,
                        path=generated.path,
                        asset_id=generated.asset_id,
                        media_type=generated.media_type,
                        duration_seconds=generated.duration_seconds,
                    ))
                job.narration_manifest = narration
                if rebind_reused_director_plan:
                    rebound_hash = self._storyboard_input_hash(job)
                    job.storyboard_plan.input_hash = rebound_hash
                    if job.director_treatment:
                        job.director_treatment.input_hash = rebound_hash
                    if job.asset_bible:
                        job.asset_bible.input_hash = rebound_hash
                    job.visual_bible.input_hash = rebound_hash
                job.render_manifest = self._build_render_manifest(
                    job, audio_assets, previous_visual_assets
                )
                job.review_bundle_hash = ""
                job = await self._save_job(job, actor)
            narration_duration = job.narration_manifest.total_duration_seconds
            if not (
                MIN_VIDEO_DURATION_SECONDS
                <= narration_duration
                <= MAX_VIDEO_DURATION_SECONDS
            ):
                raise QualityGateFailed(
                    "旁白实际时长超出 45-75 秒范围："
                    f"narration_duration_range={narration_duration:.3f}。"
                    "已在生成 AI 画面前停止，请返回修改脚本"
                )
            self._report(
                progress,
                message="旁白已就绪，准备生成分镜和首帧…",
                stage="tts",
                percent=42,
            )

            # Jobs that already froze paid video requests before storyboard v1
            # must resume those exact requests. Generating new first frames here
            # would add cost without changing the legacy Seedance tasks.
            legacy_visual_workflow = bool(
                job.visual_requests and not job.storyboard_plan
            )
            if legacy_visual_workflow:
                self._report(
                    progress,
                    message="正在恢复升级前已冻结的 AI 镜头任务，不新增首帧费用…",
                    stage="seedance_shot_1",
                    percent=54,
                )
                job = await self._ensure_video_tasks(job, actor, progress)
            else:
                job = await self._ensure_storyboard_plan(job, actor, progress)
                if (
                    job.pipeline_version == PipelineVersion.QUALITY_FIRST
                    and job.pre_generation_media_mode
                    == PreGenerationMediaMode.REVIEW_BEFORE_GENERATION
                ):
                    job = await self._ensure_style_frames(
                        job,
                        actor,
                        workspace,
                        progress,
                    )
                if (
                    job.pre_generation_media_mode
                    == PreGenerationMediaMode.REVIEW_BEFORE_GENERATION
                ):
                    job.state = JobState.MEDIA_REVIEW_REQUIRED
                    job.error = ""
                    job = await self._save_job(job, actor)
                    self._report(
                        progress,
                        message=(
                            (
                                "视觉方案、三张样片和文字分镜已就绪，"
                                "请确认视觉方向并安排自有素材；"
                            )
                            if job.pipeline_version
                            == PipelineVersion.QUALITY_FIRST
                            else "旁白和文字分镜已就绪，请安排自有素材；"
                        )
                        + (
                            "确认前不会生成 AI 图片或视频"
                        ),
                        stage="confirm_media",
                        percent=46,
                    )
                    return job
                video_shot_ids = {
                    shot.shot_id
                    for shot in job.storyboard_plan.shots
                    if shot.visual_type == "video"
                    and not shot.selected_media_id
                }
                image_shot_ids = {
                    shot.shot_id
                    for shot in job.storyboard_plan.shots
                    if shot.visual_type == "image"
                    and not shot.selected_media_id
                }
                job = await self._ensure_first_frames(
                    job,
                    actor,
                    workspace,
                    progress,
                    target_shot_ids=video_shot_ids,
                    progress_stage="first_frames",
                    progress_start=46,
                    progress_end=54,
                    progress_label="视频镜头",
                )
                job = await self._ensure_video_task_submissions(
                    job, actor, progress
                )
                job = await self._ensure_first_frames(
                    job,
                    actor,
                    workspace,
                    progress,
                    target_shot_ids=image_shot_ids,
                    progress_stage="seedance_parallel",
                    progress_start=58,
                    progress_end=65,
                    progress_label="动态图片",
                )
                job = await self._wait_for_video_tasks(job, actor, progress)
            self._report(
                progress,
                message=(
                    f"{len(job.visual_requests)} 段 Seedance 视频已就绪，正在整理素材…"
                ),
                stage="visual_assets",
                percent=74,
            )
            job, visual_assets = await self._ensure_visual_assets(
                job, actor, workspace, audio_assets
            )
            job.render_manifest = self._build_render_manifest(
                job, audio_assets, visual_assets
            )
            job.review_bundle_hash = ""
            job = await self._save_job(job, actor)
            self._report(
                progress,
                message="正在用 Remotion 合成画面、旁白与字幕…",
                stage="remotion_render",
                percent=78,
            )
            raw_draft_path = await self.renderer.render(
                job.render_manifest, self.storage, workspace
            )
            self._report(
                progress,
                message="合成完成，正在整理 MP4 容器…",
                stage="remotion_normalize",
                percent=83,
            )
            draft_path = await self.media_packager.normalize(
                raw_draft_path, workspace / "draft.normalized.mp4"
            )
            job.state = JobState.QUALITY_CHECKING
            job = await self._save_job(job, actor)
            self._report(
                progress,
                message="正在检查时长、画幅、音轨与文件完整性…",
                stage="quality",
                percent=86,
            )
            report = await self.quality_checker.inspect(draft_path, job.render_manifest)
            job.quality_report = report
            job = await self._save_job(job, actor)
            if report.automatic_status != "review_ready":
                failed_checks = [
                    f"{item.get('id') or 'unknown'}={item.get('detail', '')}"
                    for item in report.checks
                    if not bool(item.get("passed"))
                ]
                raise QualityGateFailed(
                    "成片自动质检未通过：" + "、".join(failed_checks)
                )
            self._report(
                progress,
                message="自动质检通过，正在生成封面和确认材料…",
                stage="artifact_upload",
                percent=88,
            )
            draft_asset = await self.storage.put_file(
                object_key=f"qijia-video/{job.id}/renders/draft.mp4",
                path=draft_path,
                asset_id="draft_video",
                media_type="video/mp4",
                duration_seconds=job.narration_manifest.total_duration_seconds,
            )
            job.artifacts = [Artifact(name="draft.mp4", asset=draft_asset)]
            cover_path = await self.renderer.render_cover(
                job.render_manifest, self.storage, workspace
            )
            cover_asset = await self.storage.put_file(
                object_key=f"qijia-video/{job.id}/renders/cover.jpg",
                path=cover_path,
                asset_id="cover",
                media_type="image/jpeg",
            )
            job.artifacts.append(Artifact(name="cover.jpg", asset=cover_asset))
            for name, path, media_type in await self._write_support_files(job, workspace):
                asset = await self.storage.put_file(
                    object_key=f"qijia-video/{job.id}/renders/{name}",
                    path=path,
                    asset_id=name.replace(".", "_"),
                    media_type=media_type,
                )
                job.artifacts.append(Artifact(name=name, asset=asset))
            job.review_bundle_hash = self._review_hash(job.artifacts)
            job.state = JobState.FINAL_REVIEW_REQUIRED
            job = await self._save_job(job, actor)
            self._report(
                progress,
                message="成片已就绪，等待你确认…",
                stage="confirm_final",
                percent=90,
            )
            return job
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            failed_from = latest.state
            latest.state = JobState.FAILED
            latest.failed_stage = (
                "quality" if failed_from == JobState.QUALITY_CHECKING else "production"
            )
            latest.error = str(exc)
            await self._save_job(latest, actor)
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def approve_final(
        self,
        job_id: str,
        expected_revision: int,
        review_bundle_hash: str,
        actor: Actor,
        *,
        package_immediately: bool = True,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.FINAL_REVIEW_REQUIRED:
            raise InvalidTransition("当前没有可确认的成片")
        if job.pending_shot_media_edits:
            raise InvalidTransition("请先应用或撤销待处理的镜头素材修改")
        draft = next((item for item in job.artifacts if item.name == "draft.mp4"), None)
        actual_bundle_hash = self._review_hash(job.artifacts)
        if (
            not draft
            or review_bundle_hash != actual_bundle_hash
            or review_bundle_hash != job.review_bundle_hash
        ):
            raise RevisionConflict("成片确认包已经变化，请重新预览后确认")
        job.approvals = [item for item in job.approvals if item.kind != "final"]
        job.approvals.append(ApprovalRecord(
            kind="final",
            actor=actor.username,
            artifact_hash=review_bundle_hash,
            approved_at=timestamp(),
            warnings=list((job.quality_report or QualityReport(
                automatic_status="manual_review_required",
                generated_at=timestamp(),
            )).warnings),
        ))
        job.state = JobState.FINAL_APPROVED
        job.failed_stage = ""
        job.error = ""
        job = await self._save_job(job, actor)
        if package_immediately:
            return await self.package(job.id, actor)
        return job

    async def package(
        self,
        job_id: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        if job.state == JobState.PACKAGED:
            self._report(
                progress,
                message="发布包已完成",
                stage="package",
                percent=100,
            )
            return job
        if job.state not in (JobState.FINAL_APPROVED, JobState.FAILED):
            raise InvalidTransition(f"当前状态不能生成发布包：{job.state.value}")
        if job.state == JobState.FAILED and job.failed_stage != "package":
            raise InvalidTransition("当前失败状态不能从发布包阶段重试")
        try:
            approval = job.approval("final")
            draft = next(
                (item for item in job.artifacts if item.name == "draft.mp4"), None
            )
            actual_bundle_hash = self._review_hash(job.artifacts)
            if (
                not approval
                or not draft
                or approval.artifact_hash != job.review_bundle_hash
                or actual_bundle_hash != job.review_bundle_hash
            ):
                raise RevisionConflict("已确认的成片包发生变化，请重新检查")
        except Exception as exc:
            job.state = JobState.FAILED
            job.failed_stage = "package"
            job.error = str(exc)
            await self._save_job(job, actor)
            raise
        if job.state == JobState.FAILED:
            job.state = JobState.FINAL_APPROVED
            job.failed_stage = ""
            job.error = ""
            job = await self._save_job(job, actor)

        self._report(
            progress,
            message="正在把已确认成片整理为发布版本…",
            stage="package",
            percent=94,
        )
        workspace: Path | None = None
        try:
            workspace = Path(
                tempfile.mkdtemp(prefix=f"{job.id}-final-", dir=self.work_root)
            )
            job.artifacts = [
                item
                for item in job.artifacts
                if item.name not in (
                    "final.mp4", "provenance.json", "artifact_manifest.json"
                )
            ]
            # 人工确认的 draft 就是最终成片。发布名直接引用同一不可变资产，
            # 避免从 TOS 下载后再上传一次完全相同的字节。
            job.artifacts.append(Artifact(name="final.mp4", asset=draft.asset))
            self._report(
                progress,
                message="最终视频已确认，正在写入来源与审批记录…",
                stage="package",
                percent=97,
            )
            for name, path, media_type in await self._write_package_files(job, workspace):
                asset = await self.storage.put_file(
                    object_key=f"qijia-video/{job.id}/final/{name}",
                    path=path,
                    asset_id=name.replace(".", "_"),
                    media_type=media_type,
                )
                job.artifacts.append(Artifact(name=name, asset=asset))
            name, path, media_type = await self._write_artifact_manifest(job, workspace)
            artifact_manifest_asset = await self.storage.put_file(
                object_key=f"qijia-video/{job.id}/final/{name}",
                path=path,
                asset_id="artifact_manifest_json",
                media_type=media_type,
            )
            job.artifacts.append(Artifact(name=name, asset=artifact_manifest_asset))
            present = {item.name for item in job.artifacts}
            missing = sorted(REQUIRED_PACKAGE_NAMES - present)
            if missing:
                raise QualityGateFailed("发布包不完整：" + "、".join(missing))
            job.state = JobState.PACKAGED
            job = await self._save_job(job, actor)
            self._report(
                progress,
                message="发布包已完成，可以下载",
                stage="package",
                percent=100,
            )
            return job
        except Exception as exc:
            latest = await self.get_job(job_id, actor)
            if latest.state != JobState.PACKAGED:
                latest.state = JobState.FAILED
                latest.failed_stage = "package"
                latest.error = str(exc)
                await self._save_job(latest, actor)
            raise
        finally:
            if workspace:
                await asyncio.to_thread(shutil.rmtree, workspace, True)

    async def build_release_archive(
        self,
        job_id: str,
        actor: Actor,
        destination: Path,
        *,
        shared_read: bool = False,
    ) -> Path:
        """Build the human-facing release ZIP from a completed package.

        The archive is generated on demand so jobs packaged before this feature
        was added receive the same download experience without a migration.
        """
        job = await (
            self.view_job(job_id, actor)
            if shared_read
            else self.get_job(job_id, actor)
        )
        if job.state != JobState.PACKAGED:
            raise InvalidTransition("发布包尚未完成")
        artifacts = {item.name: item for item in job.artifacts}
        missing = sorted(REQUIRED_PACKAGE_NAMES - artifacts.keys())
        if missing:
            raise QualityGateFailed("发布包不完整：" + "、".join(missing))

        staging = destination.parent / "release-files"
        staging.mkdir(parents=True, exist_ok=True)
        names = sorted(REQUIRED_PACKAGE_NAMES)
        for name in names:
            await self.storage.materialize(artifacts[name].asset, staging / name)

        destination.parent.mkdir(parents=True, exist_ok=True)

        def write_archive() -> None:
            with zipfile.ZipFile(
                destination,
                mode="w",
                compression=zipfile.ZIP_STORED,
                allowZip64=True,
            ) as archive:
                for name in names:
                    archive.write(staging / name, arcname=name)

        await asyncio.to_thread(write_archive)
        if not destination.is_file() or destination.stat().st_size == 0:
            raise QualityGateFailed("完整发布包 ZIP 生成失败")
        return destination
