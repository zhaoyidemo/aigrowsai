'''Versioned last-mile media prompt adapters.'''
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qijia_video.contracts import (
    GenerationSettings,
    ProviderAdapterSnapshot,
    content_hash,
    timestamp,
)


PROVIDER_ADAPTER_ROOT = Path(__file__).resolve().parent / 'provider_adapters'
_MANIFEST_KEYS = {
    'schema_version',
    'version',
    'display_name',
    'description',
    'default',
    'image_provider_family',
    'video_provider_family',
    'image_framework_file',
    'video_framework_file',
    'reference_policy_file',
    'audio_policy',
    'negative_rules',
}


class ProviderAdapterRegistryError(ValueError):
    pass


@dataclass(frozen=True)
class ProviderAdapterDefinition:
    adapter_id: str
    version: str
    display_name: str
    description: str
    default: bool
    image_provider_family: str
    video_provider_family: str
    image_framework: str
    video_framework: str
    reference_policy: str
    audio_policy: str
    negative_rules: tuple[str, ...]
    manifest_hash: str

    def snapshot(self) -> ProviderAdapterSnapshot:
        return ProviderAdapterSnapshot(
            adapter_id=self.adapter_id,
            version=self.version,
            display_name=self.display_name,
            description=self.description,
            image_provider_family=self.image_provider_family,
            video_provider_family=self.video_provider_family,
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
            'adapter_id': self.adapter_id,
            'version': self.version,
            'display_name': self.display_name,
            'description': self.description,
            'image_provider_family': self.image_provider_family,
            'video_provider_family': self.video_provider_family,
            'role': 'provider_syntax_only',
        }


def _resource(root: Path, relative: str) -> str:
    path = (root / str(relative or '')).resolve()
    if root.resolve() not in path.parents:
        raise RuntimeError(f'Provider Adapter 资源越界：{path}')
    try:
        value = path.read_text(encoding='utf-8').strip()
    except OSError as exc:
        raise RuntimeError(f'无法读取 Provider Adapter 资源：{path}') from exc
    if not value:
        raise RuntimeError(f'Provider Adapter 资源为空：{path}')
    return value


def _load_definition(root: Path) -> ProviderAdapterDefinition:
    try:
        manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f'无法读取 Provider Adapter manifest：{root}') from exc
    if not isinstance(manifest, dict) or set(manifest) != _MANIFEST_KEYS:
        raise RuntimeError(f'Provider Adapter manifest 字段不完整：{root}')
    if manifest['schema_version'] != '1.0':
        raise RuntimeError(f'不支持的 Provider Adapter manifest 版本：{root}')
    image_framework = _resource(root, manifest['image_framework_file'])
    video_framework = _resource(root, manifest['video_framework_file'])
    reference_policy = _resource(root, manifest['reference_policy_file'])
    payload = {
        'adapter_id': root.name,
        'manifest': manifest,
        'image_framework': image_framework,
        'video_framework': video_framework,
        'reference_policy': reference_policy,
    }
    try:
        definition = ProviderAdapterDefinition(
            adapter_id=root.name,
            version=str(manifest['version']),
            display_name=str(manifest['display_name']),
            description=str(manifest['description']),
            default=bool(manifest['default']),
            image_provider_family=str(manifest['image_provider_family']),
            video_provider_family=str(manifest['video_provider_family']),
            image_framework=image_framework,
            video_framework=video_framework,
            reference_policy=reference_policy,
            audio_policy=str(manifest['audio_policy']),
            negative_rules=tuple(str(item) for item in manifest['negative_rules']),
            manifest_hash=content_hash(payload),
        )
        definition.snapshot()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f'Provider Adapter 内容无效：{root}') from exc
    return definition


def _version_key(version: str) -> tuple[int, int, int, bool, str]:
    stable, _, suffix = version.partition('-')
    major, minor, patch = (int(item) for item in stable.split('.'))
    return major, minor, patch, not bool(suffix), suffix


class ProviderAdapterRegistry:
    def __init__(self, definitions: list[ProviderAdapterDefinition]):
        if not definitions:
            raise RuntimeError('至少需要注册一个 Provider Adapter')
        self._definitions = {
            (item.adapter_id, item.version): item for item in definitions
        }
        defaults = [item for item in definitions if item.default]
        if len(self._definitions) != len(definitions) or len(defaults) != 1:
            raise RuntimeError('Provider Adapter 版本必须唯一且只能有一个默认项')
        self._default = defaults[0]

    @classmethod
    def load(cls, root: Path = PROVIDER_ADAPTER_ROOT) -> 'ProviderAdapterRegistry':
        return cls([
            _load_definition(path)
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and (path / 'manifest.json').is_file()
        ])

    def resolve(
        self,
        adapter_id: str = '',
        version: str = '',
    ) -> ProviderAdapterDefinition:
        selected_id = str(adapter_id or self._default.adapter_id).strip()
        selected_version = str(version or '').strip()
        matches = [
            item for (candidate_id, _), item in self._definitions.items()
            if candidate_id == selected_id
        ]
        if not matches:
            raise ProviderAdapterRegistryError(f'未知 Provider Adapter：{selected_id}')
        if selected_version:
            exact = self._definitions.get((selected_id, selected_version))
            if not exact:
                raise ProviderAdapterRegistryError(
                    f'Provider Adapter 版本不存在：{selected_id}@{selected_version}'
                )
            return exact
        return max(matches, key=lambda item: _version_key(item.version))

    def freeze(
        self,
        settings: GenerationSettings,
    ) -> tuple[GenerationSettings, ProviderAdapterSnapshot]:
        definition = self.resolve(
            settings.provider_adapter_id,
            settings.provider_adapter_version,
        )
        effective = settings.model_copy(deep=True)
        effective.provider_adapter_id = definition.adapter_id
        effective.provider_adapter_version = definition.version
        return effective, definition.snapshot()

    def public_default(self) -> dict[str, Any]:
        return self._default.public_payload()


default_provider_adapter_registry = ProviderAdapterRegistry.load()
