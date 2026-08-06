"""家庭教育选题研究的独立领域契约。

抖音数据只能解释“为什么值得研究”，不能充当视频中的事实来源。
因此这里的候选选题不会继承 ``SourceCard``，也不能绕过来源核验门槛。
"""
from __future__ import annotations

import re
from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from qijia_video.contracts import ContractModel


TOPIC_SCHEMA_VERSION = "1.0"


class TopicResearchStatus(StrEnum):
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


class TopicEvidenceType(StrEnum):
    TREND_TERM = "trend_term"
    VIDEO = "video"


class TopicSignalType(StrEnum):
    CREATIVE_KEYWORD = "creative_keyword"
    RISING_TOPIC = "rising_topic"
    SEARCH_VIDEO = "search_video"
    RELATED_VIDEO = "related_video"
    HIGH_COMPLETION_VIDEO = "high_completion_video"
    LOW_FOLLOWER_VIDEO = "low_follower_video"
    HIGH_LIKE_VIDEO = "high_like_video"


class TopicEvidenceTier(StrEnum):
    """可解释的证据强度；旧记录默认未评估，不会被冒充为爆款。"""

    UNASSESSED = "unassessed"
    TREND_SIGNAL = "trend_signal"
    LOW_FOLLOWER_BREAKOUT = "low_follower_breakout"
    EMERGING_LOW_FOLLOWER_BREAKOUT = "emerging_low_follower_breakout"
    HIGH_HEAT_BREAKOUT = "high_heat_breakout"


class TopicContentPillar(StrEnum):
    COMMUNICATION = "亲子沟通"
    EMOTION = "情绪与行为"
    LEARNING = "学习习惯"
    BOUNDARIES = "规则与边界"
    DIGITAL = "数字生活"
    ADOLESCENCE = "青春期"
    PARENT_GROWTH = "父母成长"


class TopicMetrics(ContractModel):
    play_count: int = Field(default=0, ge=0)
    like_count: int = Field(default=0, ge=0)
    comment_count: int = Field(default=0, ge=0)
    share_count: int = Field(default=0, ge=0)
    collect_count: int = Field(default=0, ge=0)
    follower_count: int = Field(default=0, ge=0)
    like_rate: float | None = Field(default=None, ge=0)
    comment_rate: float | None = Field(default=None, ge=0)
    share_rate: float | None = Field(default=None, ge=0)
    collect_rate: float | None = Field(default=None, ge=0)
    deep_engagement_rate: float | None = Field(default=None, ge=0)
    play_follower_ratio: float | None = Field(default=None, ge=0)
    published_age_hours: float | None = Field(default=None, ge=0)
    average_daily_plays: int | None = Field(default=None, ge=0)


class TopicEvidence(ContractModel):
    id: str = Field(pattern=r"^ev_[a-f0-9]{12}$")
    evidence_type: TopicEvidenceType
    signal_types: list[TopicSignalType] = Field(min_length=1, max_length=6)
    queries: list[str] = Field(default_factory=list, max_length=8)
    title: str = Field(min_length=1, max_length=500)
    platform_labels: list[str] = Field(default_factory=list, max_length=8)
    quality_tier: TopicEvidenceTier = TopicEvidenceTier.UNASSESSED
    qualification_reasons: list[str] = Field(default_factory=list, max_length=8)
    source_rank: int = Field(default=0, ge=0)
    video_id: str = Field(default="", max_length=64)
    video_url: str = Field(default="", max_length=2000)
    author_name: str = Field(default="", max_length=200)
    published_at: str = Field(default="", max_length=64)
    duration_seconds: float | None = Field(default=None, ge=0)
    metrics: TopicMetrics | None = None

    @model_validator(mode="after")
    def validate_evidence_shape(self):
        if self.evidence_type == TopicEvidenceType.VIDEO:
            if not re.fullmatch(r"[A-Za-z0-9_-]{5,64}", self.video_id):
                raise ValueError("视频证据必须包含可复核的抖音作品 ID")
            canonical_prefix = "https://www.douyin.com/video/"
            if self.video_url != f"{canonical_prefix}{self.video_id}":
                raise ValueError("视频证据必须使用规范的抖音作品链接")
            if self.metrics is None:
                raise ValueError("视频证据必须包含平台返回的指标快照")
        elif self.video_id or self.video_url or self.metrics is not None:
            raise ValueError("趋势词证据不得伪装成视频证据")
        return self


