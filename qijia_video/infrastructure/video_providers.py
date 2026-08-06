"""单镜头视频 Provider；长任务语义不泄漏到领域服务之外。"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from qijia_video.contracts import (
    ProviderTask,
    ProviderTaskState,
    VisualGenerationRequest,
)
from qijia_video.errors import ProviderUnavailable


def _provider_state(value: str) -> ProviderTaskState:
    normalized = str(value or "").strip().lower()
    return {
        "queued": ProviderTaskState.QUEUED,
        "pending": ProviderTaskState.QUEUED,
        "running": ProviderTaskState.RUNNING,
        "processing": ProviderTaskState.RUNNING,
        "succeeded": ProviderTaskState.SUCCEEDED,
        "completed": ProviderTaskState.SUCCEEDED,
        "failed": ProviderTaskState.FAILED,
        "cancelled": ProviderTaskState.CANCELLED,
        "canceled": ProviderTaskState.CANCELLED,
    }.get(normalized, ProviderTaskState.UNKNOWN)


class MockVideoProvider:
    """确定性测试 Provider，不伪装成 Seedance，也不发生外部费用。"""

    name = "mock-video"

    def __init__(self):
        self._requests: dict[str, VisualGenerationRequest] = {}

    async def submit(
        self,
        request: VisualGenerationRequest,
        *,
        first_frame_url: str = "",
    ) -> ProviderTask:
        del first_frame_url
        task_id = f"mock-{request.fingerprint()[:20]}"
        self._requests[task_id] = request
        return ProviderTask(
            provider=self.name,
            provider_task_id=task_id,
            request_fingerprint=request.fingerprint(),
            model_id=request.model_id,
            state=ProviderTaskState.SUCCEEDED,
            output_url=f"mock://{task_id}",
            raw_status="succeeded",
        )

    async def get_status(
        self, provider_task_id: str, request_fingerprint: str
    ) -> ProviderTask:
        request = self._requests.get(provider_task_id)
        if not request or request.fingerprint() != request_fingerprint:
            return ProviderTask(
                provider=self.name,
                provider_task_id=provider_task_id,
                request_fingerprint=request_fingerprint,
                state=ProviderTaskState.UNKNOWN,
                error_message="测试任务不存在或请求指纹不匹配",
                raw_status="unknown",
            )
        return await self.submit(request)

    async def download(self, provider_task_id: str, destination: Path) -> Path:
        request = self._requests.get(provider_task_id)
        if not request:
            raise ProviderUnavailable("Mock 视频任务不存在")
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ProviderUnavailable("Mock 视频生成缺少 FFmpeg")
        destination.parent.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-y",
            "-f", "lavfi",
            "-i", (
                "color=c=0x1e293b:s=720x1280:r=24:"
                f"d={request.duration_seconds}"
            ),
            "-an",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            str(destination),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode != 0 or not destination.is_file():
            raise ProviderUnavailable(
                "Mock 视频生成失败：" + output.decode("utf-8", errors="replace")[-2000:]
            )
        return destination

    async def cancel(
        self, provider_task_id: str, request_fingerprint: str
    ) -> ProviderTask:
        self._requests.pop(provider_task_id, None)
        return ProviderTask(
            provider=self.name,
            provider_task_id=provider_task_id,
            request_fingerprint=request_fingerprint,
            state=ProviderTaskState.CANCELLED,
            raw_status="cancelled",
        )


class SeedanceVideoProvider:
    """火山方舟 Seedance API 适配器；submit 刻意不自动重试。"""

    name = "volcengine-seedance"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        allowed_download_hosts: tuple[str, ...],
        transport: httpx.AsyncBaseTransport | None = None,
        max_download_bytes: int = 250 * 1024 * 1024,
    ):
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.base_url = str(base_url or "").rstrip("/")
        self.allowed_download_hosts = tuple(
            str(item or "").strip().lower()
            for item in allowed_download_hosts
            if str(item or "").strip()
        )
        self.transport = transport
        self.max_download_bytes = max(1024 * 1024, int(max_download_bytes))
        if not self.model or not self.base_url.startswith("https://"):
            raise ProviderUnavailable("Seedance API 配置不完整")
        if not self.allowed_download_hosts:
            raise ProviderUnavailable("Seedance 下载域名白名单不能为空")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _client(self, *, timeout: float = 60.0) -> httpx.AsyncClient:
        if not self.configured:
            raise ProviderUnavailable("真实视频生成未配置：请设置 ARK_API_KEY")
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout, connect=20.0),
            transport=self.transport,
            follow_redirects=False,
        )

    @staticmethod
    def _response_error(response: httpx.Response) -> ProviderUnavailable:
        request_id = response.headers.get("x-request-id", "")
        try:
            payload = response.json()
            message = str(
                payload.get("error", {}).get("message")
                or payload.get("message")
                or response.reason_phrase
            )
        except (ValueError, TypeError, AttributeError):
            message = response.reason_phrase
        suffix = f"；request_id={request_id}" if request_id else ""
        return ProviderUnavailable(
            f"Seedance API 返回 HTTP {response.status_code}：{message[:500]}{suffix}"
        )

    def _parse_task(
        self,
        payload: dict,
        fingerprint: str,
        *,
        fallback_model: str = "",
    ) -> ProviderTask:
        task_id = str(payload.get("id") or payload.get("task_id") or "")
        if not task_id:
            raise ProviderUnavailable("Seedance 响应缺少任务 ID")
        raw_status = str(payload.get("status") or "queued")
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        output = payload.get("output") if isinstance(payload.get("output"), dict) else {}
        error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        try:
            usage_total_tokens = max(
                0,
                int(usage.get("total_tokens") or usage.get("completion_tokens") or 0),
            )
        except (TypeError, ValueError):
            usage_total_tokens = 0
        return ProviderTask(
            provider=self.name,
            provider_task_id=task_id,
            request_fingerprint=fingerprint,
            model_id=str(payload.get("model") or fallback_model or self.model),
            state=_provider_state(raw_status),
            output_url=str(content.get("video_url") or output.get("video_url") or ""),
            error_code=str(error.get("code") or payload.get("error_code") or ""),
            error_message=str(error.get("message") or payload.get("error_message") or ""),
            raw_status=raw_status,
            usage_total_tokens=usage_total_tokens,
        )

    async def submit(
        self,
        request: VisualGenerationRequest,
        *,
        first_frame_url: str = "",
    ) -> ProviderTask:
        content = [{"type": "text", "text": request.prompt}]
        if request.first_frame_asset_id:
            if not first_frame_url:
                raise ProviderUnavailable("Seedance 首帧资产缺少可访问地址")
            parsed = urlparse(first_frame_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ProviderUnavailable("Seedance 首帧必须使用可公开读取的 HTTPS 地址")
            content.append({
                "type": "image_url",
                "image_url": {"url": first_frame_url},
                "role": "first_frame",
            })
        requested_model = request.model_id or self.model
        body = {
            "model": requested_model,
            "content": content,
            "resolution": request.resolution,
            "ratio": request.ratio,
            "duration": request.duration_seconds,
            "generate_audio": request.generate_audio,
            "watermark": False,
        }
        if request.seed is not None:
            body["seed"] = request.seed
        async with self._client() as client:
            try:
                response = await client.post("/contents/generations/tasks", json=body)
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                raise ProviderUnavailable(
                    "Seedance 提交结果未知；为避免重复扣费，禁止自动重提"
                ) from exc
        if response.status_code >= 400:
            raise self._response_error(response)
        return self._parse_task(
            response.json(),
            request.fingerprint(),
            fallback_model=requested_model,
        )

    async def get_status(
        self, provider_task_id: str, request_fingerprint: str
    ) -> ProviderTask:
        async with self._client() as client:
            try:
                response = await client.get(
                    f"/contents/generations/tasks/{provider_task_id}"
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                raise ProviderUnavailable("查询 Seedance 任务失败，可安全重试查询") from exc
        if response.status_code >= 400:
            raise self._response_error(response)
        return self._parse_task(response.json(), request_fingerprint)

    def _download_url_allowed(self, value: str) -> bool:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        return any(
            host == suffix.lstrip(".") or host.endswith(suffix)
            for suffix in self.allowed_download_hosts
        )

    async def download(self, provider_task_id: str, destination: Path) -> Path:
        async with self._client() as client:
            status_response = await client.get(
                f"/contents/generations/tasks/{provider_task_id}"
            )
            if status_response.status_code >= 400:
                raise self._response_error(status_response)
            payload = status_response.json()
            task = self._parse_task(payload, "0" * 64)
            if task.state != ProviderTaskState.SUCCEEDED or not task.output_url:
                raise ProviderUnavailable("Seedance 任务尚未成功，不能下载")
            if not self._download_url_allowed(task.output_url):
                raise ProviderUnavailable("Seedance 返回了不在白名单中的下载地址")
            destination.parent.mkdir(parents=True, exist_ok=True)
        # 下载使用无方舟 Authorization 的独立客户端，避免把 API Key
        # 带到对象存储域名；每一次重定向仍必须命中白名单。
        current_url = task.output_url
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=20.0),
                transport=self.transport,
                follow_redirects=False,
            ) as download_client:
                for redirect_count in range(4):
                    async with download_client.stream("GET", current_url) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location", "")
                            next_url = urljoin(current_url, location)
                            if not location or not self._download_url_allowed(next_url):
                                raise ProviderUnavailable(
                                    "Seedance 下载重定向不在白名单中"
                                )
                            current_url = next_url
                            if redirect_count == 3:
                                raise ProviderUnavailable("Seedance 下载重定向次数过多")
                            continue
                        if response.status_code >= 400:
                            raise self._response_error(response)
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type and not content_type.startswith("video/"):
                            raise ProviderUnavailable("Seedance 下载内容不是视频")
                        declared = int(response.headers.get("content-length") or 0)
                        if declared > self.max_download_bytes:
                            raise ProviderUnavailable("Seedance 视频超过下载大小上限")
                        written = 0
                        with destination.open("wb") as handle:
                            async for chunk in response.aiter_bytes():
                                written += len(chunk)
                                if written > self.max_download_bytes:
                                    raise ProviderUnavailable(
                                        "Seedance 视频超过下载大小上限"
                                    )
                                handle.write(chunk)
                        break
        except ProviderUnavailable:
            destination.unlink(missing_ok=True)
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            destination.unlink(missing_ok=True)
            raise ProviderUnavailable("下载 Seedance 视频失败，可安全重试下载") from exc
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProviderUnavailable("Seedance 下载结果为空")
        return destination

    async def cancel(
        self, provider_task_id: str, request_fingerprint: str
    ) -> ProviderTask:
        async with self._client() as client:
            try:
                response = await client.delete(
                    f"/contents/generations/tasks/{provider_task_id}"
                )
            except (httpx.TimeoutException, httpx.RequestError) as exc:
                raise ProviderUnavailable("取消 Seedance 任务失败") from exc
        if response.status_code >= 400:
            raise self._response_error(response)
        if response.content:
            return self._parse_task(response.json(), request_fingerprint)
        return ProviderTask(
            provider=self.name,
            provider_task_id=provider_task_id,
            request_fingerprint=request_fingerprint,
            state=ProviderTaskState.CANCELLED,
            raw_status="cancelled",
        )
