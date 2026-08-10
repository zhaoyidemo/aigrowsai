"""可替换的本地/TOS 资产存储实现。"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path, PurePosixPath

from qijia_video.contracts import AssetRef
from qijia_video.errors import ProviderUnavailable, QualityGateFailed


TOS_SOCKET_TIMEOUT_SECONDS = 300
TOS_CONTROL_TIMEOUT_SECONDS = 20
TOS_DIRECT_UPLOAD_EXPIRES_SECONDS = 15 * 60
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

    async def create_direct_upload(
        self,
        *,
        object_key: str,
        asset_id: str,
        media_type: str,
        sha256: str,
        size_bytes: int,
        expires: int = TOS_DIRECT_UPLOAD_EXPIRES_SECONDS,
    ) -> dict | None:
        del object_key, asset_id, media_type, sha256, size_bytes, expires
        # Local development has no browser-addressable private object store.
        return None

    async def complete_direct_upload(
        self,
        *,
        object_key: str,
        asset_id: str,
        media_type: str,
        sha256: str,
        size_bytes: int,
    ) -> AssetRef:
        del object_key, asset_id, media_type, sha256, size_bytes
        raise ProviderUnavailable("本地存储不支持浏览器直传确认")

    async def delete_object(self, object_key: str) -> None:
        await asyncio.to_thread(self._path(object_key).unlink, missing_ok=True)


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

    def _client(
        self,
        *,
        timeout_seconds: int = TOS_SOCKET_TIMEOUT_SECONDS,
        max_retry_count: int = 3,
    ):
        if not self.configured:
            raise ProviderUnavailable("TOS 配置不完整")
        import tos

        return tos.TosClientV2(
            self.access_key_id,
            self.secret_access_key,
            f"tos-{self.region}.volces.com",
            self.region,
            max_retry_count=max(0, int(max_retry_count)),
            request_timeout=max(1, int(timeout_seconds)),
            socket_timeout=max(1, int(timeout_seconds)),
        )

    def _control_client(self):
        return self._client(
            timeout_seconds=TOS_CONTROL_TIMEOUT_SECONDS,
            max_retry_count=0,
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

    async def create_direct_upload(
        self,
        *,
        object_key: str,
        asset_id: str,
        media_type: str,
        sha256: str,
        size_bytes: int,
        expires: int = TOS_DIRECT_UPLOAD_EXPIRES_SECONDS,
    ) -> dict | None:
        """Issue a short-lived browser PUT without exposing TOS credentials."""

        import tos

        key = _safe_key(object_key)
        lifetime = max(60, min(int(expires), TOS_DIRECT_UPLOAD_EXPIRES_SECONDS))
        headers = {
            "Content-Type": media_type,
            "x-tos-meta-asset-id": asset_id,
            "x-tos-meta-sha256": sha256,
            "x-tos-meta-size-bytes": str(int(size_bytes)),
        }

        def sign():
            return self._client().pre_signed_url(
                tos.HttpMethodType.Http_Method_Put,
                self.bucket,
                key,
                expires=lifetime,
                header=headers,
                is_signed_all_headers=True,
            )

        try:
            signed = await asyncio.to_thread(sign)
        except Exception as exc:
            raise ProviderUnavailable(f"TOS 直传凭证生成失败：{exc}") from exc
        url = str(getattr(signed, "signed_url", "") or "")
        if not url.startswith("https://"):
            raise ProviderUnavailable("TOS 未返回有效的 HTTPS 上传地址")
        signed_headers = {
            str(name): str(value)
            for name, value in dict(
                getattr(signed, "signed_header", {}) or headers
            ).items()
            if str(name).lower() != "host"
        }
        return {
            "url": url,
            "method": "PUT",
            "headers": signed_headers,
            "expires_in_seconds": lifetime,
        }

    async def complete_direct_upload(
        self,
        *,
        object_key: str,
        asset_id: str,
        media_type: str,
        sha256: str,
        size_bytes: int,
    ) -> AssetRef:
        """Verify that the exact signed object reached TOS before queuing work."""

        key = _safe_key(object_key)

        def inspect():
            return self._control_client().head_object(self.bucket, key)

        try:
            output = await asyncio.wait_for(
                asyncio.to_thread(inspect),
                timeout=TOS_CONTROL_TIMEOUT_SECONDS + 5,
            )
        except TimeoutError as exc:
            raise ProviderUnavailable("TOS 上传确认超时，请直接重试确认") from exc
        except Exception as exc:
            raise ProviderUnavailable("TOS 尚未收到完整素材，请重新上传") from exc

        actual_type = str(getattr(output, "content_type", "") or "").split(";", 1)[0]
        actual_size = int(getattr(output, "content_length", -1) or 0)
        metadata = {
            str(name).lower(): str(value)
            for name, value in dict(getattr(output, "meta", {}) or {}).items()
        }
        if actual_size != int(size_bytes):
            raise QualityGateFailed("上传素材大小校验失败，请重新选择文件")
        if actual_type.lower() != str(media_type).lower():
            raise QualityGateFailed("上传素材类型校验失败，请重新选择文件")
        if metadata.get("asset-id") != asset_id:
            raise QualityGateFailed("上传素材身份校验失败，请重新上传")
        if metadata.get("sha256") != sha256:
            raise QualityGateFailed("上传素材完整性信息不一致，请重新上传")
        if metadata.get("size-bytes") != str(int(size_bytes)):
            raise QualityGateFailed("上传素材大小信息不一致，请重新上传")
        return AssetRef(
            asset_id=asset_id,
            object_key=key,
            sha256=sha256,
            size_bytes=int(size_bytes),
            media_type=media_type,
        )

    async def delete_object(self, object_key: str) -> None:
        key = _safe_key(object_key)

        def delete():
            return self._control_client().delete_object(self.bucket, key)

        try:
            await asyncio.wait_for(
                asyncio.to_thread(delete),
                timeout=TOS_CONTROL_TIMEOUT_SECONDS + 5,
            )
        except TimeoutError as exc:
            raise ProviderUnavailable("TOS 临时素材清理超时") from exc
        except Exception as exc:
            raise ProviderUnavailable(f"TOS 临时素材清理失败：{exc}") from exc


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