class TopicCandidate(ContractModel):
    id: str = Field(pattern=r"^topic_[a-f0-9]{12}$")
    rank: int = Field(ge=1, le=5)
    content_pillar: TopicContentPillar
    title: str = Field(min_length=4, max_length=120)
    parent_question: str = Field(min_length=4, max_length=240)
    editorial_angle: str = Field(min_length=10, max_length=600)
    opening_hook: str = Field(min_length=6, max_length=240)
    why_now: str = Field(min_length=10, max_length=600)
    evidence_refs: list[str] = Field(min_length=2, max_length=8)
    risk_note: str = Field(min_length=4, max_length=400)

    @model_validator(mode="after")
    def unique_evidence_refs(self):
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("候选选题的证据引用不得重复")
        return self


class TopicCandidateProposal(ContractModel):
    """Structured editor output before stable IDs and ranks are assigned."""

    content_pillar: TopicContentPillar
    title: str = Field(min_length=4, max_length=120)
    parent_question: str = Field(min_length=4, max_length=240)
    editorial_angle: str = Field(min_length=10, max_length=600)
    opening_hook: str = Field(min_length=6, max_length=240)
    why_now: str = Field(min_length=10, max_length=600)
    evidence_refs: list[str] = Field(min_length=2, max_length=8)
    risk_note: str = Field(min_length=4, max_length=400)

    @model_validator(mode="after")
    def unique_evidence_refs(self):
        if len(self.evidence_refs) != len(set(self.evidence_refs)):
            raise ValueError("候选提案的证据引用不得重复")
        return self


class TikHubCallRecord(ContractModel):
    endpoint: str = Field(min_length=1, max_length=300)
    request_id: str = Field(default="", max_length=200)
    response_code: int | None = None
    elapsed_ms: int = Field(default=0, ge=0)
    cache_message: str = Field(default="", max_length=300)
    succeeded: bool = False


class TopicModelUsage(ContractModel):
    provider: str = "openrouter"
    model: str = Field(default="", max_length=200)
    request_id: str = Field(default="", max_length=200)
    request_count: int = Field(default=0, ge=0, le=1)
    succeeded: bool = False
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    reported_cost_usd: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_attempt(self):
        if self.succeeded and self.request_count != 1:
            raise ValueError("成功的编辑模型调用必须记录一次请求")
        return self


class TopicCostSummary(ContractModel):
    tikhub_request_budget: int = Field(default=0, ge=0)
    tikhub_request_count: int = Field(default=0, ge=0)
    tikhub_success_count: int = Field(default=0, ge=0)
    tikhub_calls: list[TikHubCallRecord] = Field(default_factory=list, max_length=40)
    estimated_tikhub_cost_usd: float | None = Field(default=None, ge=0)
    estimated_total_cost_usd: float | None = Field(default=None, ge=0)
    estimated_cost_per_candidate_usd: float | None = Field(default=None, ge=0)
    tikhub_cost_basis: str = Field(default="", max_length=400)
    model_usage: TopicModelUsage | None = None

    @model_validator(mode="after")
    def validate_call_totals(self):
        if self.tikhub_request_count != len(self.tikhub_calls):
            raise ValueError("TikHub 请求总数必须与调用记录一致")
        succeeded = sum(item.succeeded for item in self.tikhub_calls)
        if self.tikhub_success_count != succeeded:
            raise ValueError("TikHub 成功请求数必须与调用记录一致")
        if self.tikhub_request_count > self.tikhub_request_budget:
            raise ValueError("TikHub 请求总数不得超过本轮硬预算")
        return self


