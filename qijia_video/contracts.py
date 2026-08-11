"""可迁移的领域契约，不依赖继续追问主站代码。"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator

from qijia_video.prompts import (
    DEFAULT_SCRIPT_PROMPT,
    DEFAULT_SEEDANCE_PROMPT,
    MAX_IMAGE_CHAPTER_COUNT,
)
from qijia_video.tts_options import (
    DEFAULT_TTS_SPEED_RATIO,
    DEFAULT_TTS_VOICE_ID,
    LEGACY_TTS_SPEED_RATIO,
    TtsSpeedRatio,
    TtsVoiceId,
)

SCHEMA_VERSION = "1.0"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_VISUAL_STYLE_ID = "content-skill-default"
H3_PROMPT_WRITING_PROFILE_ID = "h3-prompt-writing"
DEFAULT_PROMPT_WRITING_PROFILE_ID = H3_PROMPT_WRITING_PROFILE_ID
DEFAULT_PROMPT_ADAPTER_ID = H3_PROMPT_WRITING_PROFILE_ID
DEFAULT_SCRIPT_SKILL_ID = 'insight-led-scriptwriter'
DEFAULT_DIRECTOR_SKILL_ID = 'animated-explainer'
DEFAULT_PROVIDER_ADAPTER_ID = 'seedream-seedance'
SEEDANCE_EFFICIENT_MODEL = "doubao-seedance-1-0-pro-fast-251015"
SEEDANCE_RETIRED_MODEL = "doubao-seedance-1-5-pro-251215"
SEEDANCE_FLAGSHIP_MODEL = "doubao-seedance-2-0-260128"
SeedanceModelId = Literal[
    "doubao-seedance-1-0-pro-fast-251015",
    "doubao-seedance-1-5-pro-251215",
    "doubao-seedance-2-0-260128",
]


class ContractModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        validate_assignment=True,
    )


class ContentDomain(StrEnum):
    PARENT_EDUCATION = "parent_education"
    DEVELOPMENTAL_PSYCHOLOGY = "developmental_psychology"
    EDUCATIONAL_PSYCHOLOGY = "educational_psychology"
    PARENT_CHILD_RELATIONSHIP = "parent_child_relationship"
    PARENT_GROWTH = "parent_growth"
    GENERAL_KNOWLEDGE = "general_knowledge"
    TECHNOLOGY = "technology"
    BUSINESS = "business"
    GENERAL_NEWS = "general_news"


class ContentFormat(StrEnum):
    PERSON_IDEA = "person_idea_explainer"
    RESEARCH = "research_explainer"
    CONCEPT = "concept_explainer"
    BOOK = "book_explainer"
    RECENT_NEWS = "recent_news_briefing"


class SkillInputMode(StrEnum):
    CREATIVE_REQUEST = "creative_request"
    PERSON_VIEWPOINT = "person_viewpoint"
    RECENT_NEWS_TOPIC = "recent_news_topic"


class SkillResearchMode(StrEnum):
    NONE = "none"
    PERSON_VIEWPOINT_OPTIONAL = "person_viewpoint_optional"
    RECENT_NEWS_REQUIRED = "recent_news_required"


class SkillKnowledgeMode(StrEnum):
    """How a Content Skill may obtain background knowledge.

    ``LEGACY_EXTERNAL_RESEARCH`` is the safe default for snapshots persisted
    before this field existed. It prevents an old web-search workflow from
    being silently reinterpreted as model-only generation.
    """

    MODEL_KNOWLEDGE = "model_knowledge"
    LEGACY_EXTERNAL_RESEARCH = "legacy_external_research"


class PipelineVersion(StrEnum):
    LEGACY = 'v1'
    SINGLE_OWNER = 'v2'
    DIRECT_SCRIPT = 'v3'
    QUALITY_FIRST = 'v4'


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class SourceCardStatus(StrEnum):
    DRAFT = "draft"
    VERIFIED = "verified"


class JobState(StrEnum):
    CARD_VERIFIED = "card_verified"
    SCRIPT_GENERATING = "script_generating"
    SCRIPT_REVIEW_REQUIRED = "script_review_required"
    SCRIPT_APPROVED = "script_approved"
    PRODUCING = "producing"
    MEDIA_REVIEW_REQUIRED = "media_review_required"
    QUALITY_CHECKING = "quality_checking"
    FINAL_REVIEW_REQUIRED = "final_review_required"
    FINAL_APPROVED = "final_approved"
    PACKAGED = "packaged"
    FAILED = "failed"


class PreGenerationMediaMode(StrEnum):
    """Whether production pauses before paid visual generation."""

    AUTOMATIC = "automatic"
    REVIEW_BEFORE_GENERATION = "review_before_generation"
    CONFIRMED = "confirmed"


class ProviderTaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class Actor(ContractModel):
    user_id: int | None = None
    username: str = ""
    role: Literal["admin", "member", "system"] = "member"

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Subject(ContractModel):
    type: Literal[
        "person",
        "research",
        "concept",
        "book",
        "organization",
        "event",
        "topic",
    ]
    name: str = Field(min_length=1, max_length=300)


class SourceEntry(ContractModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    type: Literal["book", "paper", "article", "official", "other"]
    title: str = Field(min_length=1, max_length=500)
    author: str = Field(default="", max_length=300)
    publisher: str = Field(default="", max_length=300)
    edition: str = Field(default="", max_length=200)
    locator: str = Field(default="", max_length=300)
    url: str = Field(default="", max_length=2000)
    accessed_at: str = Field(default="", max_length=64)
    rights_status: Literal[
        "verified_for_citation", "licensed", "public_domain", "unknown"
    ] = "unknown"

    @model_validator(mode="after")
    def validate_locator(self):
        if self.url and not self.url.startswith(("https://", "http://")):
            raise ValueError("来源 URL 必须使用 http 或 https")
        if not self.locator and not self.url:
            raise ValueError("来源必须填写页码/章节或 URL")
        return self


class VerifiedFact(ContractModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=2000)
    source_refs: list[str] = Field(min_length=1)


class VerifiedQuote(ContractModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=2000)
    source_id: str = Field(min_length=1, max_length=64)


class InterpretationBoundary(ContractModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    text: str = Field(min_length=1, max_length=1000)


class SourceCardInput(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    content_domain: ContentDomain
    content_format: ContentFormat
    target_audience: str = Field(default="parents", min_length=1, max_length=160)
    risk_level: RiskLevel = RiskLevel.LOW
    subject: Subject
    title: str = Field(min_length=1, max_length=300)
    core_idea: str = Field(min_length=1, max_length=2000)
    parent_question: str = Field(min_length=1, max_length=500)
    # Raw creator input is not evidence. Research-backed workflows may start
    # with an empty evidence set and populate it after retrieval.
    sources: list[SourceEntry] = Field(default_factory=list, max_length=20)
    verified_facts: list[VerifiedFact] = Field(default_factory=list, max_length=50)
    verified_quotes: list[VerifiedQuote] = Field(default_factory=list, max_length=30)
    interpretation_boundary: list[InterpretationBoundary] = Field(
        default_factory=list, max_length=30
    )
    pronunciations: dict[str, str] = Field(default_factory=dict)
    # Keep a list in the persisted contract so the workflow can evolve to
    # multiple visual references later.  The validation MVP deliberately
    # exposes exactly one optional global image.
    reference_assets: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=1,
    )

    @model_validator(mode="after")
    def validate_references(self):
        source_ids = [item.id for item in self.sources]
        fact_ids = [item.id for item in self.verified_facts]
        quote_ids = [item.id for item in self.verified_quotes]
        boundary_ids = [item.id for item in self.interpretation_boundary]
        for label, values in (
            ("来源", source_ids),
            ("事实", fact_ids),
            ("引文", quote_ids),
            ("解释边界", boundary_ids),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"{label} ID 必须唯一")
        known_sources = set(source_ids)
        for fact in self.verified_facts:
            unknown = set(fact.source_refs) - known_sources
            if unknown:
                raise ValueError(f"事实 {fact.id} 引用了不存在的来源：{sorted(unknown)}")
        for quote in self.verified_quotes:
            if quote.source_id not in known_sources:
                raise ValueError(f"引文 {quote.id} 引用了不存在的来源")
        for raw_asset in self.reference_assets:
            asset = AssetRef.model_validate(raw_asset)
            if asset.media_type not in ("image/jpeg", "image/png", "image/webp"):
                raise ValueError("全局参考素材只支持 JPG、PNG 或 WebP 图片")
        return self


class CreativeRequestInput(ContractModel):
    """One immutable natural-language request for model-led creation."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    creative_request: str = Field(min_length=10, max_length=1800)

    @model_validator(mode="after")
    def normalize_request(self):
        normalized = self.creative_request.strip()
        if len(normalized) < 10:
            raise ValueError("请用至少 10 个字说明你想做的内容")
        object.__setattr__(self, "creative_request", normalized)
        return self

    def to_source_card_input(self) -> SourceCardInput:
        """Compatibility adapter for v1/v2 clients; v3 does not persist it."""

        request = self.creative_request
        compact = re.sub(r"\s+", " ", request).strip()
        return SourceCardInput(
            content_domain=ContentDomain.GENERAL_KNOWLEDGE,
            content_format=ContentFormat.PERSON_IDEA,
            target_audience="希望理解这项人物、观点或主题的普通中文受众",
            subject={"type": "topic", "name": compact[:120]},
            title=compact[:300],
            core_idea=request,
            parent_question="这项创作请求中最值得核验和解释的中心问题是什么？",
            sources=[],
            verified_facts=[],
            interpretation_boundary=[{
                "id": "boundary_01",
                "text": (
                    "原始创作请求是本次内容意图与主要材料。脚本模型可以使用其已有的"
                    "稳定知识帮助解释，但不会联网核验；人物归属、逐字引语、精确出处、"
                    "版本、日期和最新动态不得假装已经查证，存在疑问时应降格表述并交由人工确认。"
                ),
            }],
        )


