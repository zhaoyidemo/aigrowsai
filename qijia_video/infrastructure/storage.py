"""可替换的本地/TOS 资产存储实现。"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path, PurePosixPath

from qijia_video.contracts import AssetRef
from qijia_video.errors import ProviderUnavailable


TOS_SOCKET_TIMEOUT_SECONDS = 300
TOS_DOWNLOAD_ATTEMPTS = 3
TOS_DOWNLOAD_PART_SIZE_BYTES = 4 * 1024 * 1024
TOS_DOWNLOAD_TASK_COUNT = 2
TOS_DOWNLOAD_RETRY_DELAYS_SECONDS = (1.0, 3.0)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_key(value: str) -> str:
    key = str(PurePosixPath(str(value or "").strip()))
    if not key or key.startswith(("/", "../")) or "/../" in f"/{key}/":
        raise ValueError("无效的对象存储键")
    return key


class LocalArtifactStorage:
    name = "local"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @property
    def configured(self) -> bool:
        return True

    def _path(self, object_key: str) -> Path:
        target = (self.root / _safe_key(object_key)).resolve()
        try:
            target.relative_to(self.root)
        except ValueError as exc:
            raise ValueError("对象键超出本地存储目录") from exc
        return target

    def path_for(self, object_key: str) -> Path:
        """仅供同进程下载适配器使用，不向领域层暴露本地路径。"""
        return self._path(object_key)

    async def put_file(
        self,
        *,
        object_key: str,
        path: Path,
        asset_id: str,
        media_type: str,
        duration_seconds: float | None = None,
    ) -> AssetRef:
        source = path.resolve()
        if not source.is_file():
            raise FileNotFoundError(str(source))
        target = self._path(object_key)
        target.parent.mkdir(parents=True, exist_ok=True)
        if source != target:
            await asyncio.to_thread(shutil.copyfile, source, target)
        return AssetRef(
            asset_id=asset_id,
            object_key=_safe_key(object_key),
            sha256=await asyncio.to_thread(_sha256, target),
            size_bytes=target.stat().st_size,
            media_type=media_type,
            duration_seconds=duration_seconds,
        )

    async def materialize(self, asset: AssetRef, destination: Path) -> Path:
        source = self._path(asset.object_key)
        if not source.is_file():
            raise FileNotFoundError(asset.object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            await asyncio.to_thread(shutil.copyfile, source, destination)
        actual = await asyncio.to_thread(_sha256, destination)
        if actual != asset.sha256:
            raise RuntimeError(f"资产校验失败：{asset.asset_id}")
        return destination

    async def signed_get_url(self, asset: AssetRef, *, expires: int = 3600) -> str:
        return f"local://{asset.object_key}"


class TosArtifactStorage:
    name = "tos"

    def __init__(
        self,
        *,
        access_key_id: str,
        secret_access_key: str,
        bucket: str,
        region: str = "cn-shanghai",
    ):
        self.access_key_id = str(access_key_id or "").strip()
        self.secret_access_key = str(secret_access_key or "").strip()
        self.bucket = str(bucket or "").strip()
        self.region = str(region or "").strip()

    @property
    def configured(self) -> bool:
        return bool(
            self.access_key_id
            and self.secret_access_key
            and self.bucket
            and self.region
        )

    def _client(self):
        if not self.configured:
            raise ProviderUnavailable("TOS 配置不完整")
        import tos

        return tos.TosClientV2(
            self.access_key_id,
            self.secret_access_key,
            f"tos-{self.region}.volces.com",
            self.region,
            max_retry_count=3,
            request_timeout=TOS_SOCKET_TIMEOUT_SECONDS,
            socket_timeout=TOS_SOCKET_TIMEOUT_SECONDS,
        )

    async def put_file(
        self,
        *,
        object_key: str,
        path: Path,
        asset_id: str,
        media_type: str,
        duration_seconds: float | None = None,
    ) -> AssetRef:
        key = _safe_key(object_key)
        source = path.resolve()
        digest = await asyncio.to_thread(_sha256, source)

        def upload():
            return self._client().put_object_from_file(
                self.bucket,
                key,
                str(source),
                content_type=media_type,
                meta={"sha256": digest, "asset-id": asset_id},
            )

        try:
            await asyncio.to_thread(upload)
        except Exception as exc:
            raise ProviderUnavailable(
                f"TOS 上传失败，可安全重试（{key}）：{exc}"
            ) from exc
        return AssetRef(
            asset_id=asset_id,
            object_key=key,
            sha256=digest,
            size_bytes=source.stat().st_size,
            media_type=media_type,
            duration_seconds=duration_seconds,
        )

    async def materialize(self, asset: AssetRef, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.is_file():
            actual = await asyncio.to_thread(_sha256, destination)
            if actual == asset.sha256:
                return destination
            destination.unlink(missing_ok=True)

        last_error: Exception | None = None
        def download():
            return self._client().download_file(
                self.bucket,
                asset.object_key,
                str(destination),
                part_size=TOS_DOWNLOAD_PART_SIZE_BYTES,
                task_num=TOS_DOWNLOAD_TASK_COUNT,
                enable_checkpoint=True,
            )

        for attempt in range(1, TOS_DOWNLOAD_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(download)
                actual = await asyncio.to_thread(_sha256, destination)
                if actual != asset.sha256:
                    raise RuntimeError(f"资产校验失败：{asset.asset_id}")
                return destination
            except Exception as exc:
                last_error = exc
                if destination.is_file():
                    destination.unlink(missing_ok=True)
                if attempt < TOS_DOWNLOAD_ATTEMPTS:
                    await asyncio.sleep(
                        TOS_DOWNLOAD_RETRY_DELAYS_SECONDS[attempt - 1]
                    )

        raise ProviderUnavailable(
            f"TOS 下载失败，已自动断点重试 {TOS_DOWNLOAD_ATTEMPTS} 次"
            f"（{asset.object_key}）：{last_error}"
        ) from last_error

    async def signed_get_url(self, asset: AssetRef, *, expires: int = 3600) -> str:
        import tos

        def sign():
            return self._client().pre_signed_url(
                tos.HttpMethodType.Http_Method_Get,
                self.bucket,
                asset.object_key,
                expires=max(60, min(int(expires), 21600)),
            ).signed_url

        url = str(await asyncio.to_thread(sign) or "")
        if not url.startswith("https://"):
            raise RuntimeError("TOS 未返回有效的 HTTPS 下载地址")
        return url


def storage_from_settings(project_root: Path, settings):
    """由模块配置构造存储；settings 只需提供同名属性，便于独立迁移。"""
    mode = str(settings.QIJIA_VIDEO_STORAGE or "local").strip().lower()
    if mode == "tos":
        return TosArtifactStorage(
            access_key_id=str(settings.VOLCENGINE_TOS_ACCESS_KEY_ID or "").strip(),
            secret_access_key=str(settings.VOLCENGINE_TOS_SECRET_ACCESS_KEY or "").strip(),
            bucket=str(settings.VOLCENGINE_TOS_BUCKET or "").strip(),
            region=str(settings.VOLCENGINE_TOS_REGION or "cn-shanghai").strip(),
        )
    if mode != "local":
        raise ProviderUnavailable(f"不支持的资产存储模式：{mode}")
    return LocalArtifactStorage(settings.local_storage_path(project_root))