class TopicLowFollowerDiagnostics(ContractModel):
    """低粉样本的本地复核漏斗；未通过项允许同一视频重复计数。"""

    received_count: int = Field(default=0, ge=0)
    unique_qualified_count: int = Field(default=0, ge=0)
    strong_qualified_count: int = Field(default=0, ge=0)
    emerging_qualified_count: int = Field(default=0, ge=0)
    duplicate_qualified_count: int = Field(default=0, ge=0)
    empty_or_unrecognized_query_count: int = Field(default=0, ge=0)
    rejected_missing_identity_count: int = Field(default=0, ge=0)
    rejected_invalid_video_id_count: int = Field(default=0, ge=0)
    rejected_off_topic_count: int = Field(default=0, ge=0)
    rejected_invalid_publish_time_count: int = Field(default=0, ge=0)
    rejected_too_old_count: int = Field(default=0, ge=0)
    rejected_missing_followers_count: int = Field(default=0, ge=0)
    rejected_follower_ceiling_count: int = Field(default=0, ge=0)
    rejected_insufficient_plays_count: int = Field(default=0, ge=0)
    rejected_play_follower_ratio_count: int = Field(default=0, ge=0)
    rejected_like_rate_count: int = Field(default=0, ge=0)
    rejected_deep_engagement_rate_count: int = Field(default=0, ge=0)


class TopicResearchRun(ContractModel):
    schema_version: Literal["1.0"] = TOPIC_SCHEMA_VERSION
    id: str
    revision: int = Field(default=1, ge=1)
    theme: Literal["family_education"] = "family_education"
    platform: Literal["douyin"] = "douyin"
    data_provider: Literal["tikhub"] = "tikhub"
    status: TopicResearchStatus = TopicResearchStatus.RUNNING
    valid_through: str = Field(default="", max_length=32)
    data_window_note: str = Field(default="近 3 天趋势与低粉爆款、近 7 天高热补充", max_length=200)
    evidence: list[TopicEvidence] = Field(default_factory=list, max_length=80)
    candidates: list[TopicCandidate] = Field(default_factory=list, max_length=5)
    cost: TopicCostSummary = Field(default_factory=TopicCostSummary)
    low_follower_diagnostics: TopicLowFollowerDiagnostics = Field(
        default_factory=TopicLowFollowerDiagnostics
    )
    warnings: list[str] = Field(default_factory=list, max_length=20)
    selected_candidate_id: str = Field(default="", max_length=64)
    selected_by: str = Field(default="", max_length=128)
    selected_at: str = Field(default="", max_length=64)
    last_run_task_id: str = Field(default="", max_length=64)
    error: str = Field(default="", max_length=2000)
    created_by: str = Field(default="", max_length=128)
    created_at: str = Field(default="", max_length=64)
    updated_at: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def validate_candidate_references(self):
        evidence_ids = {item.id for item in self.evidence}
        if len(evidence_ids) != len(self.evidence):
            raise ValueError("研究证据 ID 必须唯一")
        candidate_ids = [item.id for item in self.candidates]
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("候选选题 ID 必须唯一")
        ranks = [item.rank for item in self.candidates]
        if ranks and ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("候选选题排名必须从 1 开始且连续")
        for candidate in self.candidates:
            unknown = set(candidate.evidence_refs) - evidence_ids
            if unknown:
                raise ValueError(
                    f"候选选题 {candidate.id} 引用了不存在的研究证据：{sorted(unknown)}"
                )
        if self.status == TopicResearchStatus.READY and len(self.candidates) != 5:
            raise ValueError("可供选择的研究必须包含完整的 5 个候选")
        if self.selected_candidate_id and self.selected_candidate_id not in set(candidate_ids):
            raise ValueError("已采用的候选选题不存在")
        return self