class CreativeMaterial(ContractModel):
    """Creator-verified material supplied with one direct creative request."""

    title: str = Field(default="用户核对材料", min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=6000)
    url: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_url(self):
        if self.url and not self.url.startswith(("https://", "http://")):
            raise ValueError("资料 URL 必须使用 http 或 https")
        return self


class CreativeInputSnapshot(ContractModel):
    """Immutable v3 intake. It is not a SourceCard or an editorial plan."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    original_request: str = Field(min_length=10, max_length=5000)
    display_title: str = Field(min_length=1, max_length=300)
    verified_materials: list[CreativeMaterial] = Field(
        default_factory=list,
        max_length=20,
    )
    reference_assets: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=1,
    )
    created_at: str = ""

    @model_validator(mode="after")
    def validate_references(self):
        for raw_asset in self.reference_assets:
            asset = AssetRef.model_validate(raw_asset)
            if asset.media_type not in ("image/jpeg", "image/png", "image/webp"):
                raise ValueError("全局参考素材只支持 JPG、PNG 或 WebP 图片")
        return self

class PersonViewpointInput(ContractModel):
    """Compatibility input for clients that still submit separate legacy fields."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    person_name: str = Field(min_length=1, max_length=120)
    viewpoint: str = Field(min_length=10, max_length=1800)

    def to_source_card_input(self) -> SourceCardInput:
        person_name = re.sub(r"\s+", " ", self.person_name).strip()
        viewpoint = re.sub(r"\s+", " ", self.viewpoint).strip()
        return SourceCardInput(
            content_domain=ContentDomain.GENERAL_KNOWLEDGE,
            content_format=ContentFormat.PERSON_IDEA,
            target_audience="关注该人物与观点的普通中文受众",
            subject={"type": "person", "name": person_name},
            title=f"{person_name}：{viewpoint}"[:300],
            core_idea=viewpoint,
            parent_question=(
                f"这段观点在{person_name}的原始语境中是什么意思，今天为什么值得理解？"
            ),
            sources=[],
            verified_facts=[],
            interpretation_boundary=[{
                "id": "boundary_01",
                "text": (
                    "输入中的人物与表述是用户提供的创作材料，不代表系统已经核验其逐字"
                    "归属、出处与语境。可以结合模型已有知识解释观点，但不得虚构精确来源，"
                    "不确定时应写成观点解读而不是人物原话。"
                ),
            }],
        )


class NewsTopicInput(ContractModel):
    """Minimal input for a Skill that must research current public news."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    topic: str = Field(min_length=2, max_length=300)
    focus: str = Field(default="", max_length=1000)
    target_audience: str = Field(
        default="关注该主题的普通用户",
        min_length=1,
        max_length=160,
    )
    content_domain: ContentDomain = ContentDomain.TECHNOLOGY

    @model_validator(mode="after")
    def validate_news_domain(self):
        if self.content_domain not in {
            ContentDomain.TECHNOLOGY,
            ContentDomain.BUSINESS,
            ContentDomain.GENERAL_NEWS,
        }:
            raise ValueError("最新新闻 Skill 仅支持科技、商业或通用新闻领域")
        return self

    def to_source_card_input(self) -> SourceCardInput:
        topic = re.sub(r"\s+", " ", self.topic).strip()
        focus = re.sub(r"\s+", " ", self.focus).strip()
        return SourceCardInput(
            content_domain=self.content_domain,
            content_format=ContentFormat.RECENT_NEWS,
            target_audience=self.target_audience,
            subject={"type": "topic", "name": topic},
            title=(focus or f"{topic} 最新公开动态")[:300],
            core_idea=focus or f"检索并解释 {topic} 截至任务创建时刻的最新公开动态。",
            parent_question=f"{topic} 最近发生了什么，为什么值得关注？"[:500],
            sources=[],
            verified_facts=[],
            interpretation_boundary=[{
                "id": "news_boundary_01",
                "text": (
                    "用户输入的主题和关注角度不是新闻证据。必须先完成联网研究，"
                    "只使用可与检索注释匹配的 research_fact；区分事件时间、发布时间、"
                    "官方计划、第三方判断与未确认信息。"
                ),
            }],
        )


class QuickSourceCardInput(ContractModel):
    """低认知负担的来源输入；完整证据结构由领域层统一整理。"""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    title: str = Field(min_length=1, max_length=300)
    source_material: str = Field(min_length=10, max_length=2000)
    rights_confirmed: Literal[True]
    # 编辑命题与已核验资料分开保存，避免把“建议角度”误标成来源事实。
    editorial_brief: str = Field(default="", max_length=600)
    parent_question: str = Field(default="", max_length=240)
    content_domain: ContentDomain = ContentDomain.PARENT_EDUCATION
    content_format: ContentFormat = ContentFormat.CONCEPT
    source_type: Literal["book", "paper", "article", "official", "other"] = "other"
    source_title: str = Field(default="", max_length=500)
    source_author: str = Field(default="", max_length=300)
    source_publisher: str = Field(default="", max_length=300)
    source_edition: str = Field(default="", max_length=200)
    source_locator: str = Field(default="", max_length=300)
    source_url: str = Field(default="", max_length=2000)
    boundary: str = Field(default="", max_length=1000)

    def _material_parts(self) -> tuple[str, str]:
        match = re.search(r"https?://[^\s<>\"']+", self.source_material)
        detected_url = ""
        fact_text = self.source_material
        if match:
            detected_url = match.group(0).rstrip("，。；、!?！？)]}）】》")
            fact_text = fact_text.replace(match.group(0), " ", 1)
        fact_text = re.sub(r"\s+", " ", fact_text).strip()
        return fact_text, self.source_url or detected_url

    @model_validator(mode="after")
    def validate_quick_material(self):
        fact_text, source_url = self._material_parts()
        if len(fact_text) < 10:
            raise ValueError("除链接外，请再粘贴至少 10 个字的关键内容或资料摘记")
        if source_url and not source_url.startswith(("https://", "http://")):
            raise ValueError("来源 URL 必须使用 http 或 https")
        return self

    def to_source_card_input(self) -> SourceCardInput:
        fact_text, source_url = self._material_parts()
        source_type = self.source_type
        if source_type == "other" and source_url:
            source_type = "article"
        if self.source_title:
            source_title = self.source_title
        elif source_url:
            host = urlparse(source_url).hostname or "网络来源"
            source_title = f"网络来源：{host}"
        else:
            source_title = f"用户提供材料：{self.title}"[:500]
        if self.content_format == ContentFormat.PERSON_IDEA:
            subject_type = "person"
        elif self.content_format == ContentFormat.RESEARCH:
            subject_type = "research"
        elif self.content_format == ContentFormat.BOOK or source_type == "book":
            subject_type = "book"
        else:
            subject_type = "concept"
        parent_question = self.parent_question or (
            self.title
            if self.title.endswith(("？", "?"))
            else f"关于“{self.title}”，家长最需要理解什么？"
        )
        boundary = self.boundary or (
            "不得将一般性教育或心理学内容表述为对具体儿童的诊断或治疗方案。"
        )
        return SourceCardInput(
            content_domain=self.content_domain,
            content_format=self.content_format,
            subject={"type": subject_type, "name": self.title},
            title=self.title,
            core_idea=self.editorial_brief or fact_text,
            parent_question=parent_question,
            sources=[{
                "id": "source_01",
                "type": source_type,
                "title": source_title,
                "author": self.source_author,
                "publisher": self.source_publisher,
                "edition": self.source_edition,
                "locator": self.source_locator or ("" if source_url else "用户粘贴的参考材料"),
                "url": source_url,
                "accessed_at": datetime.now(BEIJING_TZ).date().isoformat() if source_url else "",
                "rights_status": "verified_for_citation",
            }],
            verified_facts=[{
                "id": "fact_01",
                "text": fact_text,
                "source_refs": ["source_01"],
            }],
            interpretation_boundary=[{"id": "boundary_01", "text": boundary}],
        )


class SourceCard(SourceCardInput):
    id: str
    revision: int = Field(default=1, ge=1)
    status: SourceCardStatus = SourceCardStatus.DRAFT
    reviewed_by: str = ""
    reviewed_at: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""


class PersonResearchEvidence(ContractModel):
    """One web-grounded research point used by a person-viewpoint job."""

    claim: str = Field(min_length=1, max_length=1200)
    source_title: str = Field(min_length=1, max_length=500)
    source_url: str = Field(min_length=1, max_length=2000)
    source_kind: Literal[
        "official", "primary", "independent", "other"
    ] = "other"
    evidence_type: Literal[
        "attribution",
        "source_context",
        "biography",
        "interpretation",
        "current_relevance",
        "other",
    ] = "other"
    published_at: str = Field(default="", max_length=64)
    event_at: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_source_url(self):
        if not self.source_url.startswith(("https://", "http://")):
            raise ValueError("研究简报来源 URL 必须使用 http 或 https")
        return self


class PersonResearchBrief(ContractModel):
    """Automatic, cited context that enriches one person-viewpoint job."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    kind: Literal["person_viewpoint"] = "person_viewpoint"
    person_name: str = Field(min_length=1, max_length=120)
    viewpoint: str = Field(min_length=10, max_length=1800)
    input_type: Literal[
        "attributed_quote",
        "paraphrased_viewpoint",
        "conceptual_claim",
        "unknown",
    ] = "unknown"
    research_focus: str = Field(default="", max_length=1200)
    attribution_status: Literal[
        "verified",
        "partially_supported",
        "unverified",
        "not_applicable",
    ] = "unverified"
    verified_wording: str = Field(default="", max_length=1800)
    attribution_note: str = Field(default="", max_length=1600)
    source_context: str = Field(default="", max_length=2000)
    summary: str = Field(min_length=1, max_length=2000)
    # Pipeline v1 compatibility fields. v2 research leaves them empty because
    # editorial judgment belongs exclusively to the frozen Script Skill.
    core_tension: str = Field(default="", max_length=1200)
    audience_relevance: list[str] = Field(default_factory=list, max_length=6)
    content_angles: list[str] = Field(default_factory=list, max_length=5)
    interaction_opportunity: str = Field(default="", max_length=1000)
    evidence: list[PersonResearchEvidence] = Field(
        default_factory=list, min_length=1, max_length=8
    )
    uncertainties: list[str] = Field(default_factory=list, max_length=8)
    model_id: str = Field(default="", max_length=256)
    prompt_version: str = Field(default="", max_length=128)
    generated_at: str = Field(default="", max_length=64)


