"""Versioned, data-backed content Skill registry.

Skills own research, writing, domain visual policy and quality rules. Visual
style and prompt composition are deliberately handled by separate registries.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qijia_video.contracts import (
    ContentFormat,
    ContentSkillSnapshot,
    GenerationSettings,
    SkillInputMode,
    SkillResearchMode,
    SourceCard,
    content_hash,
    timestamp,
)


CONTENT_SKILL_ROOT = Path(__file__).resolve().parent / "content_skills"
_MANIFEST_KEYS = {
    "schema_version",
    "version",
    "display_name",
    "input_mode",
    "compatible_formats",
    "default_for_formats",
    "research_mode",
    "script_system_prompt",
    "script_prompt_file",
    "visual_policy_file",
    "research_prompt_file",
    "policy_ids",
    "quality_rules",
}


class SkillRegistryError(ValueError):
    """Raised before a job is created when Skill selection is invalid."""


@dataclass(frozen=True)
class ContentSkillDefinition:
    skill_id: str
    version: str
    display_name: str
    description: str
    instructions: str
    input_mode: SkillInputMode
    compatible_formats: tuple[ContentFormat, ...]
    default_for_formats: tuple[ContentFormat, ...]
    research_mode: SkillResearchMode
    research_prompt: str
    script_system_prompt: str
    script_prompt: str
    visual_policy: str
    policy_ids: tuple[str, ...]
    quality_rules: tuple[str, ...]
    manifest_hash: str

    def snapshot(self) -> ContentSkillSnapshot:
        return ContentSkillSnapshot(
            skill_id=self.skill_id,
            version=self.version,
            display_name=self.display_name,
            description=self.description,
            input_mode=self.input_mode,
            compatible_formats=list(self.compatible_formats),
            research_mode=self.research_mode,
            research_prompt=self.research_prompt,
            script_system_prompt=self.script_system_prompt,
            visual_policy=self.visual_policy,
            policy_ids=list(self.policy_ids),
            quality_rules=list(self.quality_rules),
            manifest_hash=self.manifest_hash,
            frozen_at=timestamp(),
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            "skill_id": self.skill_id,
            "version": self.version,
            "display_name": self.display_name,
            "description": self.description,
            "input_mode": self.input_mode.value,
            "compatible_formats": [item.value for item in self.compatible_formats],
            "research_mode": self.research_mode.value,
            "policy_ids": list(self.policy_ids),
            "quality_rules": list(self.quality_rules),
            "generation_defaults": {
                "script_prompt": self.script_prompt,
            },
        }


def _skill_metadata(path: Path) -> tuple[str, str, str]:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"无法读取 Skill 说明：{path}") from exc
    lines = text.splitlines()
    if len(lines) < 4 or lines[0].strip() != "---":
        raise RuntimeError(f"Skill 缺少 YAML frontmatter：{path}")
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], 1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise RuntimeError(f"Skill frontmatter 未闭合：{path}") from exc
    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        key, separator, value = line.partition(":")
        if separator:
            metadata[key.strip()] = value.strip().strip('"').strip("'")
    if set(metadata) != {"name", "description"}:
        raise RuntimeError(f"Skill frontmatter 只能包含 name 和 description：{path}")
    name = metadata["name"]
    description = metadata["description"]
    if not name or not description:
        raise RuntimeError(f"Skill name/description 不能为空：{path}")
    return name, description, "\n".join(lines[closing + 1:]).strip()


def _read_reference(skill_root: Path, raw_path: Any) -> str:
    relative = Path(str(raw_path or ""))
    target = (skill_root / relative).resolve()
    try:
        target.relative_to(skill_root.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Skill 引用了目录外文件：{raw_path}") from exc
    try:
        value = target.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"无法读取 Skill 资源：{target}") from exc
    if not value:
        raise RuntimeError(f"Skill 资源不能为空：{target}")
    return value


def _load_definition(skill_root: Path) -> ContentSkillDefinition:
    skill_id, description, instructions = _skill_metadata(
        skill_root / "SKILL.md"
    )
    if skill_root.name != skill_id:
        raise RuntimeError(
            f"Skill 目录名与 frontmatter name 不一致：{skill_root.name}"
        )
    try:
        manifest = json.loads(
            (skill_root / "manifest.json").read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Skill manifest 无法读取：{skill_root}") from exc
    if not isinstance(manifest, dict):
        raise RuntimeError(f"Skill manifest 必须是 JSON 对象：{skill_root}")
    unknown = set(manifest) - _MANIFEST_KEYS
    missing = _MANIFEST_KEYS - set(manifest)
    if unknown or missing:
        raise RuntimeError(
            f"Skill manifest 字段不完整：missing={sorted(missing)}, "
            f"unknown={sorted(unknown)}"
        )
    if manifest["schema_version"] != "1.0":
        raise RuntimeError(f"不支持的 Skill manifest 版本：{skill_root}")
    script_prompt = _read_reference(
        skill_root, manifest["script_prompt_file"]
    )
    visual_policy = _read_reference(
        skill_root, manifest["visual_policy_file"]
    )
    research_prompt = _read_reference(
        skill_root, manifest["research_prompt_file"]
    )
    hash_payload = {
        "skill_id": skill_id,
        "description": description,
        "instructions": instructions,
        "manifest": manifest,
        "script_prompt": script_prompt,
        "visual_policy": visual_policy,
        "research_prompt": research_prompt,
    }
    try:
        definition = ContentSkillDefinition(
            skill_id=skill_id,
            version=str(manifest["version"]),
            display_name=str(manifest["display_name"]),
            description=description,
            instructions=instructions,
            input_mode=SkillInputMode(manifest["input_mode"]),
            compatible_formats=tuple(
                ContentFormat(item)
                for item in manifest["compatible_formats"]
            ),
            default_for_formats=tuple(
                ContentFormat(item)
                for item in manifest["default_for_formats"]
            ),
            research_mode=SkillResearchMode(manifest["research_mode"]),
            research_prompt=research_prompt,
            script_system_prompt=str(manifest["script_system_prompt"]),
            script_prompt=script_prompt,
            visual_policy=visual_policy,
            policy_ids=tuple(str(item) for item in manifest["policy_ids"]),
            quality_rules=tuple(
                str(item) for item in manifest["quality_rules"]
            ),
            manifest_hash=content_hash(hash_payload),
        )
        # Reuse the persisted contract as the strict validation surface.
        definition.snapshot()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Skill manifest 内容无效：{skill_root}") from exc
    if not set(definition.default_for_formats).issubset(
        set(definition.compatible_formats)
    ):
        raise RuntimeError(f"Skill 默认格式必须包含在兼容格式中：{skill_root}")
    return definition


def _version_key(version: str) -> tuple[int, int, int, bool, str]:
    stable, _, suffix = version.partition("-")
    major, minor, patch = (int(item) for item in stable.split("."))
    # For the same numeric version, SemVer ranks the stable release above
    # prereleases. The suffix remains a deterministic tie breaker.
    return major, minor, patch, not bool(suffix), suffix


class ContentSkillRegistry:
    def __init__(self, definitions: list[ContentSkillDefinition]):
        if not definitions:
            raise RuntimeError("至少需要注册一个 Content Skill")
        self._definitions: dict[tuple[str, str], ContentSkillDefinition] = {}
        self._defaults: dict[ContentFormat, ContentSkillDefinition] = {}
        for definition in definitions:
            key = (definition.skill_id, definition.version)
            if key in self._definitions:
                raise RuntimeError(f"重复的 Skill 版本：{key}")
            self._definitions[key] = definition
            for content_format in definition.default_for_formats:
                if content_format in self._defaults:
                    raise RuntimeError(
                        f"内容格式存在多个默认 Skill：{content_format.value}"
                    )
                self._defaults[content_format] = definition

    @classmethod
    def load(cls, root: Path = CONTENT_SKILL_ROOT) -> "ContentSkillRegistry":
        definitions = [
            _load_definition(path)
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and (path / "manifest.json").is_file()
        ]
        return cls(definitions)

    def resolve(
        self, skill_id: str, version: str = ""
    ) -> ContentSkillDefinition:
        normalized_id = str(skill_id or "").strip()
        normalized_version = str(version or "").strip()
        matches = [
            definition
            for (candidate_id, _), definition in self._definitions.items()
            if candidate_id == normalized_id
        ]
        if not matches:
            raise SkillRegistryError(f"未知 Content Skill：{normalized_id}")
        if normalized_version:
            exact = self._definitions.get((normalized_id, normalized_version))
            if not exact:
                raise SkillRegistryError(
                    f"Content Skill 版本不存在：{normalized_id}@{normalized_version}"
                )
            return exact
        return max(matches, key=lambda item: _version_key(item.version))

    def recommend(self, card: SourceCard) -> ContentSkillDefinition:
        definition = self._defaults.get(card.content_format)
        if not definition:
            raise SkillRegistryError(
                f"内容格式尚未配置默认 Skill：{card.content_format.value}"
            )
        return definition

    def freeze(
        self,
        card: SourceCard,
        settings: GenerationSettings,
    ) -> tuple[GenerationSettings, ContentSkillSnapshot]:
        definition = (
            self.resolve(settings.skill_id, settings.skill_version)
            if settings.skill_id
            else self.recommend(card)
        )
        if card.content_format not in definition.compatible_formats:
            raise SkillRegistryError(
                f"Skill {definition.skill_id}@{definition.version} "
                f"不支持内容格式 {card.content_format.value}"
            )
        explicitly_set = set(settings.model_fields_set)
        effective = settings.model_copy(deep=True)
        if "script_prompt" not in explicitly_set:
            effective.script_prompt = definition.script_prompt
        effective.skill_id = definition.skill_id
        effective.skill_version = definition.version
        snapshot = definition.snapshot()
        if (
            snapshot.research_mode
            == SkillResearchMode.PERSON_VIEWPOINT_OPTIONAL
            and card.content_format != ContentFormat.PERSON_IDEA
        ):
            # Existing verified concept/book/research cards did not trigger a
            # paid person search. Preserve that behavior while sharing the
            # expert-view writing and visual Skill.
            snapshot = snapshot.model_copy(update={
                "research_mode": SkillResearchMode.NONE,
                "research_prompt": "",
            })
        return effective, snapshot

    def public_catalog(self) -> list[dict[str, Any]]:
        latest: dict[str, ContentSkillDefinition] = {}
        for definition in self._definitions.values():
            current = latest.get(definition.skill_id)
            if not current or _version_key(definition.version) > _version_key(
                current.version
            ):
                latest[definition.skill_id] = definition
        return [
            latest[skill_id].public_payload()
            for skill_id in sorted(latest)
        ]


default_skill_registry = ContentSkillRegistry.load()
