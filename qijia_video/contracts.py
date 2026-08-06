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

from qijia_video.prompts import DEFAULT_SCRIPT_PROMPT, DEFAULT_SEEDANCE_PROMPT

SCHEMA_VERSION = "1.0"
BEIJING_TZ = ZoneInfo("Asia/Shanghai")


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


class ContentFormat(StrEnum):
    PERSON_IDEA = "person_idea_explainer"
    RESEARCH = "research_explainer"
    CONCEPT = "concept_explainer"
    BOOK = "book_explainer"


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
    QUALITY_CHECKING = "quality_checking"
    FINAL_REVIEW_REQUIRED = "final_review_required"
    FINAL_APPROVED = "final_approved"
    PACKAGED = "packaged"
    FAILED = "failed"


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
    type: Literal["person", "research", "concept", "book"]
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
    target_audience: Literal["parents"] = "parents"
    risk_level: RiskLevel = RiskLevel.LOW
    subject: Subject
    title: str = Field(min_length=1, max_length=300)
    core_idea: str = Field(min_length=1, max_length=2000)
    parent_question: str = Field(min_length=1, max_length=500)
    sources: list[SourceEntry] = Field(min_length=1, max_length=20)
    verified_facts: list[VerifiedFact] = Field(min_length=1, max_length=50)
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


