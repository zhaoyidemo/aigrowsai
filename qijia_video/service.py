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
    DouyinPerformance,
    DouyinPlaybackSnapshot,
    FirstFrameCandidate,
    GenerationSettings,
    JobState,
    InterpretationBoundary,
    NewsResearchBrief,
    PersonResearchBrief,
    ProviderTask,
    ProviderTaskState,
    ProviderUsageRecord,
    QualityReport,
    RenderManifest,
    SEEDANCE_EFFICIENT_MODEL,
    SEEDANCE_FLAGSHIP_MODEL,
    SEEDANCE_RETIRED_MODEL,
    ScriptDraft,
    ScriptBeat,
    ScriptReview,
    SourceCard,
    SourceCardInput,
    SourceEntry,
    SourceCardStatus,
    SkillResearchMode,
    StoryboardPlan,
    StoryboardShot,
    ScreenTextCue,
    SubtitleCue,
    VideoJob,
    VisualGenerationRequest,
    VisualShotVersion,
    VisualBlock,
    VerifiedFact,
    content_hash,
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
from qijia_video.skill_registry import (
    ContentSkillRegistry,
    SkillRegistryError,
    default_skill_registry,
)
from qijia_video.tts_options import TTS_SCRIPT_CHARACTER_TARGETS


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
        self, candidate: FirstFrameCandidate
    ) -> FirstFrameCandidate:
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
                SEEDANCE_RETIRED_MODEL: "Seedance 1.5 Pro",
                SEEDANCE_FLAGSHIP_MODEL: "Seedance 2.0",
            }.get(task.model_id, "Seedance")
            billing_mode = (
                "无声视频"
                if task.model_id in {
                    SEEDANCE_EFFICIENT_MODEL,
                    SEEDANCE_RETIRED_MODEL,
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
        if len(script.beats) < LEGACY_STORYBOARD_SHOT_COUNT:
            raise QualityGateFailed(
                "脚本生成结果少于五个自然叙事段，本次结果未进入人工审核"
            )
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

        legacy_boundary_text = (
            "只围绕用户输入的观点展开，不补造人物经历、逐字引语、"
            "研究数据或来源出处。"
        )
        boundary_text = (
            "只围绕用户输入的观点和自动研究中有来源支持的事实展开；不得补造人物经历、"
            "逐字引语、研究数据或来源出处，也不得把用户观点、研究摘要或编辑角度写成人物"
            "原话。资料存在冲突或不确定时，必须保留限定语。"
        )
        for index, boundary in enumerate(enriched.interpretation_boundary):
            if boundary.text == legacy_boundary_text:
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
    def _script_prompt_for_settings(
        settings: GenerationSettings,
        research_brief: PersonResearchBrief | NewsResearchBrief | None = None,
    ) -> str:
        minimum, maximum = TTS_SCRIPT_CHARACTER_TARGETS[
            settings.tts_speed_ratio
        ]
        prompt = (
            settings.script_prompt.rstrip()
            + "\n\n【本任务配音节奏】"
            + f"已选 Seed-TTS 2.0 语速 {settings.tts_speed_ratio:.1f}x。"
            + f"所有 narration 的纯旁白合计建议 {minimum}-{maximum} 个汉字，"
            + "以此范围覆盖上文任何不同的字数建议；"
            + "目标仍为 45-75 秒，表达完整和自然优先。"
        )
        if not research_brief:
            return prompt
        editorial_context = {
            "summary": research_brief.summary,
            "core_tension": research_brief.core_tension,
            "audience_relevance": research_brief.audience_relevance,
            "content_angles": research_brief.content_angles,
            "interaction_opportunity": research_brief.interaction_opportunity,
            "uncertainties": research_brief.uncertainties,
        }
        if isinstance(research_brief, NewsResearchBrief):
            editorial_context.update({
                "topic": research_brief.topic,
                "research_as_of": research_brief.as_of,
                "evidence_time_context": [
                    {
                        "claim": item.claim,
                        "source_title": item.source_title,
                        "source_kind": item.source_kind,
                        "published_at": item.published_at,
                        "event_at": item.event_at,
                    }
                    for item in research_brief.evidence
                ],
            })
        return (
            prompt
            + "\n\n【自动研究简报】\n"
            + json.dumps(editorial_context, ensure_ascii=False)
            + "\n研究证据已经作为 research_fact 加入本次来源卡。"
            + "简报中的内容角度只用于编辑构思，不可冒充证据或逐字引语；"
            + "遇到 uncertainties 必须保留限定语，不要为了钩子抹掉边界。"
        )

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
            if not segment.source_refs:
                raise QualityGateFailed(f"脚本段落 {segment.id} 缺少来源引用")
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
            job.storyboard_plan.input_hash = self._storyboard_input_hash(job)
        else:
            job.storyboard_plan = None
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
        card = await self.get_source_card(source_card_id, actor)
        if card.status != SourceCardStatus.VERIFIED:
            raise QualityGateFailed("来源卡必须先核验")
        try:
            frozen_settings, skill_snapshot = self.skill_registry.freeze(
                card,
                generation_settings or GenerationSettings(),
            )
        except SkillRegistryError as exc:
            raise QualityGateFailed(str(exc)) from exc
        now = timestamp()
        draft = VideoJob(
            id="pending",
            state=JobState.CARD_VERIFIED,
            source_card_id=card.id,
            source_card_revision=card.revision,
            source_card_snapshot=card.model_dump(mode="json"),
            skill_snapshot=skill_snapshot,
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

    def content_skills(self) -> list[dict]:
        return self.skill_registry.public_catalog()

    async def list_jobs(self, actor: Actor, *, limit: int = 100) -> list[VideoJob]:
        return [
            VideoJob.model_validate(item)
            for item in await self.repository.list_visible(
                "job", actor, limit=limit
            )
        ]

    async def get_job(self, job_id: str, actor: Actor) -> VideoJob:
        return VideoJob.model_validate(
            await self.repository.get("job", job_id, actor)
        )

    async def view_job(self, job_id: str, actor: Actor) -> VideoJob:
        return VideoJob.model_validate(
            await self.repository.get_visible("job", job_id, actor)
        )

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
            card = SourceCard.model_validate(job.source_card_snapshot)
            settings = self._generation_settings(job)

            async def persist_script_usage(usage: ProviderUsageRecord) -> None:
                nonlocal job
                job = await self._persist_usage_record(job, usage, actor)

            self._report(
                progress,
                message="正在读取内容输入与来源边界…",
                stage="material_confirmed",
                percent=4,
            )
            if job.skill_snapshot:
                research_mode = job.skill_snapshot.research_mode
            elif card.content_format == ContentFormat.PERSON_IDEA:
                research_mode = SkillResearchMode.PERSON_VIEWPOINT_OPTIONAL
            else:
                research_mode = SkillResearchMode.NONE
            research_required = (
                research_mode == SkillResearchMode.RECENT_NEWS_REQUIRED
            )
            research_operation = {
                SkillResearchMode.PERSON_VIEWPOINT_OPTIONAL: "person_research",
                SkillResearchMode.RECENT_NEWS_REQUIRED: "recent_news_research",
            }.get(research_mode, "")
            research_usage_exists = any(
                item.operation == research_operation
                for item in job.usage_records
            ) if research_operation else False
            skill_research = getattr(
                self.script_provider, "research_for_skill", None
            )
            specific_research = getattr(
                self.script_provider,
                (
                    "research_recent_news"
                    if research_required
                    else "research_person_viewpoint"
                ),
                None,
            )
            if research_mode != SkillResearchMode.NONE and job.research_brief:
                card = self._card_with_person_research(
                    card, job.research_brief
                )
            elif research_mode != SkillResearchMode.NONE and research_usage_exists:
                if not job.research_warning:
                    job.research_warning = (
                        "上次最新新闻研究调用未形成完整简报；为避免重复计费，"
                        "本次不会自动重新提交，且禁止降级生成脚本。"
                        if research_required
                        else (
                            "上次自动研究调用未形成完整简报；为避免重复计费，"
                            "本次直接使用原始人物观点继续生成。"
                        )
                    )
                    job = await self._save_job(job, actor)
                if research_required:
                    raise QualityGateFailed(job.research_warning)
            elif research_mode != SkillResearchMode.NONE and job.research_warning:
                if research_required:
                    raise QualityGateFailed(job.research_warning)
            elif (
                research_mode != SkillResearchMode.NONE
                and (callable(skill_research) or callable(specific_research))
            ):
                self._report(
                    progress,
                    message=(
                        "正在检索最新公开动态并交叉核验来源…"
                        if research_required
                        else "正在联网研究人物与主题，整理可追溯简报…"
                    ),
                    stage=research_operation,
                    percent=7,
                )
                try:
                    if callable(skill_research):
                        brief = await skill_research(
                            card,
                            research_mode=research_mode.value,
                            research_prompt=(
                                job.skill_snapshot.research_prompt
                                if job.skill_snapshot
                                else ""
                            ),
                            research_as_of=(
                                job.skill_snapshot.frozen_at
                                if job.skill_snapshot
                                else job.created_at
                            ),
                            on_usage=persist_script_usage,
                        )
                    else:
                        research_kwargs = {
                            "on_usage": persist_script_usage,
                        }
                        if research_required:
                            research_kwargs["as_of"] = (
                                job.skill_snapshot.frozen_at
                                if job.skill_snapshot
                                else job.created_at
                            )
                        brief = await specific_research(card, **research_kwargs)
                    if (
                        research_required
                        and not isinstance(brief, NewsResearchBrief)
                    ):
                        raise ProviderUnavailable(
                            "新闻研究 Provider 返回了错误的简报类型"
                        )
                    if (
                        not research_required
                        and not isinstance(brief, PersonResearchBrief)
                    ):
                        raise ProviderUnavailable(
                            "人物研究 Provider 返回了错误的简报类型"
                        )
                    card = self._card_with_person_research(card, brief)
                    job.research_brief = brief
                    job.research_warning = ""
                    job.source_card_snapshot = card.model_dump(mode="json")
                    job = await self._save_job(job, actor)
                except Exception as research_error:
                    job.research_warning = (
                        (
                            "最新新闻研究未形成两个独立、可追溯的来源，"
                            "禁止降级生成脚本："
                            if research_required
                            else (
                                "自动研究暂未形成可追溯简报，"
                                "已使用原始人物观点继续生成："
                            )
                        )
                        + str(research_error)
                    )[:2000]
                    job = await self._save_job(job, actor)
                    if research_required:
                        raise QualityGateFailed(job.research_warning) from research_error
            elif research_mode != SkillResearchMode.NONE:
                job.research_warning = (
                    "当前脚本 Provider 不支持最新新闻研究，禁止生成脚本。"
                    if research_required
                    else "当前脚本 Provider 不支持人物研究，已使用原始观点继续生成。"
                )
                job = await self._save_job(job, actor)
                if research_required:
                    raise QualityGateFailed(job.research_warning)

            self._report(
                progress,
                message="正在调用脚本模型生成口播脚本…",
                stage="script_generation",
                percent=14,
            )
            script_prompt = self._script_prompt_for_settings(
                settings, job.research_brief
            )

            generate_for_skill = getattr(
                self.script_provider, "generate_for_skill", None
            )
            generate_with_usage = getattr(
                self.script_provider, "generate_with_usage", None
            )
            if callable(generate_for_skill) and job.skill_snapshot:
                script = await generate_for_skill(
                    card,
                    script_prompt,
                    system_prompt=job.skill_snapshot.script_system_prompt,
                    on_usage=persist_script_usage,
                )
            elif callable(generate_with_usage):
                script = await generate_with_usage(
                    card,
                    script_prompt,
                    on_usage=persist_script_usage,
                )
            else:
                script = await self.script_provider.generate(
                    card, script_prompt
                )
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
            self._report(
                progress,
                message="脚本已生成，正在自动核对引用与安全边界…",
                stage="script_generation",
                percent=24,
            )
            review = await self.script_provider.review(card, script)
            if not review.passed or review.blocking_reasons:
                raise QualityGateFailed("自动脚本审核未通过")
            if review.input_hash != content_hash(script):
                raise QualityGateFailed("脚本审核结果没有绑定当前脚本")
            job.script = script
            job.script_hash = content_hash(script)
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
        seedance_prompt: str | None = None,
        tts_voice_id: str | None = None,
        tts_speed_ratio: float | None = None,
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.SCRIPT_REVIEW_REQUIRED:
            raise InvalidTransition("只有待确认脚本可以编辑")
        card = SourceCard.model_validate(job.source_card_snapshot)
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
        seedance_prompt_changed = (
            seedance_prompt is not None
            and seedance_prompt != current_settings.seedance_prompt
        )
        if seedance_prompt is not None:
            current_settings.seedance_prompt = seedance_prompt
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
        settings_changed = (
            seedance_prompt_changed or tts_voice_changed or tts_speed_changed
        )
        if not script_changed and not settings_changed:
            return job
        job.generation_settings = current_settings
        self._apply_reviewed_script(
            job,
            script,
            review,
            allow_visual_reuse=not seedance_prompt_changed,
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
    ) -> VideoJob:
        job = await self.get_job(job_id, actor)
        self._assert_revision(job.revision, expected_revision)
        if job.state != JobState.SCRIPT_REVIEW_REQUIRED or not job.script:
            raise InvalidTransition("当前没有可确认的脚本")
        card = SourceCard.model_validate(job.source_card_snapshot)
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
        job.state = JobState.SCRIPT_APPROVED
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

    @staticmethod
    def _expanded_storyboard_groups(
        beats: list[ScriptBeat],
        weights: list[float],
        shot_count: int,
    ) -> list[list[ScriptBeat]]:
        """Allocate extra image chapters inside longer semantic beats."""

        counts = [1 for _ in beats]
        # Keep the opening hook as one uninterrupted moving chapter so the
        # first five seconds are not cut into a video/image hand-off.
        eligible_indices = list(range(1, len(beats))) or [0]
        for _ in range(shot_count - len(beats)):
            index = max(
                eligible_indices,
                key=lambda item: (
                    max(0.001, float(weights[item])) / counts[item],
                    -item,
                ),
            )
            counts[index] += 1
        return [
            [beat]
            for beat, count in zip(beats, counts)
            for _ in range(count)
        ]

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
        if settings.shot_count >= len(beats):
            weights = (
                [durations[item.id] for item in beats]
                if durations
                else [max(1, len(item.narration)) for item in beats]
            )
            return cls._expanded_storyboard_groups(
                beats, weights, settings.shot_count
            )
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
        source_card = SourceCard.model_validate(job.source_card_snapshot)
        return bool(source_card.reference_assets)

    @classmethod
    def _storyboard_base_style(cls, job: VideoJob) -> str:
        if cls._has_reference_image(job):
            return (
                "本任务提供全局参考图。参考图是画风、色彩、光影、材质和人物视觉特征的"
                "最高优先级。分镜只设计场景、人物动作、空间关系、构图和运镜，不另行规定"
                "艺术媒介、固定配色或与参考图冲突的造型。"
            )
        settings = cls._generation_settings(job)
        return settings.seedance_prompt

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
            payload["visual_types"] = list(
                QijiaVideoService._storyboard_visual_types(job, groups)
            )
        return content_hash(payload)

    @classmethod
    def _storyboard_input_hash(cls, job: VideoJob) -> str:
        return cls._storyboard_input_hash_for_style(
            job, cls._storyboard_base_style(job)
        )

    async def _ensure_storyboard_plan(
        self,
        job: VideoJob,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> VideoJob:
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
            if self._has_reference_image(job):
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

        generate_with_usage = getattr(
            self.storyboard_provider, "generate_with_usage", None
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
        if plan.input_hash != expected_hash:
            raise ProviderUnavailable("分镜 Provider 返回了错误的输入指纹")
        if [item.beat_ids for item in plan.shots] != beat_groups:
            raise ProviderUnavailable("分镜 Provider 返回了错误的段落映射")
        if [item.visual_type for item in plan.shots] != visual_types:
            raise ProviderUnavailable("分镜 Provider 返回了错误的图像/视频分配")
        job.storyboard_plan = plan
        job = await self._save_job(job, actor)
        self._report(
            progress,
            message=(
                f"{len(plan.shots)} 章节分镜已就绪，"
                "开始生成统一风格首帧…"
            ),
            stage="first_frames",
            percent=46,
        )
        return job

    @staticmethod
    def _first_frame_prompt(
        job: VideoJob,
        shot: StoryboardShot,
        *,
        has_reference_image: bool = False,
    ) -> str:
        settings = QijiaVideoService._generation_settings(job)
        frame_prompt = shot.first_frame_prompt.strip()[:750]
        style_direction = (
            "【视觉基准】已提供的全局参考图是画风、色彩、光影、材质、人物造型与视觉"
            "气质的最高优先级。严格延续参考图，只根据本镜头内容重新组织场景、动作和"
            "构图；不要采用其他文字设定中的艺术风格，也不要照搬参考图里的文字、Logo"
            "或水印。"
            if has_reference_image
            else f"【全局视觉导演设定】{settings.seedance_prompt.strip()[:750]}"
        )
        return (
            f"{style_direction}\n"
            f"【静止首帧】{frame_prompt}\n"
            "主体关系清楚，构图简洁，优先保证核心变化或信息关系一眼可懂。\n"
            "只生成一张竖屏 9:16 画面首帧。画面中不得出现任何文字、字幕、"
            "字母、数字、Logo、水印、可读书页、屏幕界面或品牌标识；"
            "底部保留干净的字幕安全区。"
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
        source_card = SourceCard.model_validate(job.source_card_snapshot)
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
                    has_reference_image=bool(reference_asset),
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
    def _seedance_style_context(cls, job: VideoJob) -> str:
        if cls._has_reference_image(job):
            return (
                "【视觉基准】严格以已提供的首帧为唯一画风、色彩、光影、材质和人物造型"
                "基准；任何文字描述与首帧冲突时，以首帧为准。"
            )
        settings = cls._generation_settings(job)
        return settings.seedance_prompt.strip()[:2000]

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
            style_context = cls._seedance_style_context(job)
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
            for shot_index, shot in enumerate(job.storyboard_plan.shots):
                if shot.visual_type != "video":
                    continue
                candidate = candidates_by_id.get(shot.selected_candidate_id)
                if not candidate or not candidate.asset:
                    raise QualityGateFailed(
                        f"分镜 {shot.shot_id} 缺少已选中的首帧资产"
                    )
                opening_direction = (
                    "【抖音开场执行】这是全片第一个镜头。首帧已经处在冲突、反差或关键选择中；"
                    "人物动作从第一帧立即发生，不要空镜、缓慢入场或先建立环境。前 2 秒让关系"
                    "与矛盾清楚，前 5 秒通过自然反应或构图变化提供第二层信息。\n"
                    if shot_index == 0
                    else ""
                )
                prompt = (
                    f"{style_context}\n"
                    f"{opening_direction}"
                    f"【本镜头视觉意图】{shot.visual_intent[:450]}\n"
                    "【首帧驱动】严格从已提供的首帧自然延展，保持人物、服装、"
                    "空间、构图、配色和画风稳定。\n"
                    f"【动作与运镜】{shot.motion_prompt[:1000]}\n"
                    "本镜头只安排一个清楚可信的动作和一种克制运镜，结尾保持自然。"
                    "只生成自然动画画面，不生成旁白或模型音频。不得新增任何文字、"
                    "字幕、字母、数字、Logo、水印、可读书页、屏幕界面或品牌标识。"
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
            return None
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
                    SEEDANCE_FLAGSHIP_MODEL,
                    SEEDANCE_RETIRED_MODEL,
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
        if job.storyboard_plan:
            candidates_by_id = {
                item.candidate_id: item for item in job.first_frame_candidates
            }
            for shot in job.storyboard_plan.shots:
                candidate = candidates_by_id.get(shot.selected_candidate_id)
                if candidate and candidate.asset:
                    selected_frames[shot.shot_id] = candidate.asset

        video_by_shot = {
            request.request_id: asset
            for request, asset in zip(job.visual_requests, visual_assets)
        }
        if job.storyboard_plan and selected_frames:
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
                if shot.visual_type == "video" and video_asset:
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
        cover_asset = (
            selected_frames.get(job.storyboard_plan.shots[0].shot_id)
            if job.storyboard_plan and job.storyboard_plan.shots
            else None
        )
        required_visual_asset_ids = {
            block.asset_id for block in blocks if block.asset_id
        }
        if cover_asset:
            required_visual_asset_ids.add(cover_asset.asset_id)
        render_visual_assets = [
            item
            for item in [*visual_assets, *selected_frames.values()]
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
                storyboard_shot = next(
                    (
                        item
                        for item in candidate.storyboard_plan.shots
                        if item.shot_id == request.request_id
                    ),
                    None,
                )
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
        candidate.review_bundle_hash = ""
        candidate.approvals = [
            item for item in candidate.approvals if item.kind != "final"
        ]

        self._report(
            progress,
            message="新镜头已就绪，正在用 Remotion 更新成片…",
            stage="remotion",
            percent=78,
            workflow="shot_edit",
            shot_id=request.request_id,
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
            shot_id=request.request_id,
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
            f"qijia-video/{job.id}/renders/shot-edits/"
            f"r{job.revision + 1}-{version_id}"
        )
        draft_asset = await self.storage.put_file(
            object_key=f"{object_prefix}/draft.mp4",
            path=draft_path,
            asset_id=f"draft_video_{version_id}_r{job.revision + 1}",
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
            asset_id=f"cover_{version_id}_r{job.revision + 1}",
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
                    f"{name.replace('.', '_')}_{version_id}_r{job.revision + 1}"
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
            message="镜头版本和成片已更新，等待你确认…",
            stage="confirm_final",
            percent=90,
            workflow="shot_edit",
            shot_id=request.request_id,
        )
        return saved

    async def regenerate_shot(
        self,
        job_id: str,
        shot_id: str,
        prompt: str,
        expected_selected_fingerprint: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
        first_frame_candidate_id: str = "",
        seedance_model: str = "",
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
        cleaned_prompt = str(prompt or "").strip()
        if not cleaned_prompt:
            raise QualityGateFailed("镜头提示词不能为空")
        requested_model = str(seedance_model or "").strip()
        if not requested_model:
            requested_model = self._seedance_model_for_request(job, current)
        if requested_model not in {
            SEEDANCE_EFFICIENT_MODEL,
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
                        and item.request.prompt == cleaned_prompt
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
                        "prompt": cleaned_prompt,
                        "model_id": requested_model,
                        "seed": secrets.randbits(32),
                        "first_frame_asset_id": requested_frame_asset_id,
                    })
                    if request.fingerprint() not in known_fingerprints:
                        break
                self._report(
                    progress,
                    message=f"正在提交 AI 镜头 {shot_number}/5 的新版本…",
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
                    message=f"AI 镜头 {shot_number}/5 的新版本生成中…",
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
        card = SourceCard.model_validate(job.source_card_snapshot)
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
        source_card_path = support / "source_card.json"
        source_card_path.write_text(
            json.dumps(card.model_dump(mode="json"), ensure_ascii=False, indent=2),
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
        return [
            ("script.json", script_path, "application/json"),
            ("render_manifest.json", render_path, "application/json"),
            ("quality_report.json", quality_path, "application/json"),
            ("caption.md", caption_path, "text/markdown"),
            ("sources.md", sources_path, "text/markdown"),
            ("subtitles.srt", srt_path, "application/x-subrip"),
            ("source_card.json", source_card_path, "application/json"),
            ("narration_manifest.json", narration_path, "application/json"),
            ("visual_requests.json", video_requests_path, "application/json"),
            ("video_tasks.json", video_tasks_path, "application/json"),
            ("storyboard_plan.json", storyboard_path, "application/json"),
            ("first_frame_manifest.json", first_frames_path, "application/json"),
            ("skill_snapshot.json", skill_path, "application/json"),
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
            "source_card_id": job.source_card_id,
            "source_card_revision": job.source_card_revision,
            "content_skill": (
                job.skill_snapshot.model_dump(mode="json")
                if job.skill_snapshot
                else None
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
            card = SourceCard.model_validate(job.source_card_snapshot)
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
                video_shot_ids = {
                    shot.shot_id
                    for shot in job.storyboard_plan.shots
                    if shot.visual_type == "video"
                }
                image_shot_ids = {
                    shot.shot_id
                    for shot in job.storyboard_plan.shots
                    if shot.visual_type == "image"
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
