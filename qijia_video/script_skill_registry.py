'''Versioned script-creation strategies with one active owner per task.'''
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qijia_video.contracts import (
    ContentFormat,
    GenerationSettings,
    ScriptSkillSnapshot,
    SourceCard,
    content_hash,
    timestamp,
)


SCRIPT_SKILL_ROOT = Path(__file__).resolve().parent / 'script_skills'
_MANIFEST_KEYS = {
    'schema_version',
    'version',
    'display_name',
    'description',
    'default',
    'compatible_formats',
    'planning_instructions_file',
    'writing_instructions_file',
    'critic_rules',
}


class ScriptSkillRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ScriptSkillDefinition:
    skill_id: str
    version: str
    display_name: str
    description: str
    default: bool
    compatible_formats: tuple[ContentFormat, ...]
    planning_instructions: str
    writing_instructions: str
    critic_rules: tuple[str, ...]
    manifest_hash: str

    def snapshot(self) -> ScriptSkillSnapshot:
        return ScriptSkillSnapshot(
            skill_id=self.skill_id,
            version=self.version,
            display_name=self.display_name,
            description=self.description,
            compatible_formats=list(self.compatible_formats),
            planning_instructions=self.planning_instructions,
            writing_instructions=self.writing_instructions,
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
            'compatible_formats': [item.value for item in self.compatible_formats],
            'default': self.default,
            'role': 'script_owner',
        }


def _read_resource(root: Path, name: str) -> str:
    path = (root / str(name or '')).resolve()
    if root.resolve() not in path.parents:
        raise RuntimeError(f'Script Skill 资源越界：{path}')
    try:
        value = path.read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise RuntimeError(f'无法读取 Script Skill 资源：{path}') from exc
    if not value:
        raise RuntimeError(f'Script Skill 资源为空：{path}')
    return value


def _load_definition(root: Path) -> ScriptSkillDefinition:
    try:
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'无法读取 Script Skill manifest：{root}') from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise RuntimeError(f'Script Skill manifest 字段不完整：{root}')
    if manifest['schema_version'] != '1.0':
        raise RuntimeError(f'不支持的 Script Skill manifest 版本：{root}')
    planning = _read_resource(root, manifest['planning_instructions_file'])
    writing = _read_resource(root, manifest['writing_instructions_file'])
    payload = {
        'skill_id': root.name,
        'manifest': manifest,
        'planning': planning,
        'writing': writing,
    }
    try:
        definition = ScriptSkillDefinition(
            skill_id=root.name,
            version=str(manifest['version']),
            display_name=str(manifest['display_name']),
            description=str(manifest['description']),
            default=bool(manifest['default']),
            compatible_formats=tuple(
                ContentFormat(item) for item in manifest['compatible_formats']
            ),
            planning_instructions=planning,
            writing_instructions=writing,
            critic_rules=tuple(str(item) for item in manifest['critic_rules']),
            manifest_hash=content_hash(payload),
        )
        definition.snapshot()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'Script Skill 内容无效：{root}') from exc
    return definition


def _version_key(version: str) -> tuple[int, int, int, bool, str]:
    stable, _, suffix = version.partition('-')
    major, minor, patch = (int(item) for item in stable.split('.'))
    return major, minor, patch, not bool(suffix), suffix


class ScriptSkillRegistry:
    def __init__(self, definitions: list[ScriptSkillDefinition]):
        if not definitions:
            raise RuntimeError('至少需要注册一个 Script Skill')
        self._definitions = {
            (item.skill_id, item.version): item for item in definitions
        }
        defaults = [item for item in definitions if item.default]
        if len(self._definitions) != len(definitions) or len(defaults) != 1:
            raise RuntimeError('Script Skill 版本必须唯一且只能有一个默认项')
        self._default = defaults[0]

    @classmethod
    def load(cls, root: Path = SCRIPT_SKILL_ROOT) -> 'ScriptSkillRegistry':
        return cls([
            _load_definition(path)
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and (path / 'manifest.json').is_file()
        ])

    def resolve(self, skill_id: str = '', version: str = '') -> ScriptSkillDefinition:
        selected_id = str(skill_id or self._default.skill_id).strip()
        selected_version = str(version or '').strip()
        matches = [
            item for (candidate_id, _), item in self._definitions.items()
            if candidate_id == selected_id
        ]
        if not matches:
            raise ScriptSkillRegistryError(f'未知 Script Skill：{selected_id}')
        if selected_version:
            exact = self._definitions.get((selected_id, selected_version))
            if not exact:
                raise ScriptSkillRegistryError(
                    f'Script Skill 版本不存在：{selected_id}@{selected_version}'
                )
            return exact
        return max(matches, key=lambda item: _version_key(item.version))

    def freeze(
        self,
        card: SourceCard,
        settings: GenerationSettings,
    ) -> tuple[GenerationSettings, ScriptSkillSnapshot]:
        definition = self.resolve(
            settings.script_skill_id,
            settings.script_skill_version,
        )
        if card.content_format not in definition.compatible_formats:
            raise ScriptSkillRegistryError(
                f'{definition.display_name} 不支持 {card.content_format.value}'
            )
        effective = settings.model_copy(deep=True)
        effective.script_skill_id = definition.skill_id
        effective.script_skill_version = definition.version
        return effective, definition.snapshot()

    def public_catalog(self) -> list[dict[str, Any]]:
        latest: dict[str, ScriptSkillDefinition] = {}
        for item in self._definitions.values():
            current = latest.get(item.skill_id)
            if not current or _version_key(item.version) > _version_key(current.version):
                latest[item.skill_id] = item
        return [latest[key].public_payload() for key in sorted(latest)]


default_script_skill_registry = ScriptSkillRegistry.load()