class PersonViewpointInput(ContractModel):
    """面向创作者的最小输入：一个人物和一个值得展开的观点。"""

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    person_name: str = Field(min_length=1, max_length=120)
    viewpoint: str = Field(min_length=10, max_length=1800)

    def to_source_card_input(self) -> SourceCardInput:
        person_name = re.sub(r"\s+", " ", self.person_name).strip()
        viewpoint = re.sub(r"\s+", " ", self.viewpoint).strip()
        return SourceCardInput(
            content_domain=ContentDomain.PARENT_EDUCATION,
            content_format=ContentFormat.PERSON_IDEA,
            subject={"type": "person", "name": person_name},
            title=f"{person_name}：{viewpoint}"[:300],
            core_idea=viewpoint,
            parent_question=(
                f"{person_name}的这个观点为什么值得家长重新思考？"
            ),
            sources=[{
                "id": "source_01",
                "type": "other",
                "title": f"用户输入的创作命题：{person_name}"[:500],
                "locator": "用户输入的人物与观点",
                "rights_status": "verified_for_citation",
            }],
            verified_facts=[{
                "id": "fact_01",
                "text": viewpoint,
                "source_refs": ["source_01"],
            }],
            interpretation_boundary=[{
                "id": "boundary_01",
                "text": (
                    "只围绕用户输入的观点展开，不补造人物经历、逐字引语、"
                    "研究数据或来源出处。"
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


class ScriptBeat(ContractModel):
    """One semantic beat with independent spoken, visual and editorial tracks."""

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
    schema_version: Literal["1.0", "2.0"] = "2.0"
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
            if self.beats[0].role != "hook" or self.beats[-1].role != "closing":
                raise ValueError("ScriptDraft v2 必须以 hook 开始并以 closing 收束")
            # Compatibility fields are derived mirrors, never a second source
            # of truth in v2.
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
    model_id: str = ""
    prompt_version: str = ""
    input_hash: str = Field(default="", pattern=r"^$|^[a-f0-9]{64}$")
    reviewed_at: str = ""


class GenerationSettings(ContractModel):
    """创建任务时冻结的可实验生成参数。"""

    script_prompt: str = Field(
        default=DEFAULT_SCRIPT_PROMPT,
        min_length=1,
        max_length=8000,
    )
    seedance_prompt: str = Field(
        default=DEFAULT_SEEDANCE_PROMPT,
        min_length=1,
        max_length=3200,
    )
    shot_count: Literal[5] = 5


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
    visual_intent: str = Field(min_length=1, max_length=600)
    first_frame_prompt: str = Field(min_length=1, max_length=1800)
    motion_prompt: str = Field(min_length=1, max_length=1800)
    selected_candidate_id: str = Field(default="", max_length=96)

    @model_validator(mode="after")
    def normalize_beat_ids(self):
        if not self.beat_ids:
            self.beat_ids = [self.segment_id]
        if self.beat_ids[0] != self.segment_id:
            raise ValueError("segment_id 必须等于 beat_ids 的第一个叙事段")
        if len(self.beat_ids) != len(set(self.beat_ids)):
            raise ValueError("同一镜头不能重复引用叙事段")
        return self


class StoryboardPlan(ContractModel):
    schema_version: Literal["1.0"] = SCHEMA_VERSION
    shots: list[StoryboardShot] = Field(min_length=5, max_length=5)
    model_id: str = Field(default="", max_length=256)
    prompt_version: str = Field(default="", max_length=128)
    input_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    created_at: str

    @model_validator(mode="after")
    def validate_shots(self):
        shot_ids = [item.shot_id for item in self.shots]
        beat_ids = [beat_id for item in self.shots for beat_id in item.beat_ids]
        if len(shot_ids) != len(set(shot_ids)):
            raise ValueError("分镜 ID 必须唯一")
        if len(beat_ids) != len(set(beat_ids)):
            raise ValueError("五个分镜不能重复消费同一个叙事段")
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
    enabled: Literal[True] = True
    start_frame: int = Field(default=0, ge=0)
    duration_in_frames: int = Field(default=90, gt=0)


class VisualGenerationRequest(ContractModel):
    """供应商无关的单镜头生成请求。"""

    request_id: str = Field(min_length=1, max_length=64)
    prompt: str = Field(min_length=1, max_length=4000)
    # 保留 720p 以反序列化已经付费生成的旧任务；新任务默认使用 480p。
    resolution: Literal["480p", "720p"] = "480p"
    ratio: Literal["9:16"] = "9:16"
    duration_seconds: int = Field(default=8, ge=4, le=15)
    generate_audio: Literal[False] = False
    seed: int | None = Field(default=None, ge=0, le=4294967295)
    # Stable asset identity is hashed into the paid request; expiring signed URLs are not.
    first_frame_asset_id: str = Field(default="", max_length=128)

    def fingerprint(self) -> str:
        return content_hash(self)


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
    template_version: str = "neutral_knowledge_v1"
    # 新任务直接输出竖屏 480p；保留 1080p 仅用于读取和恢复旧任务。
    width: Literal[480, 1080] = 480
    height: Literal[854, 1920] = 854
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
        if (self.width, self.height) not in {(480, 854), (1080, 1920)}:
            raise ValueError("成片尺寸必须是 480x854 或兼容旧任务的 1080x1920")
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
        if (
            self.ai_content_label.start_frame
            + self.ai_content_label.duration_in_frames
            > self.duration_in_frames
        ):
            raise ValueError("AI 生成内容标识超出成片时间轴")
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
    id: str
    revision: int = Field(default=1, ge=1)
    state: JobState
    source_card_id: str
    source_card_revision: int = Field(ge=1)
    source_card_snapshot: dict[str, Any]
    # None 仅用于兼容上线前已经持久化的任务；所有新任务都会冻结完整配置。
    generation_settings: GenerationSettings | None = None
    script: ScriptDraft | None = None
    script_hash: str = ""
    script_review: ScriptReview | None = None
    approvals: list[ApprovalRecord] = Field(default_factory=list)
    narration_manifest: NarrationManifest | None = None
    storyboard_plan: StoryboardPlan | None = None
    first_frame_candidates: list[FirstFrameCandidate] = Field(default_factory=list)
    # 旧任务读兼容字段。新任务每镜头只有一张首帧，不写入选择记录。
    frame_selections: list[FrameSelection] = Field(default_factory=list)
    frame_selection_warning: str = Field(default="", max_length=2000)
    visual_requests: list[VisualGenerationRequest] = Field(default_factory=list)
    video_tasks: list[ProviderTask] = Field(default_factory=list)
    visual_versions: list[VisualShotVersion] = Field(default_factory=list)
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