class NewsResearchBrief(ContractModel):
    """Cited, time-frozen research required by the recent-news Skill."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    kind: Literal["recent_news"] = "recent_news"
    topic: str = Field(min_length=2, max_length=300)
    as_of: str = Field(min_length=1, max_length=64)
    summary: str = Field(min_length=1, max_length=2000)
    # Compatibility-only editorial fields. Research now emits evidence and
    # uncertainty, not hooks, angles or audience strategy.
    core_tension: str = Field(default="", max_length=1200)
    audience_relevance: list[str] = Field(default_factory=list, max_length=6)
    content_angles: list[str] = Field(default_factory=list, max_length=5)
    interaction_opportunity: str = Field(default="", max_length=1000)
    evidence: list[PersonResearchEvidence] = Field(
        default_factory=list, min_length=1, max_length=10
    )
    uncertainties: list[str] = Field(default_factory=list, max_length=10)
    model_id: str = Field(default="", max_length=256)
    prompt_version: str = Field(default="", max_length=128)
    generated_at: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_news_research(self):
        try:
            frozen_at = datetime.fromisoformat(self.as_of)
        except ValueError as exc:
            raise ValueError("最新新闻检索截止时间必须是 ISO 8601") from exc
        if frozen_at.tzinfo is None:
            raise ValueError("最新新闻检索截止时间必须包含时区")
        return self


class ResearchDiagnostics(ContractModel):
    """Bounded citation-matching diagnostics safe to expose to an editor."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    operation: Literal["recent_news_research"] = "recent_news_research"
    attempt_count: int = Field(default=0, ge=0)
    web_search_requests: int | None = Field(default=None, ge=0)
    citation_count: int = Field(default=0, ge=0)
    candidate_evidence_count: int = Field(default=0, ge=0)
    matched_citation_count: int = Field(default=0, ge=0)
    accepted_evidence_count: int = Field(default=0, ge=0)
    accepted_site_count: int = Field(default=0, ge=0)
    accepted_timed_evidence_count: int | None = Field(default=None, ge=0)
    citation_excerpt_claim_count: int = Field(default=0, ge=0)
    citation_identity_samples: list[str] = Field(
        default_factory=list, max_length=5
    )
    candidate_identity_samples: list[str] = Field(
        default_factory=list, max_length=5
    )
    unexpected_response_fields: list[str] = Field(
        default_factory=list, max_length=10
    )
    validation_errors: list[str] = Field(default_factory=list, max_length=10)
    rejected_counts: dict[str, int] = Field(default_factory=dict)
    detail: str = Field(default="", max_length=1000)
    generated_at: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def restore_legacy_derived_counts(self):
        post_match_reasons = (
            "missing_claim",
            "duplicate_url",
            "url_too_long",
            "missing_host",
        )
        inferred_matches = self.accepted_evidence_count + sum(
            max(0, int(self.rejected_counts.get(reason, 0)))
            for reason in post_match_reasons
        )
        if self.matched_citation_count < inferred_matches:
            self.matched_citation_count = inferred_matches
        if self.web_search_requests == 0 and self.citation_count > 0:
            self.web_search_requests = None
        return self


