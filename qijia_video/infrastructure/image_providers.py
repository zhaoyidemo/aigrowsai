"""First-frame image providers for the independent Qijia video workflow."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from qijia_video.errors import (
    ProviderRequestNotSubmitted,
    ProviderSubmissionUnknown,
    ProviderUnavailable,
)
from qijia_video.ports import GeneratedImage


SEEDREAM_MAX_SEED = (1 << 31) - 1
SEEDREAM_GENERATION_READ_TIMEOUT_SECONDS = 600.0
SEEDREAM_PRE_SUBMIT_ATTEMPTS = 2


class MockImageProvider:
    """Deterministic local provider used only by tests and the explicit CLI demo."""

    name = "mock-first-frame"
    _PNG = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    async def generate(
        self,
        prompt: str,
        *,
        seed: int,
        reference_image_url: str = "",
        reference_image_urls: list[str] | None = None,
    ) -> GeneratedImage:
        references = list(reference_image_urls or [])
        if reference_image_url and reference_image_url not in references:
            references.insert(0, reference_image_url)
        digest = hashlib.sha256(
            f"{seed}:{'|'.join(references)}:{prompt}".encode("utf-8")
        ).hexdigest()
        return GeneratedImage(
            url=f"mock://first-frame/{digest}.png",
            model_id=self.name,
            size="1x1",
        )

    async def download(self, source_url: str, destination: Path) -> Path:
        if not str(source_url).startswith("mock://first-frame/"):
            raise ProviderUnavailable("Mock 首帧不存在")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self._PNG)
        return destination


class SeedreamImageProvider:
    """Volcengine Ark Seedream adapter; paid submissions are never auto-retried."""

    name = "volcengine-seedream"

    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        size: str,
        allowed_download_hosts: tuple[str, ...],
        transport: httpx.AsyncBaseTransport | None = None,
        max_download_bytes: int = 30 * 1024 * 1024,
        generation_read_timeout: float = SEEDREAM_GENERATION_READ_TIMEOUT_SECONDS,
        pre_submit_attempts: int = SEEDREAM_PRE_SUBMIT_ATTEMPTS,
    ):
        self.api_key = str(api_key or "").strip()
        self.model = str(model or "").strip()
        self.base_url = str(base_url or "").rstrip("/")
        self.size = str(size or "").strip()
        self.allowed_download_hosts = tuple(
            str(item or "").strip().lower()
            for item in allowed_download_hosts
            if str(item or "").strip()
        )
        self.transport = transport
        self.max_download_bytes = max(1024 * 1024, int(max_download_bytes))
        self.generation_read_timeout = max(1.0, float(generation_read_timeout))
        self.pre_submit_attempts = max(1, min(3, int(pre_submit_attempts)))
        if not self.model or not self.size or not self.base_url.startswith("https://"):
            raise ProviderUnavailable("Seedream API 配置不完整")
        if not self.allowed_download_hosts:
            raise ProviderUnavailable("Seedream 下载域名白名单不能为空")

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _client(self) -> httpx.AsyncClient:
        if not self.configured:
            raise ProviderUnavailable("真实首帧生成未配置：请设置 ARK_API_KEY")
        return httpx.AsyncClient(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(
                connect=20.0,
                read=self.generation_read_timeout,
                write=60.0,
                pool=20.0,
            ),
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
            f"Seedream API 返回 HTTP {response.status_code}：{message[:500]}{suffix}"
        )

    async def generate(
        self,
        prompt: str,
        *,
        seed: int,
        reference_image_url: str = "",
        reference_image_urls: list[str] | None = None,
    ) -> GeneratedImage:
        seed_value = int(seed)
        if not 0 <= seed_value <= SEEDREAM_MAX_SEED:
            raise ProviderUnavailable(
                "Seedream seed 必须是 0 到 2147483647 之间的 int32"
            )
        body = {
            "model": self.model,
            "prompt": str(prompt).strip(),
            "size": self.size,
            "seed": seed_value,
            "sequential_image_generation": "disabled",
            "response_format": "url",
            "watermark": False,
        }
        references = [
            str(item or '').strip()
            for item in list(reference_image_urls or [])
            if str(item or '').strip()
        ]
        if reference_image_url and reference_image_url not in references:
            references.insert(0, str(reference_image_url))
        if len(references) > 10:
            raise ProviderUnavailable("Seedream 最多接收 10 张参考图")
        if references:
            if any(not item.startswith("https://") for item in references):
                raise ProviderUnavailable("Seedream 参考图必须使用 HTTPS 访问地址")
            body["image"] = references
        async with self._client() as client:
            for attempt in range(1, self.pre_submit_attempts + 1):
                try:
                    response = await client.post("/images/generations", json=body)
                    break
                except (
                    httpx.ConnectTimeout,
                    httpx.ConnectError,
                    httpx.PoolTimeout,
                ) as exc:
                    # These failures happen before an HTTP request can be
                    # submitted. Retrying them cannot duplicate a paid image.
                    if attempt < self.pre_submit_attempts:
                        continue
                    raise ProviderRequestNotSubmitted(
                        "Seedream 无法建立连接，请求未提交"
                        f"（{type(exc).__name__}，已安全尝试 {attempt} 次）；"
                        "不会产生图片生成扣费，可安全重试"
                    ) from exc
                except httpx.RequestError as exc:
                    # Read/write/protocol failures can happen after Ark accepted
                    # the synchronous generation request. ImageGenerations does
                    # not return a queryable task id, so blind resubmission is
                    # intentionally forbidden.
                    timeout_note = (
                        f"，读取等待上限 {self.generation_read_timeout:g} 秒"
                        if isinstance(exc, httpx.ReadTimeout)
                        else ""
                    )
                    raise ProviderSubmissionUnknown(
                        "Seedream 请求已发送但生成结果未返回"
                        f"（{type(exc).__name__}{timeout_note}）；结果状态未知，"
                        "系统不会自动重提。已成功保存的其他图片不会重跑"
                    ) from exc
        if response.status_code >= 400:
            raise self._response_error(response)
        try:
            payload = response.json()
            data = payload.get("data") or []
            item = data[0]
            source_url = str(item.get("url") or "")
            if not source_url:
                raise KeyError("url")
            usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
            return GeneratedImage(
                url=source_url,
                model_id=str(payload.get("model") or self.model),
                size=str(item.get("size") or self.size),
                usage_total_tokens=max(0, int(usage.get("total_tokens") or 0)),
            )
        except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
            request_id = response.headers.get("x-request-id", "")
            suffix = f"；request_id={request_id}" if request_id else ""
            raise ProviderSubmissionUnknown(
                "Seedream 已返回成功 HTTP，但响应缺少可下载图片；"
                f"结果状态未知，系统不会自动重提{suffix}"
            ) from exc

    def _download_url_allowed(self, value: str) -> bool:
        parsed = urlparse(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        return any(
            host == suffix.lstrip(".") or host.endswith(suffix)
            for suffix in self.allowed_download_hosts
        )

    async def download(self, source_url: str, destination: Path) -> Path:
        if not self._download_url_allowed(source_url):
            raise ProviderUnavailable("Seedream 返回了不在白名单中的下载地址")
        destination.parent.mkdir(parents=True, exist_ok=True)
        current_url = source_url
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=20.0),
                transport=self.transport,
                follow_redirects=False,
            ) as client:
                for redirect_count in range(4):
                    async with client.stream("GET", current_url) as response:
                        if response.status_code in (301, 302, 303, 307, 308):
                            location = response.headers.get("location", "")
                            next_url = urljoin(current_url, location)
                            if not location or not self._download_url_allowed(next_url):
                                raise ProviderUnavailable(
                                    "Seedream 下载重定向不在白名单中"
                                )
                            current_url = next_url
                            if redirect_count == 3:
                                raise ProviderUnavailable("Seedream 下载重定向次数过多")
                            continue
                        if response.status_code >= 400:
                            raise self._response_error(response)
                        content_type = response.headers.get("content-type", "").lower()
                        if content_type and not content_type.startswith("image/"):
                            raise ProviderUnavailable("Seedream 下载内容不是图片")
                        try:
                            declared = int(response.headers.get("content-length") or 0)
                        except ValueError:
                            declared = 0
                        if declared > self.max_download_bytes:
                            raise ProviderUnavailable("Seedream 图片超过下载大小上限")
                        written = 0
                        with destination.open("wb") as handle:
                            async for chunk in response.aiter_bytes():
                                written += len(chunk)
                                if written > self.max_download_bytes:
                                    raise ProviderUnavailable(
                                        "Seedream 图片超过下载大小上限"
                                    )
                                handle.write(chunk)
                        break
        except ProviderUnavailable:
            destination.unlink(missing_ok=True)
            raise
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            destination.unlink(missing_ok=True)
            raise ProviderUnavailable("下载 Seedream 图片失败，可安全重试下载") from exc
        if not destination.is_file() or destination.stat().st_size == 0:
            raise ProviderUnavailable("Seedream 下载结果为空")
        return destination
