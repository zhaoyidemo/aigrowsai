'''Versioned directing methods, independent from visual style and providers.'''
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qijia_video.contracts import (
    ContentFormat,
    DirectorSkillSnapshot,
    GenerationSettings,
    SourceCard,
    content_hash,
    timestamp,
)


DIRECTOR_SKILL_ROOT = Path(__file__).resolve().parent / 'director_skills'
_MANIFEST_KEYS = {
    'schema_version',
    'version',
    'display_name',
    'description',
    'default',
    'mode',
    'compatible_formats',
    'workflow_file',
    'scene_design_file',
    'shot_design_file',
    'continuity_file',
    'media_policy_file',
    'critic_rules',
}


class DirectorSkillRegistryError(ValueError):
    pass


def _version_key(version: str) -> tuple[int, int, int, bool, str]:
    try:
        stable, _, suffix = version.partition('-')
        major, minor, patch = (int(item) for item in stable.split('.'))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'Director Skill 版本不是有效 SemVer：{version}') from exc
    return major, minor, patch, not bool(suffix), suffix


def _read_resource(root: Path, name: str, *, skill_body: bool = False) -> str:
    target = (root / str(name or '')).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise RuntimeError(f'Director Skill 资源越界：{target}') from exc
    try:
        value = target.read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise RuntimeError(f'无法读取 Director Skill 资源：{target}') from exc
    if skill_body and value.startswith('---'):
        parts = value.split('---', 2)
        if len(parts) != 3:
            raise RuntimeError(f'Director Skill frontmatter 不完整：{target}')
        value = parts[2].strip()
    if not value:
        raise RuntimeError(f'Director Skill 资源为空：{target}')
    return value


@dataclass(frozen=True)
class DirectorSkillDefinition:
    skill_id: str
    version: str
    display_name: str
    description: str
    default: bool
    mode: str
    compatible_formats: tuple[ContentFormat, ...]
    workflow_instructions: str
    scene_design_rules: str
    shot_design_rules: str
    continuity_rules: str
    media_rules: str
    critic_rules: tuple[str, ...]
    manifest_hash: str

    def snapshot(self) -> DirectorSkillSnapshot:
        return DirectorSkillSnapshot(
            skill_id=self.skill_id,
            version=self.version,
            display_name=self.display_name,
            description=self.description,
            mode=self.mode,
            compatible_formats=list(self.compatible_formats),
            workflow_instructions=self.workflow_instructions,
            scene_design_rules=self.scene_design_rules,
            shot_design_rules=self.shot_design_rules,
            continuity_rules=self.continuity_rules,
            media_rules=self.media_rules,
            critic_rules=list(self.critic_rules),
            manifest_hash=self.manifest_hash,
            frozen_at=timestamp(),
        )

    def public_payload(self) -> dict[str, Any]:
        return {
            'skill_id': self.skill_id,
            'version': self.version,
            'display_name': self.display_name,
            'description': self.description,
            'mode': self.mode,
            'compatible_formats': [item.value for item in self.compatible_formats],
            'default': self.default,
            'role': 'director_owner',
        }


def _load_definition(root: Path) -> DirectorSkillDefinition:
    try:
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'无法读取 Director Skill manifest：{root}') from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise RuntimeError(f'Director Skill manifest 字段不完整：{root}')
    if manifest['schema_version'] != '1.0':
        raise RuntimeError(f'不支持的 Director Skill manifest 版本：{root}')
    _version_key(str(manifest['version']))
    resources = {
        'workflow': _read_resource(
            root, manifest['workflow_file'], skill_body=True
        ),
        'scene_design': _read_resource(root, manifest['scene_design_file']),
        'shot_design': _read_resource(root, manifest['shot_design_file']),
        'continuity': _read_resource(root, manifest['continuity_file']),
        'media_policy': _read_resource(root, manifest['media_policy_file']),
    }
    payload = {
        'skill_id': root.name,
        'manifest': manifest,
        **resources,
    }
    try:
        definition = DirectorSkillDefinition(
            skill_id=root.name,
            version=str(manifest['version']),
            display_name=str(manifest['display_name']),
            description=str(manifest['description']),
            default=bool(manifest['default']),
            mode=str(manifest['mode']),
            compatible_formats=tuple(
                ContentFormat(item) for item in manifest['compatible_formats']
            ),
            workflow_instructions=resources['workflow'],
            scene_design_rules=resources['scene_design'],
            shot_design_rules=resources['shot_design'],
            continuity_rules=resources['continuity'],
            media_rules=resources['media_policy'],
            critic_rules=tuple(str(item) for item in manifest['critic_rules']),
            manifest_hash=content_hash(payload),
        )
        definition.snapshot()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'Director Skill 内容无效：{root}') from exc
    return definition


class DirectorSkillRegistry:
    def __init__(self, definitions: list[DirectorSkillDefinition]):
        if not definitions:
            raise RuntimeError('至少需要注册一个 Director Skill')
        self._definitions = {
            (item.skill_id, item.version): item for item in definitions
        }
        defaults = [item for item in definitions if item.default]
        if len(self._definitions) != len(definitions) or len(defaults) != 1:
            raise RuntimeError('Director Skill 版本必须唯一且只能有一个默认项')
        self._default = defaults[0]

    @classmethod
    def load(cls, root: Path = DIRECTOR_SKILL_ROOT) -> 'DirectorSkillRegistry':
        return cls([
            _load_definition(path)
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and (path / 'manifest.json').is_file()
        ])

    def resolve(
        self,
        skill_id: str = '',
        version: str = '',
    ) -> DirectorSkillDefinition:
        selected_id = str(skill_id or self._default.skill_id).strip()
        selected_version = str(version or '').strip()
        matches = [
            item
            for (candidate_id, _), item in self._definitions.items()
            if candidate_id == selected_id
        ]
        if not matches:
            raise DirectorSkillRegistryError(
                f'未知 Director Skill：{selected_id}'
            )
        if selected_version:
            exact = self._definitions.get((selected_id, selected_version))
            if not exact:
                raise DirectorSkillRegistryError(
                    f'Director Skill 版本不存在：'
                    f'{selected_id}@{selected_version}'
                )
            return exact
        return max(matches, key=lambda item: _version_key(item.version))

    def freeze(
        self,
        card: SourceCard,
        settings: GenerationSettings,
    ) -> tuple[GenerationSettings, DirectorSkillSnapshot]:
        definition = self.resolve(
            settings.director_skill_id,
            settings.director_skill_version,
        )
        if card.content_format not in definition.compatible_formats:
            raise DirectorSkillRegistryError(
                f'{definition.display_name} 不支持 {card.content_format.value}'
            )
        effective = settings.model_copy(deep=True)
        effective.director_skill_id = definition.skill_id
        effective.director_skill_version = definition.version
        return effective, definition.snapshot()

    def public_catalog(self) -> list[dict[str, Any]]:
        latest: dict[str, DirectorSkillDefinition] = {}
        for item in self._definitions.values():
            current = latest.get(item.skill_id)
            if not current or _version_key(item.version) > _version_key(
                current.version
            ):
                latest[item.skill_id] = item
        return [latest[key].public_payload() for key in sorted(latest)]


default_director_skill_registry = DirectorSkillRegistry.load()
