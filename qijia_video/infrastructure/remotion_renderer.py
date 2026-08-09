"""Remotion 渲染适配器。"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import shutil
import subprocess
from pathlib import Path

from qijia_video.contracts import RenderManifest
from qijia_video.errors import ProviderUnavailable


class RemotionRenderer:
    name = "remotion"

    def __init__(
        self,
        renderer_root: Path,
        *,
        timeout_seconds: int = 1800,
        node_binary: str = "node",
        concurrency: str = "50%",
    ):
        self.renderer_root = renderer_root.resolve()
        self.timeout_seconds = max(60, int(timeout_seconds))
        self.node_binary = str(node_binary or "node")
        self.concurrency = str(concurrency or "50%")

    def available(self) -> tuple[bool, str]:
        node = shutil.which(self.node_binary)
        entry = self.renderer_root / "render.mjs"
        modules = self.renderer_root / "node_modules" / "remotion"
        if not node:
            return False, "未找到 Node.js"
        if not entry.is_file():
            return False, "缺少 Remotion 渲染入口"
        if not modules.exists():
            return False, "Remotion 依赖尚未安装"
        return True, "ready"

    def metadata(self) -> dict:
        package_path = self.renderer_root / "package.json"
        package = {}
        if package_path.is_file():
            try:
                package = json.loads(package_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                package = {}
        node_version = "unknown"
        node = shutil.which(self.node_binary)
        if node:
            try:
                node_version = subprocess.run(
                    [node, "--version"],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=5,
                ).stdout.strip()
            except (OSError, subprocess.SubprocessError):
                pass
        dependencies = package.get("dependencies") or {}
        return {
            "name": self.name,
            "package_version": package.get("version") or "unknown",
            "remotion_version": dependencies.get("remotion") or "unknown",
            "node_version": node_version,
        }

    async def _invoke(
        self,
        manifest,
        storage,
        workspace: Path,
        *,
        output_name: str,
        still: bool,
        cover_output_name: str | None = None,
    ) -> Path:
        manifest = RenderManifest.model_validate(manifest)
        ready, reason = self.available()
        if not ready:
            raise ProviderUnavailable(reason)
        assets_dir = workspace / "render-assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        resolved_assets: dict[str, str] = {}
        for asset in manifest.assets:
            suffix = Path(asset.object_key).suffix
            if not suffix:
                suffix = mimetypes.guess_extension(asset.media_type) or ".bin"
            destination = assets_dir / f"{asset.asset_id}{suffix}"
            await storage.materialize(asset, destination)
            resolved_assets[asset.asset_id] = str(destination.resolve())

        runtime = manifest.model_dump(mode="json")
        runtime["resolved_assets"] = resolved_assets
        runtime_path = workspace / "render_manifest.runtime.json"
        runtime_path.write_text(
            json.dumps(runtime, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        output = workspace / output_name
        cover_output = (
            workspace / cover_output_name if cover_output_name else None
        )
        node = shutil.which(self.node_binary) or self.node_binary
        environment = dict(os.environ)
        environment["REMOTION_CONCURRENCY"] = self.concurrency
        arguments = [
            node,
            str(self.renderer_root / "render.mjs"),
            "--manifest", str(runtime_path),
            "--output", str(output),
        ]
        if still:
            arguments.append("--still")
        if cover_output:
            arguments.extend(["--cover-output", str(cover_output)])
        process = await asyncio.create_subprocess_exec(
            *arguments,
            cwd=str(self.renderer_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=environment,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise ProviderUnavailable("Remotion 渲染超时") from exc
        log = stdout.decode("utf-8", errors="replace")
        if (
            process.returncode != 0
            or not output.is_file()
            or (cover_output is not None and not cover_output.is_file())
        ):
            raise ProviderUnavailable(
                "Remotion 渲染失败：" + log[-4000:]
            )
        return output

    async def render(self, manifest, storage, workspace: Path) -> Path:
        return await self._invoke(
            manifest,
            storage,
            workspace,
            output_name="draft.mp4",
            still=False,
            cover_output_name="cover.jpg",
        )

    async def render_cover(self, manifest, storage, workspace: Path) -> Path:
        cached = workspace / "cover.jpg"
        if cached.is_file():
            return cached
        return await self._invoke(
            manifest,
            storage,
            workspace,
            output_name="cover.jpg",
            still=True,
        )
