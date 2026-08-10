"""应用层端口；迁移时只需替换这些端口的实现。"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from qijia_video.contracts import (
    Actor,
    AssetRef,
    NarrationManifest,
    ProviderUsageRecord,
    QualityReport,
    RenderManifest,
    ScriptDraft,
    ScriptReview,
    SourceCard,
    StoryboardPlan,
    ProviderTask,
    VisualGenerationRequest,
)


class AggregateRepository(Protocol):
    async def create(
        self, kind: str, name: str, actor: Actor, document: dict
    ) -> dict: ...

    async def get(self, kind: str, resource_id: str, actor: Actor) -> dict: ...

    async def get_visible(
        self, kind: str, resource_id: str, actor: Actor
    ) -> dict: ...

    async def list(
        self, kind: str, actor: Actor, *, limit: int = 100
    ) -> list[dict]: ...

    async def list_visible(
        self, kind: str, actor: Actor, *, limit: int = 100
    ) -> list[dict]: ...

    async def replace(
        self,
        kind: str,
        resource_id: str,
        actor: Actor,
        document: dict,
        *,
        expected_revision: int,
    ) -> dict: ...


class ScriptProvider(Protocol):
    name: str

    async def generate(
        self, card: SourceCard, prompt: str | None = None
    ) -> ScriptDraft: ...

    async def review(self, card: SourceCard, script: ScriptDraft) -> ScriptReview: ...


class StoryboardProvider(Protocol):
    name: str

    async def generate(
        self,
        script: ScriptDraft,
        base_style: str,
        beat_groups: list[list[str]],
        visual_types: list[str],
    ) -> StoryboardPlan: ...


@dataclass(frozen=True)
class GeneratedImage:
    url: str
    model_id: str
    size: str = ""
    usage_total_tokens: int = 0


class ImageProvider(Protocol):
    name: str

    async def generate(
        self,
        prompt: str,
        *,
        seed: int,
        reference_image_url: str = "",
    ) -> GeneratedImage: ...

    async def download(self, source_url: str, destination: Path) -> Path: ...


@dataclass(frozen=True)
class GeneratedFile:
    asset_id: str
    path: Path
    media_type: str
    duration_seconds: float | None = None


class TtsProvider(Protocol):
    name: str

    async def synthesize(
        self,
        script: ScriptDraft,
        workspace: Path,
        *,
        voice_id: str | None = None,
        speed_ratio: float = 1.0,
    ) -> tuple[NarrationManifest, list[GeneratedFile]]: ...

    async def synthesize_preview(
        self,
        text: str,
        workspace: Path,
        *,
        voice_id: str,
        speed_ratio: float,
        on_usage=None,
    ) -> GeneratedFile: ...


class VideoProvider(Protocol):
    name: str

    async def submit(
        self,
        request: VisualGenerationRequest,
        *,
        first_frame_url: str = "",
    ) -> ProviderTask: ...

    async def get_status(
        self, provider_task_id: str, request_fingerprint: str
    ) -> ProviderTask: ...

    async def download(self, provider_task_id: str, destination: Path) -> Path: ...

    async def cancel(
        self, provider_task_id: str, request_fingerprint: str
    ) -> ProviderTask: ...


@dataclass(frozen=True)
class DouyinVideoPerformance:
    video_id: str
    video_url: str
    play_count: int
    like_count: int | None = None
    comment_count: int | None = None
    share_count: int | None = None
    collect_count: int | None = None
    video_title: str = ""
    author_name: str = ""
    request_id: str = ""


UsageRecordRecorder = Callable[[ProviderUsageRecord], Awaitable[None]]


class DouyinPerformanceProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    @property
    def configuration_errors(self) -> list[str]: ...

    async def fetch_by_share_url(
        self,
        share_text: str,
        *,
        on_usage: UsageRecordRecorder | None = None,
    ) -> DouyinVideoPerformance: ...

    async def fetch_by_video_id(
        self,
        video_id: str,
        *,
        on_usage: UsageRecordRecorder | None = None,
    ) -> DouyinVideoPerformance: ...


class ArtifactStorage(Protocol):
    name: str

    async def put_file(
        self,
        *,
        object_key: str,
        path: Path,
        asset_id: str,
        media_type: str,
        duration_seconds: float | None = None,
    ) -> AssetRef: ...

    async def materialize(self, asset: AssetRef, destination: Path) -> Path: ...

    async def signed_get_url(self, asset: AssetRef, *, expires: int = 3600) -> str: ...

    async def create_direct_upload(
        self,
        *,
        object_key: str,
        asset_id: str,
        media_type: str,
        sha256: str,
        size_bytes: int,
        expires: int = 900,
    ) -> dict | None: ...

    async def complete_direct_upload(
        self,
        *,
        object_key: str,
        asset_id: str,
        media_type: str,
        sha256: str,
        size_bytes: int,
    ) -> AssetRef: ...

    async def delete_object(self, object_key: str) -> None: ...


class Renderer(Protocol):
    name: str

    async def render(
        self,
        manifest: RenderManifest,
        storage: ArtifactStorage,
        workspace: Path,
    ) -> Path: ...

    async def render_cover(
        self,
        manifest: RenderManifest,
        storage: ArtifactStorage,
        workspace: Path,
    ) -> Path: ...


class QualityChecker(Protocol):
    name: str

    async def inspect(
        self, path: Path, manifest: RenderManifest
    ) -> QualityReport: ...


class MediaPackager(Protocol):
    name: str

    async def normalize(self, source: Path, destination: Path) -> Path: ...

    async def prepare_video_for_timeline(
        self,
        source: Path,
        destination: Path,
        *,
        minimum_duration_seconds: float,
    ) -> tuple[Path, float]: ...

    async def prepare_uploaded_video_for_timeline(
        self,
        source: Path,
        destination: Path,
        *,
        chapter_duration_seconds: float,
    ) -> tuple[Path, float]: ...
