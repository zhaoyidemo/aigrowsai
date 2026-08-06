"""家庭教育选题研究的应用层端口。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from qijia_video.errors import ProviderUnavailable
from qijia_video.topic_contracts import (
    TikHubCallRecord,
    TopicCandidateProposal,
    TopicEvidence,
    TopicLowFollowerDiagnostics,
    TopicModelUsage,
)


ProgressReporter = Callable[[dict], None]
TikHubCallRecorder = Callable[[list[TikHubCallRecord]], Awaitable[None]]
TopicModelUsageRecorder = Callable[[TopicModelUsage], Awaitable[None]]


@dataclass(frozen=True)
class TopicResearchCollection:
    valid_through: str
    evidence: list[TopicEvidence]
    calls: list[TikHubCallRecord]
    warnings: list[str]
    low_follower_diagnostics: TopicLowFollowerDiagnostics = field(
        default_factory=TopicLowFollowerDiagnostics
    )


@dataclass(frozen=True)
class TopicEditorialResult:
    proposals: list[TopicCandidateProposal]
    usage: TopicModelUsage


class TopicCollectionFailed(ProviderUnavailable):
    """A failed collection that still exposes already-spent API calls."""

    def __init__(
        self,
        message: str,
        calls: list[TikHubCallRecord],
        low_follower_diagnostics: TopicLowFollowerDiagnostics | None = None,
        warnings: list[str] | None = None,
    ):
        super().__init__(message)
        self.calls = list(calls)
        self.low_follower_diagnostics = (
            low_follower_diagnostics.model_copy(deep=True)
            if low_follower_diagnostics is not None
            else TopicLowFollowerDiagnostics()
        )
        self.warnings = list(warnings or [])


class TopicEditorialFailed(ProviderUnavailable):
    """A failed editor call that still exposes its billing metadata."""

    def __init__(self, message: str, usage: TopicModelUsage):
        super().__init__(message)
        self.usage = usage.model_copy(deep=True)


class TopicDataProvider(Protocol):
    name: str
    request_budget: int

    @property
    def configured(self) -> bool: ...

    async def collect_family_education(
        self,
        progress: ProgressReporter | None = None,
        on_calls: TikHubCallRecorder | None = None,
    ) -> TopicResearchCollection: ...


class TopicEditor(Protocol):
    name: str
    model: str

    @property
    def configured(self) -> bool: ...

    async def propose(
        self,
        evidence: list[TopicEvidence],
        *,
        valid_through: str,
        on_usage: TopicModelUsageRecorder | None = None,
    ) -> TopicEditorialResult: ...