class ScriptBeat(ContractModel):
    """One semantic script beat; visual_direction is v2 compatibility only."""

    id: str = Field(min_length=1, max_length=64)
    narration: str = Field(min_length=1, max_length=2000)
    role: Literal[
        "hook",
        "suspense",
        "context",
        "reframe",
        "explanation",
        "example",
        "application",
        "closing",
    ]
    # Historical ScriptDraft v2 field. Pipeline v2 assigns visuals only after
    # the editor has confirmed the complete narration.
    visual_direction: str = Field(default="", max_length=1200)
    on_screen_text: str = Field(default="", max_length=80)
    source_refs: list[str] = Field(default_factory=list)
    quote_ref: str | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_v1_segment(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        if "narration" not in normalized and "text" in normalized:
            normalized["narration"] = normalized.pop("text")
        if "role" not in normalized and "segment_type" in normalized:
            normalized["role"] = normalized.pop("segment_type")
        return normalized

    # Python-level compatibility for persisted jobs and integrations written
    # against ScriptDraft v1. New JSON uses narration/role exclusively.
    @property
    def text(self) -> str:
        return self.narration

    @text.setter
    def text(self, value: str):
        self.narration = value

    @property
    def segment_type(self) -> str:
        return self.role

    @segment_type.setter
    def segment_type(self, value: str):
        self.role = value


# Import compatibility. The domain concept is a ScriptBeat in v2.
NarrationSegment = ScriptBeat


class ScriptDraft(ContractModel):
    schema_version: Literal["1.0", "2.0", "3.0"] = "3.0"
    source_card_id: str
    source_card_revision: int = Field(ge=1)
    video_title: str = Field(min_length=1, max_length=200)
    cover_text: str = Field(min_length=1, max_length=40)
    hook: str = Field(min_length=1, max_length=300)
    beats: list[ScriptBeat] = Field(min_length=3, max_length=12)
    closing: str = Field(min_length=1, max_length=500)
    estimated_duration_seconds: int = Field(default=60, ge=45, le=75)
    caption: str = Field(min_length=1, max_length=2000)
    hashtags: list[str] = Field(default_factory=list, max_length=12)

    @model_validator(mode="before")
    @classmethod
    def accept_v1_draft(cls, value):
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        used_v1_field = "beats" not in normalized and "narration_segments" in normalized
        if used_v1_field:
            normalized["beats"] = normalized.pop("narration_segments")
            normalized.setdefault("schema_version", "1.0")
        return normalized

    @model_validator(mode="after")
    def validate_v2_tracks(self):
        beat_ids = [item.id for item in self.beats]
        if len(beat_ids) != len(set(beat_ids)):
            raise ValueError("脚本叙事段 ID 必须唯一")
        if self.schema_version == "2.0":
            missing_visuals = [item.id for item in self.beats if not item.visual_direction]
            if missing_visuals:
                raise ValueError(
                    "ScriptDraft v2 的每个叙事段都必须包含 visual_direction："
                    + "、".join(missing_visuals)
                )
        if self.schema_version in {"2.0", "3.0"}:
            if self.beats[0].role != "hook" or self.beats[-1].role != "closing":
                raise ValueError("ScriptDraft 必须以 hook 开始并以 closing 收束")
            # Compatibility fields are derived mirrors, never a second source
            # of truth in v2/v3.
            object.__setattr__(self, "hook", self.beats[0].narration)
            object.__setattr__(self, "closing", self.beats[-1].narration)
        return self

    @model_serializer(mode="wrap")
    def serialize_versioned(self, handler):
        data = handler(self)
        if self.schema_version == "1.0":
            data["narration_segments"] = [
                {
                    "id": item.id,
                    "text": item.narration,
                    "segment_type": item.role,
                    "source_refs": list(item.source_refs),
                    "quote_ref": item.quote_ref,
                }
                for item in self.beats
            ]
            data.pop("beats", None)
        return data

    @property
    def narration_segments(self) -> list[ScriptBeat]:
        return self.beats

    def narration_text(self) -> str:
        return "\n".join(item.narration for item in self.beats)


class ScriptReview(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    passed: bool = False
    claim_checks: list[dict[str, Any]] = Field(default_factory=list)
    quote_checks: list[dict[str, Any]] = Field(default_factory=list)
    boundary_checks: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    quality_scores: dict[str, int] = Field(default_factory=dict)
    strengths: list[str] = Field(default_factory=list, max_length=6)
    revision_requests: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=12,
    )
    factual_risks: list[dict[str, Any]] = Field(
        default_factory=list,
        max_length=12,
    )
    preserve: list[str] = Field(default_factory=list, max_length=8)
    reviewed_draft_hash: str = Field(
        default="",
        pattern=r"^$|^[a-f0-9]{64}$",
    )
    model_id: str = ""
    prompt_version: str = ""
    input_hash: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    reviewed_at: str = ""


class ContentSkillSnapshot(ContractModel):
    """Immutable workflow definition frozen before any paid task is started."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    skill_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    input_mode: SkillInputMode
    compatible_formats: list[ContentFormat] = Field(min_length=1)
    knowledge_mode: SkillKnowledgeMode = (
        SkillKnowledgeMode.LEGACY_EXTERNAL_RESEARCH
    )
    research_mode: SkillResearchMode = SkillResearchMode.NONE
    research_prompt: str = Field(default="", max_length=12000)
    # Compatibility-only prompt fields. Workflow presets created after v2
    # leave these empty and contribute only routing and factual policy.
    script_system_prompt: str = Field(default="", max_length=4000)
    visual_policy: str = Field(default="", max_length=3200)
    policy_ids: list[str] = Field(default_factory=list, max_length=30)
    quality_rules: list[str] = Field(default_factory=list, max_length=30)
    output_schema: Literal["script_draft_v2"] = "script_draft_v2"
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    frozen_at: str = Field(min_length=1, max_length=64)


class ScriptSkillSnapshot(ContractModel):
    '''The one script strategy allowed to own editorial decisions.'''

    schema_version: Literal['1.0'] = SCHEMA_VERSION
    skill_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$',
    )
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=r'^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$',
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    compatible_formats: list[ContentFormat] = Field(min_length=1)
    planning_instructions: str = Field(min_length=1, max_length=6000)
    writing_instructions: str = Field(min_length=1, max_length=8000)
    critic_rules: list[str] = Field(min_length=1, max_length=30)
    manifest_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    frozen_at: str = Field(min_length=1, max_length=64)


class DirectorSkillSnapshot(ContractModel):
    '''The one visual workflow allowed to turn a confirmed script into shots.'''

    schema_version: Literal['1.0'] = SCHEMA_VERSION
    skill_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$',
    )
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=r'^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$',
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    # New Director Skills own a production mode and directing method. The
    # legacy fields remain readable for jobs created before the method/style
    # split, but are empty in new snapshots.
    mode: str = Field(default='legacy-style-director', min_length=1, max_length=64)
    compatible_formats: list[ContentFormat] = Field(default_factory=list)
    workflow_instructions: str = Field(default='', max_length=6000)
    scene_design_rules: str = Field(default='', max_length=8000)
    shot_design_rules: str = Field(default='', max_length=8000)
    continuity_rules: str = Field(default='', max_length=6000)
    media_rules: str = Field(default='', max_length=5000)
    critic_rules: list[str] = Field(default_factory=list, max_length=30)
    directing_instructions: str = Field(default='', max_length=4000)
    storyboard_rules: str = Field(default='', max_length=4000)
    image_art_direction: str = Field(default='', max_length=4000)
    motion_art_direction: str = Field(default='', max_length=4000)
    negative_rules: list[str] = Field(default_factory=list, max_length=30)
    manifest_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    frozen_at: str = Field(min_length=1, max_length=64)

    @model_validator(mode='after')
    def validate_directing_method(self):
        if self.mode == 'legacy-style-director':
            return self
        required_method_parts = (
            self.compatible_formats,
            self.workflow_instructions,
            self.scene_design_rules,
            self.shot_design_rules,
            self.continuity_rules,
            self.media_rules,
            self.critic_rules,
        )
        if not all(required_method_parts):
            raise ValueError('新版 Director Skill 必须包含完整导演方法与质量规则')
        return self


class ProviderAdapterSnapshot(ContractModel):
    '''Last-mile compiler that cannot make editorial or directing choices.'''

    schema_version: Literal['1.0'] = SCHEMA_VERSION
    adapter_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$',
    )
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=r'^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$',
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    image_provider_family: str = Field(min_length=1, max_length=64)
    video_provider_family: str = Field(min_length=1, max_length=64)
    image_framework: str = Field(min_length=1, max_length=4000)
    video_framework: str = Field(min_length=1, max_length=4000)
    reference_policy: str = Field(min_length=1, max_length=3000)
    audio_policy: str = Field(min_length=1, max_length=1000)
    negative_rules: list[str] = Field(default_factory=list, max_length=30)
    manifest_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    frozen_at: str = Field(min_length=1, max_length=64)


class VisualStyleSnapshot(ContractModel):
    """Provider-neutral visual language frozen independently of content logic."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    style_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    director_prompt: str = Field(default="", max_length=3200)
    storyboard_rules: str = Field(default="", max_length=4000)
    image_rules: str = Field(default="", max_length=4000)
    motion_rules: str = Field(default="", max_length=4000)
    negative_rules: list[str] = Field(default_factory=list, max_length=30)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    frozen_at: str = Field(min_length=1, max_length=64)


class PromptWritingProfileSnapshot(ContractModel):
    """Pipeline v1 compatibility snapshot; never used by new v2 jobs."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    profile_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    # All framework fields exist only to resume frozen Pipeline v1 jobs.
    research_framework: str = Field(default="", max_length=6000)
    script_framework: str = Field(default="", max_length=6000)
    creative_brief_framework: str = Field(default="", max_length=6000)
    planning_framework: str = Field(min_length=1, max_length=4000)
    image_framework: str = Field(min_length=1, max_length=4000)
    video_framework: str = Field(min_length=1, max_length=4000)
    reference_policy: str = Field(min_length=1, max_length=3000)
    audio_policy: str = Field(min_length=1, max_length=1000)
    negative_rules: list[str] = Field(default_factory=list, max_length=30)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    frozen_at: str = Field(min_length=1, max_length=64)


class PromptAdapterSnapshot(ContractModel):
    """Frozen internal prompt compiler used before the single Script Skill call."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    adapter_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    version: str = Field(
        min_length=1,
        max_length=32,
        pattern=r"^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    display_name: str = Field(min_length=1, max_length=120)
    description: str = Field(min_length=1, max_length=1000)
    compilation_framework: str = Field(min_length=1, max_length=8000)
    quality_rules: list[str] = Field(min_length=1, max_length=30)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    frozen_at: str = Field(min_length=1, max_length=64)


class ResearchPromptSnapshot(ContractModel):
    """One input-bound research instruction frozen before a paid web call."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    research_mode: SkillResearchMode
    profile_id: str = Field(default="", max_length=64)
    profile_version: str = Field(default="", max_length=32)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt: str = Field(min_length=1, max_length=20000)
    compiled_at: str = Field(min_length=1, max_length=64)


class CreativeBrief(ContractModel):
    """Pipeline v1 H3 decision record retained for historical job recovery."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    central_question: str = Field(min_length=1, max_length=500)
    core_thesis: str = Field(min_length=1, max_length=1200)
    audience_promise: str = Field(min_length=1, max_length=800)
    narrative_arc: list[str] = Field(min_length=3, max_length=8)
    tone: str = Field(min_length=1, max_length=500)
    visual_concept: str = Field(min_length=1, max_length=1000)
    continuity_anchors: list[str] = Field(default_factory=list, max_length=8)
    must_include: list[str] = Field(default_factory=list, max_length=12)
    must_avoid: list[str] = Field(default_factory=list, max_length=12)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    model_id: str = Field(default="", max_length=256)
    prompt_version: str = Field(default="", max_length=128)
    input_hash: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    generated_at: str = Field(default="", max_length=64)


class EditorialAngle(ContractModel):
    angle_id: str = Field(
        min_length=1,
        max_length=32,
        pattern=r'^[a-z0-9]+(?:_[a-z0-9]+)*$',
    )
    premise: str = Field(min_length=1, max_length=800)
    audience_value: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(default_factory=list, max_length=30)
    risk: str = Field(default='', max_length=600)


class EditorialPlan(ContractModel):
    '''Provider-neutral script plan with no visual decisions.'''

    schema_version: Literal['1.0'] = SCHEMA_VERSION
    objective: str = Field(min_length=1, max_length=600)
    central_question: str = Field(min_length=1, max_length=500)
    candidate_angles: list[EditorialAngle] = Field(min_length=2, max_length=3)
    selected_angle_id: str = Field(min_length=1, max_length=32)
    selection_reason: str = Field(min_length=1, max_length=800)
    core_thesis: str = Field(min_length=1, max_length=1200)
    audience_promise: str = Field(min_length=1, max_length=800)
    narrative_arc: list[str] = Field(min_length=3, max_length=8)
    tone: str = Field(min_length=1, max_length=500)
    must_include: list[str] = Field(default_factory=list, max_length=12)
    must_avoid: list[str] = Field(default_factory=list, max_length=12)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    critic_summary: str = Field(min_length=1, max_length=1200)
    model_id: str = Field(default='', max_length=256)
    prompt_version: str = Field(default='', max_length=128)
    input_hash: str = Field(default='', pattern=r'^$|^[a-f0-9]{64}$')
    draft_script_hash: str = Field(default='', pattern=r'^$|^[a-f0-9]{64}$')
    generated_at: str = Field(default='', max_length=64)

    @model_validator(mode='after')
    def validate_selected_angle(self):
        angle_ids = [item.angle_id for item in self.candidate_angles]
        if len(angle_ids) != len(set(angle_ids)):
            raise ValueError('候选脚本角度 ID 必须唯一')
        if self.selected_angle_id not in angle_ids:
            raise ValueError('selected_angle_id 必须引用候选脚本角度')
        return self


class GenerationSettings(ContractModel):
    """创建任务时冻结的生成参数；视觉编排方法由系统固定。"""

    skill_id: str = Field(
        default="",
        max_length=64,
        pattern=r"^$|^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    skill_version: str = Field(
        default="",
        max_length=32,
        pattern=r"^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    script_skill_id: str = Field(
        default=DEFAULT_SCRIPT_SKILL_ID,
        max_length=64,
        pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$',
    )
    script_skill_version: str = Field(
        default='',
        max_length=32,
        pattern=r'^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$',
    )
    visual_style_id: str = Field(
        default=DEFAULT_VISUAL_STYLE_ID,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    visual_style_version: str = Field(
        default="",
        max_length=32,
        pattern=r"^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    director_skill_id: str = Field(
        # Empty remains valid only for persisted/internal v1 callers. The v2
        # public request contract always supplies an explicit Director Skill.
        default='',
        max_length=64,
        pattern=r'^$|^[a-z0-9]+(?:-[a-z0-9]+)*$',
    )
    director_skill_version: str = Field(
        default='',
        max_length=32,
        pattern=r'^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$',
    )
    provider_adapter_id: str = Field(
        default=DEFAULT_PROVIDER_ADAPTER_ID,
        max_length=64,
        pattern=r'^[a-z0-9]+(?:-[a-z0-9]+)*$',
    )
    provider_adapter_version: str = Field(
        default='',
        max_length=32,
        pattern=r'^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$',
    )
    prompt_writing_profile_id: str = Field(
        default='',
        max_length=64,
        pattern=r'^$|^[a-z0-9]+(?:-[a-z0-9]+)*$',
    )
    prompt_writing_profile_version: str = Field(
        default="",
        max_length=32,
        pattern=r"^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    script_prompt: str = Field(
        default=DEFAULT_SCRIPT_PROMPT,
        max_length=8000,
    )
    # Persisted for historical task recovery and as the selected style's
    # internal director resource. New API callers may not override it.
    seedance_prompt: str = Field(
        default=DEFAULT_SEEDANCE_PROMPT,
        max_length=3200,
    )
    video_resolution: Literal["480p", "720p", "1080p"] = "1080p"
    tts_voice_id: TtsVoiceId = DEFAULT_TTS_VOICE_ID
    tts_speed_ratio: TtsSpeedRatio = DEFAULT_TTS_SPEED_RATIO
    # Quality-first tasks use Seedance 2.0 by default. 1.0 Pro Fast remains
    # available as an explicit preview/cost-saving choice.
    seedance_model: SeedanceModelId = SEEDANCE_FLAGSHIP_MODEL
    image_count: int = Field(
        default=0,
        ge=0,
        le=MAX_IMAGE_CHAPTER_COUNT,
    )
    shot_count: int = Field(
        default=0,
        ge=0,
        le=MAX_IMAGE_CHAPTER_COUNT + 3,
    )

    @model_validator(mode="before")
    @classmethod
    def derive_compatible_chapter_counts(cls, value: Any) -> Any:
        """Infer the missing count while preserving persisted five-shot jobs."""

        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        has_images = "image_count" in normalized
        has_shots = "shot_count" in normalized
        if has_shots and not has_images:
            try:
                count = int(normalized["shot_count"])
                normalized["image_count"] = count - 3 if count else 0
            except (TypeError, ValueError):
                pass
        elif has_images and not has_shots:
            try:
                count = int(normalized["image_count"])
                normalized["shot_count"] = count + 3 if count else 0
            except (TypeError, ValueError):
                pass
        return normalized

    @model_validator(mode="after")
    def validate_chapter_counts(self):
        if self.shot_count == 0 and self.image_count == 0:
            return self
        if self.shot_count != self.image_count + 3:
            raise ValueError("历史固定章节设置必须等于 3 段视频加动态图片数量")
        return self


class ShotContextIR(ContractModel):
    '''Observable, provider-neutral direction for one semantic chapter.'''

    semantic_goal: str = Field(min_length=1, max_length=600)
    # v3 makes an observable event and blocking mandatory. Metaphor is an
    # optional supporting device, never the scene's default substance.
    concrete_event: str = Field(default='', max_length=1000)
    blocking: str = Field(default='', max_length=1000)
    visual_metaphor: str = Field(default='', max_length=800)
    subject: str = Field(min_length=1, max_length=600)
    action: str = Field(min_length=1, max_length=600)
    environment: str = Field(min_length=1, max_length=600)
    composition: str = Field(min_length=1, max_length=600)
    continuity_handoff: str = Field(min_length=1, max_length=600)
    start_state: str = Field(min_length=1, max_length=600)
    end_state: str = Field(min_length=1, max_length=600)
    camera_intent: str = Field(min_length=1, max_length=600)
    media_rationale: str = Field(min_length=1, max_length=600)
    reference_roles: list[str] = Field(default_factory=list, max_length=8)


class MultimodalReferenceIR(ContractModel):
    """H3-derived reference-role contract, independent from provider syntax."""

    reference_id: str = Field(min_length=1, max_length=96)
    roles: list[
        Literal["identity", "wardrobe", "object", "location", "style", "composition"]
    ] = Field(min_length=1, max_length=6)
    applies_to: list[str] = Field(default_factory=list, max_length=20)
    retention_level: Literal["strict", "strong", "inspiration"] = "strong"
    preserve: list[str] = Field(default_factory=list, max_length=12)
    allow_change: list[str] = Field(default_factory=list, max_length=12)
    forbidden_transfer: list[str] = Field(default_factory=list, max_length=12)


class DirectorTreatment(ContractModel):
    """Global directing decision made before any shot is planned."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    visual_thesis: str = Field(min_length=1, max_length=1200)
    audience_experience: str = Field(min_length=1, max_length=800)
    chapter_progression: list[str] = Field(min_length=3, max_length=10)
    motif_system: list[str] = Field(min_length=1, max_length=12)
    rhythm_strategy: str = Field(min_length=1, max_length=1000)
    edit_pattern: str = Field(min_length=1, max_length=1000)
    style_application: str = Field(min_length=1, max_length=1200)
    model_id: str = Field(default="", max_length=256)
    input_hash: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    created_at: str = Field(default="", max_length=64)


class AssetBible(ContractModel):
    """Reusable subjects, locations and props locked before shot planning."""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    subjects: list[str] = Field(min_length=1, max_length=20)
    locations: list[str] = Field(min_length=1, max_length=16)
    props: list[str] = Field(default_factory=list, max_length=20)
    identity_locks: list[str] = Field(min_length=1, max_length=20)
    material_locks: list[str] = Field(min_length=1, max_length=20)
    allowed_variations: list[str] = Field(default_factory=list, max_length=16)
    motion_grammar: list[str] = Field(min_length=1, max_length=16)
    review_criteria: list[str] = Field(min_length=2, max_length=20)
    references: list[MultimodalReferenceIR] = Field(
        default_factory=list,
        max_length=8,
    )
    model_id: str = Field(default="", max_length=256)
    input_hash: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    created_at: str = Field(default="", max_length=64)


class VisualBible(ContractModel):
    '''Canonical visual contract; downstream adapters need no Director internals.'''

    schema_version: Literal['1.0'] = SCHEMA_VERSION
    core_visual_idea: str = Field(min_length=1, max_length=1000)
    visual_world: str = Field(min_length=1, max_length=1200)
    recurring_subjects: list[str] = Field(min_length=1, max_length=12)
    scene_anchors: list[str] = Field(min_length=1, max_length=12)
    continuity_rules: list[str] = Field(min_length=2, max_length=16)
    color_material_system: str = Field(min_length=1, max_length=800)
    composition_system: str = Field(min_length=1, max_length=800)
    reference_strategy: str = Field(min_length=1, max_length=800)
    forbidden_elements: list[str] = Field(default_factory=list, max_length=20)
    director_skill_id: str = Field(min_length=1, max_length=64)
    director_skill_version: str = Field(min_length=1, max_length=32)
    model_id: str = Field(default='', max_length=256)
    input_hash: str = Field(default='', pattern=r'^$|^[a-f0-9]{64}$')
    created_at: str = Field(default='', max_length=64)


class StoryboardShot(ContractModel):
    """A semantic shot plan shared by image and video generation providers."""

    shot_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    segment_id: str = Field(min_length=1, max_length=64)
    beat_ids: list[str] = Field(default_factory=list, min_length=0, max_length=8)
    narration_excerpt: str = Field(min_length=1, max_length=2000)
    # Defaults to video so storyboard plans persisted before the hybrid workflow
    # keep their original all-video behavior when they are resumed.
    visual_type: Literal["video", "image"] = "video"
    visual_intent: str = Field(default='', max_length=600)
    first_frame_prompt: str = Field(default='', max_length=1800)
    motion_prompt: str = Field(default='', max_length=1800)
    context: ShotContextIR | None = None
    selected_candidate_id: str = Field(default="", max_length=96)
    # An editor-uploaded image or video can override the AI visual without
    # deleting the generated candidate/version history.
    selected_media_id: str = Field(default="", max_length=96)

    @model_validator(mode="after")
    def normalize_beat_ids(self):
        if not self.beat_ids:
            self.beat_ids = [self.segment_id]
        if self.beat_ids[0] != self.segment_id:
            raise ValueError("segment_id 必须等于 beat_ids 的第一个叙事段")
        if len(self.beat_ids) != len(set(self.beat_ids)):
            raise ValueError("同一镜头不能重复引用叙事段")
        if self.context is None and not all((
            self.visual_intent,
            self.first_frame_prompt,
            self.motion_prompt,
        )):
            raise ValueError('旧版分镜必须包含 visual_intent 和媒体提示词')
        if self.context is not None and not self.visual_intent:
            self.visual_intent = self.context.semantic_goal
        return self


class StoryboardPlan(ContractModel):
    schema_version: Literal['1.0', '2.0', '3.0'] = SCHEMA_VERSION
    shots: list[StoryboardShot] = Field(min_length=3, max_length=13)
    model_id: str = Field(default="", max_length=256)
    prompt_version: str = Field(default="", max_length=128)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str

    @model_validator(mode="after")
    def validate_shots(self):
        shot_ids = [item.shot_id for item in self.shots]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("分镜 ID 必须唯一")
        if self.schema_version == '2.0' and any(
            item.context is None for item in self.shots
        ):
            raise ValueError('StoryboardPlan v2 的每个镜头都必须包含 ShotContextIR')
        if self.schema_version == '3.0':
            if len(self.shots) > 12:
                raise ValueError('StoryboardPlan v3 最多包含 12 个视觉章节')
            if sum(item.visual_type == 'video' for item in self.shots) > 3:
                raise ValueError('StoryboardPlan v3 最多包含 3 个视频章节')
            missing_context = [
                item.shot_id for item in self.shots if item.context is None
            ]
            if missing_context:
                raise ValueError('StoryboardPlan v3 的每个镜头都必须包含 ShotContextIR')
            contexts = [item.context for item in self.shots if item.context]
            missing_events = [
                item.shot_id
                for item in self.shots
                if not item.context.concrete_event or not item.context.blocking
            ]
            if missing_events:
                raise ValueError(
                    'StoryboardPlan v3 必须提供具体事件与主体调度：'
                    + '、'.join(missing_events)
                )
            event_keys = [
                ''.join(item.concrete_event.split()).casefold()
                for item in contexts
            ]
            if len(event_keys) != len(set(event_keys)):
                raise ValueError('StoryboardPlan v3 的具体事件不得重复')
            if any(
                ''.join(item.start_state.split()).casefold()
                == ''.join(item.end_state.split()).casefold()
                for item in contexts
            ):
                raise ValueError('StoryboardPlan v3 的起止状态必须发生可见变化')
            allowed_reference_roles = {
                'identity', 'wardrobe', 'object', 'location', 'style'
            }
            if any(
                role not in allowed_reference_roles
                for item in contexts
                for role in item.reference_roles
            ):
                raise ValueError('StoryboardPlan v3 包含未知的参考图职责')
        return self


class ApprovalRecord(ContractModel):
    kind: Literal["script", "final"]
    actor: str
    artifact_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    approved_at: str
    warnings: list[str] = Field(default_factory=list)


class AssetRef(ContractModel):
    asset_id: str
    object_key: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=0)
    media_type: str
    duration_seconds: float | None = Field(default=None, ge=0)


class ShotMediaVersion(ContractModel):
    """Append-only editor media retained beside the generated shot history."""

    media_id: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    shot_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    media_kind: Literal["image", "video"]
    asset: AssetRef
    original_filename: str = Field(default="", max_length=255)
    created_by: str = Field(default="", max_length=160)
    created_at: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_media_kind(self):
        if not self.asset.media_type.startswith(f"{self.media_kind}/"):
            raise ValueError("上传素材类型与资产媒体类型不一致")
        return self


class PendingShotMediaEdit(ContractModel):
    """One prepared editor-media selection waiting for a batch render."""

    shot_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    # Empty means restoring the generated AI visual for this shot.
    media_id: str = Field(
        default="",
        max_length=96,
        pattern=r"^$|^[A-Za-z0-9_-]+$",
    )
    staged_by: str = Field(default="", max_length=160)
    staged_at: str = Field(default="", max_length=64)


class ProviderUsageRecord(ContractModel):
    """One provider attempt persisted before downstream validation can fail."""

    usage_id: str = Field(min_length=1, max_length=96)
    operation: str = Field(min_length=1, max_length=64)
    provider: str = Field(min_length=1, max_length=128)
    model_id: str = Field(default="", max_length=256)
    request_id: str = Field(default="", max_length=256)
    request_count: int = Field(default=1, ge=1, le=100)
    succeeded: bool = False
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    quantity: float = Field(default=1, ge=0)
    unit: str = Field(default="request", max_length=32)
    reported_cost: float | None = Field(default=None, ge=0)
    reported_currency: Literal["USD", "CNY"] | None = None
    estimated_cost: float | None = Field(default=None, ge=0)
    estimated_currency: Literal["USD", "CNY"] | None = None
    pricing_basis: str = Field(default="", max_length=500)
    note: str = Field(default="", max_length=500)
    occurred_at: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_money_currency(self):
        if self.reported_cost is not None and not self.reported_currency:
            raise ValueError("供应商回传金额必须标明币种")
        if self.estimated_cost is not None and not self.estimated_currency:
            raise ValueError("估算金额必须标明币种")
        if self.reported_cost is not None and self.estimated_cost is not None:
            raise ValueError("单次调用不能同时计入供应商回传金额和估算金额")
        return self


class DouyinPlaybackSnapshot(ContractModel):
    """One paid, point-in-time reading of a Douyin video's public metrics."""

    play_count: int = Field(ge=0)
    like_count: int | None = Field(default=None, ge=0)
    comment_count: int | None = Field(default=None, ge=0)
    share_count: int | None = Field(default=None, ge=0)
    collect_count: int | None = Field(default=None, ge=0)
    observed_at: str = Field(min_length=1, max_length=64)
    request_id: str = Field(default="", max_length=256)


class DouyinPerformance(ContractModel):
    """Douyin-only publishing feedback bound to one packaged video job."""

    platform: Literal["douyin"] = "douyin"
    video_id: str = Field(pattern=r"^\d{5,32}$")
    video_url: str = Field(max_length=2000)
    video_title: str = Field(default="", max_length=500)
    author_name: str = Field(default="", max_length=200)
    bound_at: str = Field(min_length=1, max_length=64)
    updated_at: str = Field(min_length=1, max_length=64)
    snapshots: list[DouyinPlaybackSnapshot] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_canonical_video_url(self):
        expected = f"https://www.douyin.com/video/{self.video_id}"
        if self.video_url != expected:
            raise ValueError("抖音作品链接必须是由作品 ID 构造的标准链接")
        return self


class FirstFrameCandidate(ContractModel):
    candidate_id: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    shot_id: str = Field(min_length=1, max_length=64)
    variant: int = Field(ge=1, le=4)
    prompt: str = Field(min_length=1, max_length=2000)
    seed: int = Field(ge=0, le=4294967295)
    model_id: str = Field(default="", max_length=256)
    source_url: str = Field(default="", max_length=4000)
    size: str = Field(default="", max_length=64)
    usage_total_tokens: int = Field(default=0, ge=0)
    estimated_cost_cny: float | None = Field(default=None, ge=0)
    pricing_basis: str = Field(default="", max_length=500)
    asset: AssetRef | None = None
    created_at: str


class StyleFrameCandidate(ContractModel):
    """One paid visual-development frame created before bulk generation."""

    candidate_id: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    variant: int = Field(ge=1, le=3)
    prompt: str = Field(min_length=1, max_length=6000)
    seed: int = Field(ge=0, le=(1 << 31) - 1)
    model_id: str = Field(default="", max_length=256)
    source_url: str = Field(default="", max_length=4000)
    size: str = Field(default="", max_length=64)
    usage_total_tokens: int = Field(default=0, ge=0)
    estimated_cost_cny: float | None = Field(default=None, ge=0)
    pricing_basis: str = Field(default="", max_length=500)
    asset: AssetRef | None = None
    created_at: str


# 仅用于读取 0.7.x 已持久化的双首帧任务；0.8+ 新任务不再生成评分。
class FrameCandidateEvaluation(ContractModel):
    candidate_id: str = Field(min_length=1, max_length=96)
    semantic_score: int = Field(ge=0, le=100)
    style_score: int = Field(ge=0, le=100)
    composition_score: int = Field(ge=0, le=100)
    technical_score: int = Field(ge=0, le=100)
    total_score: int = Field(ge=0, le=100)
    summary: str = Field(default="", max_length=500)


class FrameSelection(ContractModel):
    shot_id: str = Field(min_length=1, max_length=64)
    recommended_candidate_id: str = Field(min_length=1, max_length=96)
    evaluations: list[FrameCandidateEvaluation] = Field(min_length=1, max_length=4)
    model_id: str = Field(default="", max_length=256)
    evaluated_at: str

    @model_validator(mode="after")
    def validate_candidates(self):
        candidate_ids = [item.candidate_id for item in self.evaluations]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("首帧评分中的候选 ID 必须唯一")
        if self.recommended_candidate_id not in candidate_ids:
            raise ValueError("推荐首帧必须存在于评分结果中")
        return self


class NarrationAudioSegment(ContractModel):
    segment_id: str
    text: str
    asset_id: str
    start_seconds: float = Field(ge=0)
    duration_seconds: float = Field(gt=0)


class NarrationManifest(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    provider: str
    voice_id: str
    speed_ratio: TtsSpeedRatio = LEGACY_TTS_SPEED_RATIO
    sample_rate: int = Field(default=48000, ge=8000)
    total_duration_seconds: float = Field(gt=0)
    full_audio_asset_id: str
    segments: list[NarrationAudioSegment] = Field(min_length=1)


class VisualBlock(ContractModel):
    id: str
    type: Literal[
        "generated_video",
        "generated_image",
        "title_card",
        "quote_card",
        "source_card",
    ]
    shot_id: str = ""
    start_frame: int = Field(ge=0)
    duration_in_frames: int = Field(gt=0)
    asset_id: str | None = None
    # New assets are prepared to cover their chapter and play naturally.
    # ``None`` preserves the historical fit-to-chapter behavior for old jobs.
    playback_rate: float | None = Field(default=None, ge=0.5, le=2.0)
    headline: str = ""
    body: str = ""
    source_refs: list[str] = Field(default_factory=list)


class SubtitleCue(ContractModel):
    id: str
    start_frame: int = Field(ge=0)
    duration_in_frames: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=80)


class ScreenTextCue(ContractModel):
    """Sparse editorial text rendered by Remotion, never by image/video models."""

    id: str
    start_frame: int = Field(ge=0)
    duration_in_frames: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=80)
    kind: Literal["headline", "emphasis", "closing"] = "emphasis"


class RenderOutput(ContractModel):
    codec: Literal["h264"] = "h264"
    pixel_format: Literal["yuv420p"] = "yuv420p"
    audio_codec: Literal["aac"] = "aac"


class AiContentLabel(ContractModel):
    """Legacy manifest field retained so older jobs remain readable."""

    enabled: bool = False
    start_frame: int = Field(default=0, ge=0)
    duration_in_frames: int = Field(default=90, gt=0)


class VisualGenerationRequest(ContractModel):
    """供应商无关的单镜头生成请求。"""

    request_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=4000)
    # Human editors describe the desired change in semantic language. The
    # provider prompt remains a compiled, read-only artifact.
    revision_intent: str = Field(default="", max_length=600)
    model_id: SeedanceModelId | Literal[""] = ""
    resolution: Literal["480p", "720p", "1080p"] = "480p"
    ratio: Literal["9:16"] = "9:16"
    duration_seconds: int = Field(default=8, ge=4, le=15)
    generate_audio: Literal[False] = False
    seed: int | None = Field(default=None, ge=0, le=4294967295)
    # Stable asset identity is hashed into the paid request; expiring signed URLs are not.
    first_frame_asset_id: str = Field(default="", max_length=128)

    def fingerprint(self) -> str:
        payload = self.model_dump(mode="json")
        # Requests persisted before model selection was introduced must keep
        # their paid-request fingerprint, so polling can never become a submit.
        if not payload.get("model_id"):
            payload.pop("model_id", None)
        # Preserve fingerprints for requests persisted before semantic shot
        # revisions were introduced.
        if not payload.get("revision_intent"):
            payload.pop("revision_intent", None)
        return content_hash(payload)


