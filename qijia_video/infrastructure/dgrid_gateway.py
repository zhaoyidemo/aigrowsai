"""Small, provider-specific helpers for DGrid's OpenAI-compatible gateway."""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlparse

import httpx


DGRID_DEFAULT_BASE_URL = "https://api.dgrid.ai/v1"
DGRID_BILLING_POLL_DELAYS_SECONDS = (0.0, 1.0, 2.0, 4.0)


def dgrid_headers(api_key: str, *, title: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": title,
    }


def dgrid_request_id(response: httpx.Response) -> str:
    """Return the billing-reconcilable request ID documented by DGrid."""

    return str(
        response.headers.get("DGrid-Request-ID")
        or response.headers.get("x-request-id")
        or ""
    ).strip()


def dgrid_billing_url(base_url: str) -> str:
    parsed = urlparse(str(base_url or DGRID_DEFAULT_BASE_URL))
    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else "https://api.dgrid.ai"
    )
    return origin + "/api/v1/model-router/billing-json"


async def dgrid_billing_snapshot(
    *,
    api_key: str,
    base_url: str = DGRID_DEFAULT_BASE_URL,
    request_id: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> dict[str, Any] | None:
    """Fetch DGrid's immutable billing snapshot for one completed request."""

    if not api_key or not request_id:
        return None
    timeout = min(5.0, max(2.0, float(timeout_seconds)))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout, connect=min(3.0, timeout)),
        transport=transport,
    ) as client:
        for delay in DGRID_BILLING_POLL_DELAYS_SECONDS:
            if delay:
                await asyncio.sleep(delay)
            try:
                response = await client.get(
                    dgrid_billing_url(base_url),
                    headers={"Authorization": f"Bearer {api_key}"},
                    params={"request_id": request_id},
                )
            except (httpx.TimeoutException, httpx.RequestError):
                return None
            if response.status_code not in (200, 404):
                return None
            try:
                payload = response.json()
            except ValueError:
                return None
            data = payload.get("data") if isinstance(payload, dict) else None
            billing = (
                data.get("billing_json") if isinstance(data, dict) else None
            )
            if isinstance(billing, dict):
                return billing
            response_code = (
                payload.get("code") if isinstance(payload, dict) else None
            )
            if response.status_code != 404 and response_code not in (404, "404"):
                return None
    return None


async def dgrid_model_access(
    *,
    api_key: str,
    models_url: str,
    model: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> str:
    """Check the current key's permission-aware model catalog after failure."""

    timeout = min(15.0, max(5.0, float(timeout_seconds)))
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            transport=transport,
        ) as client:
            response = await client.get(
                models_url,
                headers={"Authorization": f"Bearer {api_key}"},
            )
    except (httpx.TimeoutException, httpx.RequestError):
        return "probe_unavailable"
    if response.status_code != 200:
        return f"probe_http_{response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return "probe_invalid_response"
    raw_models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(raw_models, list):
        return "probe_invalid_response"
    available_ids = {
        str(item.get("id") or "").strip()
        for item in raw_models
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    return "listed_for_key" if model in available_ids else "not_listed_for_key"
