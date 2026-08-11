"""Versioned internal prompt adapters for direct Script Skill invocation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qijia_video.contracts import (
    DEFAULT_PROMPT_ADAPTER_ID,
    PromptAdapterSnapshot,
    content_hash,
    timestamp,
)


PROMPT_ADAPTER_ROOT = Path(__file__).resolve().parent / "prompt_adapters"


def _version_key(version: str) -> tuple[int, int, int, bool, str]:
    stable, _, suffix = version.partition("-")
    try:
        major, minor, patch = (int(item) for item in stable.split("."))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"提示词适配器版本不是有效 SemVer：{version}") from exc
    return major, minor, patch, not bool(suffix), suffix


@dataclass(frozen=True)
class PromptAdapterDefinition:
    adapter_id: str
    version: str
    display_name: str
    description: str
    default: bool
    compilation_framework: str
    quality_rules: tuple[str, ...]
    manifest_hash: str

    def snapshot(self) -> PromptAdapterSnapshot:
        return PromptAdapterSnapshot(
            adapter_id=self.adapter_id,
            version=self.version,
            display_name=self.display_name,
            description=self.description,
            compilation_framework=self.compilation_framework,
            quality_rules=list(self.quality_rules),
            manifest_hash=self.manifest_hash,
            frozen_at=timestamp(),
        )


def _load_definition(root: Path) -> PromptAdapterDefinition:
    manifest_path = root / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"提示词适配器 manifest 无法读取：{manifest_path}") from exc
    expected = {
        "schema_version",
        "version",
        "display_name",
        "description",
        "default",
        "compilation_framework_file",
        "quality_rules",
    }
    if not isinstance(manifest, dict) or set(manifest) != expected:
        raise RuntimeError(f"提示词适配器 manifest 字段不完整：{manifest_path}")
    if manifest["schema_version"] != "1.0":
        raise RuntimeError(f"不支持的提示词适配器 manifest 版本：{manifest_path}")
    _version_key(str(manifest["version"]))
    framework_path = (root / str(manifest["compilation_framework_file"])).resolve()
    if root.resolve() not in framework_path.parents:
        raise RuntimeError(f"提示词适配器资源越界：{framework_path}")
    try:
        framework = framework_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"提示词适配器资源无法读取：{framework_path}") from exc
    payload: dict[str, Any] = {
        "adapter_id": root.name,
        "manifest": manifest,
        "compilation_framework": framework,
    }
    definition = PromptAdapterDefinition(
        adapter_id=root.name,
        version=str(manifest["version"]),
        display_name=str(manifest["display_name"]),
        description=str(manifest["description"]),
        default=bool(manifest["default"]),
        compilation_framework=framework,
        quality_rules=tuple(str(item) for item in manifest["quality_rules"]),
        manifest_hash=content_hash(payload),
    )
    definition.snapshot()
    return definition


class PromptAdapterRegistry:
    def __init__(self, definitions: list[PromptAdapterDefinition]):
        if not definitions:
            raise RuntimeError("至少需要注册一个内部提示词适配器")
        self._definitions = {
            (item.adapter_id, item.version): item for item in definitions
        }
        defaults = [item for item in definitions if item.default]
        if len(defaults) != 1:
            raise RuntimeError("内部提示词适配器必须且只能声明一个默认版本")
        self._default = defaults[0]
        if self._default.adapter_id != DEFAULT_PROMPT_ADAPTER_ID:
            raise RuntimeError(
                f"默认提示词适配器必须是 {DEFAULT_PROMPT_ADAPTER_ID}"
            )

    @classmethod
    def load(cls, root: Path = PROMPT_ADAPTER_ROOT) -> "PromptAdapterRegistry":
        return cls([
            _load_definition(path)
            for path in sorted(root.iterdir(), key=lambda item: item.name)
            if path.is_dir() and (path / "manifest.json").is_file()
        ])

    def freeze_default(self) -> PromptAdapterSnapshot:
        return self._default.snapshot()


default_prompt_adapter_registry = PromptAdapterRegistry.load()
