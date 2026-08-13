"""Director Skills plus Pipeline v1 visual/profile compatibility registries."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qijia_video.lazy_registry import LazyRegistryProxy

from qijia_video.contracts import (
    DEFAULT_PROMPT_WRITING_PROFILE_ID,
    DEFAULT_VISUAL_STYLE_ID,
    GenerationSettings,
    PromptWritingProfileSnapshot,
    VisualStyleSnapshot,
    content_hash,
    timestamp,
)


VISUAL_STYLE_ROOT = Path(__file__).resolve().parent / "visual_styles"
PROMPT_WRITING_PROFILE_ROOT = (
    Path(__file__).resolve().parent / "prompt_writing_profiles"
)
_STYLE_KEYS = {
    "schema_version",
    "version",
    "display_name",
    "description",
    "default",
    "director_prompt_file",
    "storyboard_rules_file",
    "image_rules_file",
    "motion_rules_file",
    "negative_rules",
    "tags",
}
_PROFILE_KEYS = {
    "schema_version",
    "version",
    "display_name",
    "description",
    "default",
    "planning_framework_file",
    "image_framework_file",
    "video_framework_file",
    "reference_policy_file",
    "audio_policy",
    "negative_rules",
}
_PROFILE_OPTIONAL_KEYS = {
    "creative_brief_framework_file",
    "research_framework_file",
    "script_framework_file",
}


class VisualStyleRegistryError(ValueError):
    """Raised before task creation when style/profile selection is invalid."""


def _version_key(version: str) -> tuple[int, int, int, bool, str]:
    try:
        stable, _, suffix = version.partition("-")
        major, minor, patch = (int(item) for item in stable.split("."))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"版本不是有效 SemVer：{version}") from exc
    return major, minor, patch, not bool(suffix), suffix


def _manifest(
    path: Path,
    expected: set[str],
    label: str,
    *,
    optional: set[str] | None = None,
) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} manifest 无法读取：{path}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} manifest 必须是 JSON 对象：{path}")
    missing = expected - set(value)
    unknown = set(value) - expected - (optional or set())
    if missing or unknown:
        raise RuntimeError(
            f"{label} manifest 字段不完整：missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    if value["schema_version"] != "1.0":
        raise RuntimeError(f"不支持的 {label} manifest 版本：{path}")
    _version_key(str(value["version"]))
    return value


def _read_resource(root: Path, raw_path: Any, *, required: bool = True) -> str:
    if raw_path in (None, ""):
        if required:
            raise RuntimeError(f"提示词资源路径不能为空：{root}")
        return ""
    target = (root / Path(str(raw_path))).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"提示词资源不能位于目录外：{raw_path}") from exc
    try:
        value = target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"提示词资源无法读取：{target}") from exc
    if required and not value:
        raise RuntimeError(f"提示词资源不能为空：{target}")
    return value


@dataclass(frozen=True)
class VisualStyleDefinition:
    style_id: str
    version: str
    display_name: str
    description: str
    default: bool
    director_prompt: str
    storyboard_rules: str
    image_rules: str
    motion_rules: str
    negative_rules: tuple[str, ...]
    tags: tuple[str, ...]
    manifest_hash: str

    def snapshot(self) -> VisualStyleSnapshot:
        return VisualStyleSnapshot(
            style_id=self.style_id,
            version=self.version,
            display_name=self.display_name,
            description=self.description,
            director_prompt=self.director_prompt,
            storyboard_rules=self.storyboard_rules,
            image_rules=self.image_rules,
            motion_rules=self.motion_rules,
            negative_rules=list(self.negative_rules),
            manifest_hash=self.manifest_hash,
            frozen_at=timestamp(),
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "style_id": self.style_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "default": self.default,
            "tags": list(self.tags),
            "role": "visual_language",
        }


@dataclass(frozen=True)
class PromptWritingProfileDefinition:
    profile_id: str
    version: str
    display_name: str
    description: str
    default: bool
    research_framework: str
    script_framework: str
    creative_brief_framework: str
    planning_framework: str
    image_framework: str
    video_framework: str
    reference_policy: str
    audio_policy: str
    negative_rules: tuple[str, ...]
    manifest_hash: str

    def snapshot(self) -> PromptWritingProfileSnapshot:
        return PromptWritingProfileSnapshot(
            profile_id=self.profile_id,
            version=self.version,
            display_name=self.display_name,
            description=self.description,
            research_framework=self.research_framework,
            script_framework=self.script_framework,
            creative_brief_framework=self.creative_brief_framework,
            planning_framework=self.planning_framework,
            image_framework=self.image_framework,
            video_framework=self.video_framework,
            reference_policy=self.reference_policy,
            audio_policy=self.audio_policy,
            negative_rules=list(self.negative_rules),
            manifest_hash=self.manifest_hash,
            frozen_at=timestamp(),
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "stages": [
                stage
                for stage, enabled in (
                    ("creative_brief", bool(self.creative_brief_framework)),
                    ("storyboard", True),
                    ("image", True),
                    ("video", True),
                )
                if enabled
            ],
        }


def _load_style(root: Path) -> VisualStyleDefinition:
    manifest = _manifest(root / "manifest.json", _STYLE_KEYS, "视觉风格")
    resources = {
        "director_prompt": _read_resource(
            root, manifest["director_prompt_file"], required=False
        ),
        "storyboard_rules": _read_resource(
            root, manifest["storyboard_rules_file"], required=False
        ),
        "image_rules": _read_resource(
            root, manifest["image_rules_file"], required=False
        ),
        "motion_rules": _read_resource(
            root, manifest["motion_rules_file"], required=False
        ),
    }
    hash_payload = {"style_id": root.name, "manifest": manifest, **resources}
    try:
        definition = VisualStyleDefinition(
            style_id=root.name,
            version=str(manifest["version"]),
            display_name=str(manifest["display_name"]),
            description=str(manifest["description"]),
            default=bool(manifest["default"]),
            director_prompt=resources["director_prompt"],
            storyboard_rules=resources["storyboard_rules"],
            image_rules=resources["image_rules"],
            motion_rules=resources["motion_rules"],
            negative_rules=tuple(str(item) for item in manifest["negative_rules"]),
            tags=tuple(str(item) for item in manifest["tags"]),
            manifest_hash=content_hash(hash_payload),
        )
        definition.snapshot()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"视觉风格内容无效：{root}") from exc
    return definition


def _load_profile(root: Path) -> PromptWritingProfileDefinition:
    manifest = _manifest(
        root / "manifest.json",
        _PROFILE_KEYS,
        "提示词方法",
        optional=_PROFILE_OPTIONAL_KEYS,
    )
    resources = {
        "research_framework": _read_resource(
            root,
            manifest.get("research_framework_file"),
            required=False,
        ),
        "script_framework": _read_resource(
            root,
            manifest.get("script_framework_file"),
            required=False,
        ),
        "creative_brief_framework": _read_resource(
            root,
            manifest.get("creative_brief_framework_file"),
            required=False,
        ),
        "planning_framework": _read_resource(
            root, manifest["planning_framework_file"]
        ),
        "image_framework": _read_resource(root, manifest["image_framework_file"]),
        "video_framework": _read_resource(root, manifest["video_framework_file"]),
        "reference_policy": _read_resource(
            root, manifest["reference_policy_file"]
        ),
    }
    hash_payload = {"profile_id": root.name, "manifest": manifest, **resources}
    try:
        definition = PromptWritingProfileDefinition(
            profile_id=root.name,
            version=str(manifest["version"]),
            display_name=str(manifest["display_name"]),
            description=str(manifest["description"]),
            default=bool(manifest["default"]),
            research_framework=resources["research_framework"],
            script_framework=resources["script_framework"],
            creative_brief_framework=resources["creative_brief_framework"],
            planning_framework=resources["planning_framework"],
            image_framework=resources["image_framework"],
            video_framework=resources["video_framework"],
            reference_policy=resources["reference_policy"],
            audio_policy=str(manifest["audio_policy"]),
            negative_rules=tuple(str(item) for item in manifest["negative_rules"]),
            manifest_hash=content_hash(hash_payload),
        )
        definition.snapshot()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"提示词方法内容无效：{root}") from exc
    return definition


class VisualStyleRegistry:
    def __init__(self, definitions: list[VisualStyleDefinition]):
        if not definitions:
            raise RuntimeError("至少需要注册一个视觉风格")
        self._definitions: dict[tuple[str, str], VisualStyleDefinition] = {}
        defaults: list[VisualStyleDefinition] = []
        for definition in definitions:
            key = (definition.style_id, definition.version)
            if key in self._definitions:
                raise RuntimeError(f"重复的视觉风格版本：{key}")
            self._definitions[key] = definition
            if definition.default:
                defaults.append(definition)
        if len(defaults) != 1:
            raise RuntimeError("视觉风格必须且只能声明一个默认版本")
        self._default = defaults[0]
        if self._default.style_id != DEFAULT_VISUAL_STYLE_ID:
            raise RuntimeError(
                f"默认视觉风格必须是 {DEFAULT_VISUAL_STYLE_ID}"
            )

    @classmethod
    def load(cls, root: Path = VISUAL_STYLE_ROOT) -> "VisualStyleRegistry":
        return cls([
            _load_style(path)
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and (path / "manifest.json").is_file()
        ])

    def resolve(self, style_id: str, version: str = "") -> VisualStyleDefinition:
        normalized_id = str(style_id or self._default.style_id).strip()
        normalized_version = str(version or "").strip()
        matches = [
            definition
            for (candidate_id, _), definition in self._definitions.items()
            if candidate_id == normalized_id
        ]
        if not matches:
            raise VisualStyleRegistryError(f"未知视觉风格：{normalized_id}")
        if normalized_version:
            exact = self._definitions.get((normalized_id, normalized_version))
            if not exact:
                raise VisualStyleRegistryError(
                    f"视觉风格版本不存在：{normalized_id}@{normalized_version}"
                )
            return exact
        return max(matches, key=lambda item: _version_key(item.version))

    def freeze(
        self,
        settings: GenerationSettings,
    ) -> tuple[GenerationSettings, VisualStyleSnapshot]:
        definition = self.resolve(
            settings.visual_style_id, settings.visual_style_version
        )
        effective = settings.model_copy(deep=True)
        if definition.director_prompt:
            effective.seedance_prompt = definition.director_prompt
        effective.visual_style_id = definition.style_id
        effective.visual_style_version = definition.version
        return effective, definition.snapshot()

    def public_catalog(self) -> list[dict[str, Any]]:
        latest: dict[str, VisualStyleDefinition] = {}
        for definition in self._definitions.values():
            current = latest.get(definition.style_id)
            if not current or _version_key(definition.version) > _version_key(
                current.version
            ):
                latest[definition.style_id] = definition
        return [latest[style_id].public_payload() for style_id in sorted(latest)]


class PromptWritingProfileRegistry:
    def __init__(self, definitions: list[PromptWritingProfileDefinition]):
        if not definitions:
            raise RuntimeError("至少需要注册一个提示词方法")
        self._definitions: dict[
            tuple[str, str], PromptWritingProfileDefinition
        ] = {}
        defaults: list[PromptWritingProfileDefinition] = []
        for definition in definitions:
            key = (definition.profile_id, definition.version)
            if key in self._definitions:
                raise RuntimeError(f"重复的提示词方法版本：{key}")
            self._definitions[key] = definition
            if definition.default:
                defaults.append(definition)
        if len(defaults) != 1:
            raise RuntimeError("提示词方法必须且只能声明一个默认版本")
        self._default = defaults[0]
        if self._default.profile_id != DEFAULT_PROMPT_WRITING_PROFILE_ID:
            raise RuntimeError(
                f"默认提示词方法必须是 {DEFAULT_PROMPT_WRITING_PROFILE_ID}"
            )

    @classmethod
    def load(
        cls, root: Path = PROMPT_WRITING_PROFILE_ROOT
    ) -> "PromptWritingProfileRegistry":
        return cls([
            _load_profile(path)
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and (path / "manifest.json").is_file()
        ])

    def resolve(
        self, profile_id: str, version: str = ""
    ) -> PromptWritingProfileDefinition:
        normalized_id = str(profile_id or self._default.profile_id).strip()
        normalized_version = str(version or "").strip()
        matches = [
            definition
            for (candidate_id, _), definition in self._definitions.items()
            if candidate_id == normalized_id
        ]
        if not matches:
            raise VisualStyleRegistryError(f"未知提示词方法：{normalized_id}")
        if normalized_version:
            exact = self._definitions.get((normalized_id, normalized_version))
            if not exact:
                raise VisualStyleRegistryError(
                    f"提示词方法版本不存在：{normalized_id}@{normalized_version}"
                )
            return exact
        return max(matches, key=lambda item: _version_key(item.version))

    def freeze(
        self, settings: GenerationSettings
    ) -> tuple[GenerationSettings, PromptWritingProfileSnapshot]:
        if (
            settings.prompt_writing_profile_id
            != DEFAULT_PROMPT_WRITING_PROFILE_ID
        ):
            raise VisualStyleRegistryError(
                "Prompt Writing Profile 仅用于读取 Pipeline v1 历史任务"
            )
        definition = self.resolve(
            DEFAULT_PROMPT_WRITING_PROFILE_ID,
            settings.prompt_writing_profile_version,
        )
        effective = settings.model_copy(deep=True)
        effective.prompt_writing_profile_id = definition.profile_id
        effective.prompt_writing_profile_version = definition.version
        return effective, definition.snapshot()

    def public_default(self) -> dict[str, Any]:
        return self._default.public_payload()


default_visual_style_registry = VisualStyleRegistry.load()
default_prompt_writing_profile_registry = LazyRegistryProxy(
    PromptWritingProfileRegistry.load
)
