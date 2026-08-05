"""应用层端口；迁移时只需替换这些端口的实现。"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from qijia_video.contracts import (
    Actor,
    AssetRef,
    NarrationManifest,
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

    async def list(
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
        self, script: ScriptDraft, workspace: Path
    ) -> tuple[NarrationManifest, list[GeneratedFile]]: ...


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