class ProviderTask(ContractModel):
    provider: str
    provider_task_id: str = Field(min_length=1, max_length=256)
    request_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(default="", max_length=64)
    model_id: str = Field(default="", max_length=256)
    state: ProviderTaskState
    output_url: str = ""
    error_code: str = ""
    error_message: str = ""
    raw_status: str = ""
    usage_total_tokens: int = Field(default=0, ge=0)
    estimated_cost_cny: float | None = Field(default=None, ge=0)
    pricing_rate_cny_per_million: float | None = Field(default=None, ge=0)
    pricing_basis: str = Field(default="", max_length=500)
    created_at: str = Field(default="", max_length=64)


class VisualShotVersion(ContractModel):
    """A paid generation attempt retained for preview and later reuse."""

    version_id: str = Field(
        min_length=1,
        max_length=96,
        pattern=r"^[A-Za-z0-9_-]+$",
    )
    shot_id: str = Field(min_length=1, max_length=64)
    version: int = Field(ge=1)
    request: VisualGenerationRequest
    task: ProviderTask
    asset: AssetRef | None = None
    created_by: str = ""
    created_at: str = ""


class RenderManifest(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    job_id: str
    renderer: Literal["remotion"] = "remotion"
    composition_id: Literal["KnowledgeVideoV1"] = "KnowledgeVideoV1"
    template_version: str = "neutral_knowledge_v2"
    # 三档画质都以同一套 1080x1920 设计坐标渲染，保证排版一致。
    width: Literal[480, 720, 1080] = 480
    height: Literal[854, 1280, 1920] = 854
    fps: Literal[30] = 30
    duration_in_frames: int = Field(gt=0)
    video_title: str = Field(default="", max_length=200)
    cover_text: str = Field(default="", max_length=80)
    assets: list[AssetRef] = Field(default_factory=list)
    cover_asset_id: str = ""
    audio_asset_id: str
    visual_blocks: list[VisualBlock] = Field(min_length=1)
    subtitle_cues: list[SubtitleCue] = Field(default_factory=list)
    screen_text_cues: list[ScreenTextCue] = Field(default_factory=list)
    ai_content_label: AiContentLabel = Field(default_factory=AiContentLabel)
    brand_overlay: None = None
    output: RenderOutput = Field(default_factory=RenderOutput)

    @model_validator(mode="after")
    def validate_timeline(self):
        if (self.width, self.height) not in {
            (480, 854),
            (720, 1280),
            (1080, 1920),
        }:
            raise ValueError(
                "成片尺寸必须是 480x854、720x1280 或 1080x1920"
            )
        asset_ids = [item.asset_id for item in self.assets]
        if len(asset_ids) != len(set(asset_ids)):
            raise ValueError("渲染资产 ID 必须唯一")
        known_asset_ids = set(asset_ids)
        if self.cover_asset_id:
            if self.cover_asset_id not in known_asset_ids:
                raise ValueError("封面背景引用了不存在的资产")
            cover_asset = next(
                item for item in self.assets if item.asset_id == self.cover_asset_id
            )
            if not cover_asset.media_type.startswith("image/"):
                raise ValueError("封面背景资产类型不是图片")
        if self.audio_asset_id not in known_asset_ids:
            raise ValueError("主旁白资产不存在")
        audio_asset = next(
            item for item in self.assets if item.asset_id == self.audio_asset_id
        )
        if not audio_asset.media_type.startswith("audio/"):
            raise ValueError("主旁白资产类型不是音频")
        block_ids = [item.id for item in self.visual_blocks]
        cue_ids = [item.id for item in self.subtitle_cues]
        screen_text_ids = [item.id for item in self.screen_text_cues]
        if len(block_ids) != len(set(block_ids)):
            raise ValueError("画面块 ID 必须唯一")
        if len(cue_ids) != len(set(cue_ids)):
            raise ValueError("字幕 ID 必须唯一")
        if len(screen_text_ids) != len(set(screen_text_ids)):
            raise ValueError("屏幕文字 ID 必须唯一")
        for block in self.visual_blocks:
            if block.asset_id and block.asset_id not in known_asset_ids:
                raise ValueError(f"画面块 {block.id} 引用了不存在的资产")
            if block.start_frame + block.duration_in_frames > self.duration_in_frames:
                raise ValueError(f"画面块 {block.id} 超出成片时间轴")
        for cue in self.subtitle_cues:
            if cue.start_frame + cue.duration_in_frames > self.duration_in_frames:
                raise ValueError(f"字幕 {cue.id} 超出成片时间轴")
        for cue in self.screen_text_cues:
            if cue.start_frame + cue.duration_in_frames > self.duration_in_frames:
                raise ValueError(f"屏幕文字 {cue.id} 超出成片时间轴")
        return self


class QualityReport(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    automatic_status: Literal["failed", "manual_review_required", "review_ready"]
    manual_review_required: Literal[True] = True
    checks: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    generated_at: str


class Artifact(ContractModel):
    name: str
    asset: AssetRef


class VideoJob(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    pipeline_version: PipelineVersion = PipelineVersion.LEGACY
    id: str
    revision: int = Field(default=1, ge=1)
    state: JobState
    # v1/v2 jobs persist a SourceCard. New v3 jobs persist the creator's input
    # directly and synthesize a neutral compatibility card only inside domain
    # validation boundaries.
    source_card_id: str = ""
    source_card_revision: int = Field(default=1, ge=1)
    source_card_snapshot: dict[str, Any] = Field(default_factory=dict)
    input_snapshot: CreativeInputSnapshot | None = None
    # New jobs freeze the full content workflow. None means the persisted task
    # predates Skill routing and must continue under legacy semantics.
    skill_snapshot: ContentSkillSnapshot | None = None
    script_skill_snapshot: ScriptSkillSnapshot | None = None
    director_skill_snapshot: DirectorSkillSnapshot | None = None
    provider_adapter_snapshot: ProviderAdapterSnapshot | None = None
    # v3 freezes H3 as an internal, deterministic prompt compiler. It creates
    # no user-facing plan and never becomes a second content owner.
    prompt_adapter_snapshot: PromptAdapterSnapshot | None = None
    # New tasks also freeze visual treatment and the internal prompt-writing
    # method. They remain separate from Content Skill and provider selection.
    visual_style_snapshot: VisualStyleSnapshot | None = None
    prompt_writing_profile_snapshot: PromptWritingProfileSnapshot | None = None
    # None 仅用于兼容上线前已经持久化的任务；所有新任务都会冻结完整配置。
    generation_settings: GenerationSettings | None = None
    # Legacy and all-AI jobs stay automatic. Editors can explicitly request one
    # pause after narration/storyboard planning so uploaded media prevents the
    # corresponding Seedream/Seedance calls instead of replacing paid results.
    pre_generation_media_mode: PreGenerationMediaMode = (
        PreGenerationMediaMode.AUTOMATIC
    )
    # Research results and bounded failure diagnostics live in the aggregate so
    # retries never silently repeat a paid search.
    research_prompt_snapshot: ResearchPromptSnapshot | None = None
    research_brief: PersonResearchBrief | NewsResearchBrief | None = None
    # ScriptDraft v3 creates this once; the visual director reuses it instead
    # of reinterpreting a second per-beat visual instruction track.
    creative_brief: CreativeBrief | None = None
    editorial_plan: EditorialPlan | None = None
    research_warning: str = Field(default="", max_length=2000)
    research_diagnostics: ResearchDiagnostics | None = None
    # One initial research attempt is implicit. Every additional paid attempt
    # requires an editor-authorized increment through the dedicated API.
    research_retry_authorizations: int = Field(default=0, ge=0)
    script: ScriptDraft | None = None
    script_hash: str = ""
    script_review: ScriptReview | None = None
    # This is an append-only audit trail.  A hard item limit would eventually
    # make an otherwise valid paid workflow impossible to save after repeated
    # revisions, so retention is intentionally controlled by the aggregate
    # repository rather than by schema validation.
    usage_records: list[ProviderUsageRecord] = Field(default_factory=list)
    douyin_performance: DouyinPerformance | None = None
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    narration_manifest: NarrationManifest | None = None
    storyboard_plan: StoryboardPlan | None = None
    director_treatment: DirectorTreatment | None = None
    asset_bible: AssetBible | None = None
    visual_bible: VisualBible | None = None
    style_frame_candidates: list[StyleFrameCandidate] = Field(
        default_factory=list,
        max_length=3,
    )
    selected_style_frame_id: str = Field(default="", max_length=96)
    first_frame_candidates: list[FirstFrameCandidate] = Field(default_factory=list)
    # 旧任务读兼容字段。新任务每镜头只有一张首帧，不写入选择记录。
    frame_selections: list[FrameSelection] = Field(default_factory=list)
    frame_selection_warning: str = Field(default="", max_length=2000)
    visual_requests: list[VisualGenerationRequest] = Field(default_factory=list)
    video_tasks: list[ProviderTask] = Field(default_factory=list)
    visual_versions: list[VisualShotVersion] = Field(default_factory=list)
    shot_media_versions: list[ShotMediaVersion] = Field(default_factory=list)
    pending_shot_media_edits: list[PendingShotMediaEdit] = Field(
        default_factory=list
    )
    render_manifest: RenderManifest | None = None
    quality_report: QualityReport | None = None
    artifacts: list[Artifact] = Field(default_factory=list)
    review_bundle_hash: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    last_run_task_id: str = ""
    failed_stage: str = ""
    error: str = ""
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    deleted_at: str = ""
    deleted_by: str = ""

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_generation_settings(cls, value: Any) -> Any:
        """Preserve paid history while migrating requests never submitted."""
        if not isinstance(value, dict):
            return value
        raw_settings = value.get("generation_settings")
        if isinstance(raw_settings, BaseModel):
            settings = raw_settings.model_dump(mode="python")
        elif isinstance(raw_settings, dict):
            settings = raw_settings
        else:
            return value
        additions: dict[str, Any] = {}
        if "video_resolution" not in settings:
            additions["video_resolution"] = "480p"
        if "seedance_model" not in settings:
            additions["seedance_model"] = (
                SEEDANCE_FLAGSHIP_MODEL
                if value.get("pipeline_version") == PipelineVersion.QUALITY_FIRST.value
                else SEEDANCE_EFFICIENT_MODEL
            )
        if "tts_voice_id" not in settings:
            additions["tts_voice_id"] = DEFAULT_TTS_VOICE_ID
        if "tts_speed_ratio" not in settings:
            # Existing jobs were synthesized at the provider's normal speed.
            additions["tts_speed_ratio"] = LEGACY_TTS_SPEED_RATIO

        def provider_task_id(item: Any) -> str:
            if isinstance(item, dict):
                return str(item.get("provider_task_id") or "").strip()
            return str(getattr(item, "provider_task_id", "") or "").strip()

        submitted_tasks = list(value.get("video_tasks") or [])
        for version in value.get("visual_versions") or []:
            task = (
                version.get("task")
                if isinstance(version, dict)
                else getattr(version, "task", None)
            )
            if task is not None:
                submitted_tasks.append(task)
        migrate_unsubmitted_retired_model = (
            settings.get("seedance_model") == SEEDANCE_RETIRED_MODEL
            and not any(provider_task_id(item) for item in submitted_tasks)
        )
        if not additions and not migrate_unsubmitted_retired_model:
            return value
        normalized = dict(value)
        normalized_settings = dict(settings)
        normalized_settings.update(additions)
        if migrate_unsubmitted_retired_model:
            # 1.5 Pro remains valid so paid historical tasks stay pollable. Only
            # requests without a provider task ID are safe to move and resubmit.
            normalized_settings["seedance_model"] = SEEDANCE_EFFICIENT_MODEL
            normalized_requests: list[Any] = []
            for request in value.get("visual_requests") or []:
                request_data = (
                    request.model_dump(mode="python")
                    if isinstance(request, BaseModel)
                    else dict(request) if isinstance(request, dict) else request
                )
                if (
                    isinstance(request_data, dict)
                    and request_data.get("model_id") == SEEDANCE_RETIRED_MODEL
                ):
                    request_data["model_id"] = SEEDANCE_EFFICIENT_MODEL
                normalized_requests.append(request_data)
            normalized["visual_requests"] = normalized_requests
        normalized["generation_settings"] = normalized_settings
        return normalized

    def approval(self, kind: Literal["script", "final"]) -> ApprovalRecord | None:
        return next((item for item in reversed(self.approvals) if item.kind == kind), None)


def canonical_json(value: BaseModel | dict[str, Any]) -> str:
    data = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: BaseModel | dict[str, Any] | bytes) -> str:
    payload = value if isinstance(value, bytes) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def timestamp(value: datetime | None = None) -> str:
    return (value or datetime.now(BEIJING_TZ)).isoformat(timespec="seconds")
