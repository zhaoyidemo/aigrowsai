"""真实脚本生成 Provider；只依赖 OpenRouter 的 OpenAI 兼容接口。"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from pydantic import ValidationError

from qijia_video.contracts import (
    NewsResearchBrief,
    PersonResearchBrief,
    ProviderUsageRecord,
    ScriptDraft,
    ScriptReview,
    SourceCard,
    StoryboardPlan,
    StoryboardShot,
    content_hash,
    timestamp,
)
from qijia_video.errors import ProviderUnavailable, ResearchEvidenceUnavailable
from qijia_video.prompts import (
    DEFAULT_SCRIPT_PROMPT,
    SCRIPT_OUTPUT_CONTRACT,
    narration_char_count,
)


SCRIPT_PROMPT_VERSION = "qijia_script_v13_narrative_progression"
STORYBOARD_PROMPT_VERSION = "qijia_storyboard_v11_h3_orchestrated"
PERSON_RESEARCH_PROMPT_VERSION = "qijia_person_research_v1"
NEWS_RESEARCH_PROMPT_VERSION = "recent_news_research_v5"
OPENROUTER_REASONING_EFFORT = "high"
SCRIPT_MAX_COMPLETION_TOKENS = 48_000
PERSON_RESEARCH_MAX_COMPLETION_TOKENS = 48_000
NEWS_RESEARCH_MAX_COMPLETION_TOKENS = 48_000
STORYBOARD_MAX_COMPLETION_TOKENS = 128_000
UsageRecorder = Callable[[ProviderUsageRecord], Awaitable[None]]


_SCRIPT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string", "enum": ["2.0"]},
        "video_title": {"type": "string"},
        "cover_text": {"type": "string"},
        "hook": {"type": "string"},
        "closing": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {
            "type": "array",
            "items": {"type": "string"},
        },
        "beats": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "role": {
                        "type": "string",
                        "enum": [
                            "hook",
                            "suspense",
                            "context",
                            "reframe",
                            "explanation",
                            "example",
                            "application",
                            "closing",
                        ],
                    },
                    "narration": {"type": "string"},
                    "visual_direction": {"type": "string"},
                    "on_screen_text": {"type": "string"},
                    "source_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "quote_ref": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "null"},
                        ]
                    },
                },
                "required": [
                    "id",
                    "role",
                    "narration",
                    "visual_direction",
                    "on_screen_text",
                    "source_refs",
                    "quote_ref",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "schema_version",
        "video_title",
        "cover_text",
        "hook",
        "closing",
        "caption",
        "hashtags",
        "beats",
    ],
    "additionalProperties": False,
}

_PERSON_RESEARCH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "core_tension": {"type": "string"},
        "audience_relevance": {
            "type": "array",
            "items": {"type": "string"},
        },
        "content_angles": {
            "type": "array",
            "items": {"type": "string"},
        },
        "interaction_opportunity": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {"type": "string"},
                    "source_title": {"type": "string"},
                    "source_url": {"type": "string"},
                },
                "required": ["claim", "source_title", "source_url"],
                "additionalProperties": False,
            },
        },
        "uncertainties": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "summary",
        "core_tension",
        "audience_relevance",
        "content_angles",
        "interaction_opportunity",
        "evidence",
        "uncertainties",
    ],
    "additionalProperties": False,
}

_NEWS_RESEARCH_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "core_tension": {"type": "string"},
        "audience_relevance": {
            "type": "array",
            "items": {"type": "string"},
        },
        "content_angles": {
            "type": "array",
            "items": {"type": "string"},
        },
        "interaction_opportunity": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "claim": {
                        "type": "string",
                        "description": (
                            "一条非空、由 source_url 页面直接支持的事实描述；"
                            "无法确认时不要输出该 evidence"
                        ),
                    },
                    "source_title": {"type": "string"},
                    "source_url": {
                        "type": "string",
                        "description": "必须原样复制本次联网检索结果中的 URL",
                    },
                    "source_kind": {
                        "type": "string",
                        "enum": [
                            "official",
                            "primary",
                            "independent",
                            "other",
                        ],
                    },
                    "published_at": {"type": "string"},
                    "event_at": {"type": "string"},
                },
                "required": [
                    "claim",
                    "source_title",
                    "source_url",
                    "source_kind",
                    "published_at",
                    "event_at",
                ],
                "additionalProperties": False,
            },
        },
        "uncertainties": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "summary",
        "core_tension",
        "audience_relevance",
        "content_angles",
        "interaction_opportunity",
        "evidence",
        "uncertainties",
    ],
    "additionalProperties": False,
}

_STORYBOARD_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "shots": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "segment_id": {"type": "string"},
                    "beat_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "visual_intent": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 600,
                        "description": (
                            "一句内容语义目标，只写必须看懂的主体、关系、"
                            "变化或结果，不写风格、构图、运镜或方法说明"
                        ),
                    },
                    "first_frame_prompt": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1800,
                        "description": (
                            "可直接提交图片模型的自包含最终首帧提示词，"
                            "完整落实统一基础规格，不复述方法说明"
                        ),
                    },
                    "motion_prompt": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 1800,
                        "description": (
                            "video 写可直接提交 I2V 的最终动作提示词；"
                            "image 只写 Remotion 后期取景方向"
                        ),
                    },
                },
                "required": [
                    "segment_id",
                    "beat_ids",
                    "visual_intent",
                    "first_frame_prompt",
                    "motion_prompt",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["shots"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class _OpenRouterJsonResponse:
    data: dict
    message: dict
    model_id: str
    web_search_requests: int | None = None


_STORYBOARD_FALLBACKS = (
    {
        "role": "关键变化钩子",
        "first_frame_prompt": (
            "核心主体正处于关键动作或状态变化的起点，中景构图，前中后景关系清楚，"
            "底部留出字幕安全区，画面中无文字"
        ),
        "motion_prompt": (
            "开场动作从第一帧立即发生，镜头克制推近，在前两秒内让核心变化清楚"
        ),
    },
    {
        "role": "主体细节",
        "first_frame_prompt": (
            "延续同一主体、空间、材质和光线，近景聚焦能够解释变化的动作或物件细节，"
            "层次清楚，画面中无文字"
        ),
        "motion_prompt": "从整体关系缓慢推进到关键细节，保持克制、可信的运动",
    },
    {
        "role": "关系与机制",
        "first_frame_prompt": (
            "延续同一视觉空间，用主体、环境和关键物件的前中后景关系呈现变化机制，"
            "构图简洁、因果关系可理解，画面中无文字"
        ),
        "motion_prompt": "镜头在相关主体之间缓慢横移，以轻微景深变化揭示信息关系",
    },
    {
        "role": "影响展开",
        "first_frame_prompt": (
            "延续同一主体与视觉锚点，清楚展示变化发生后的下一步动作或影响，"
            "中景构图，状态差异可见，画面中无文字"
        ),
        "motion_prompt": (
            "主体完成一个明确动作，镜头轻缓跟随并停在新的可观察状态"
        ),
    },
    {
        "role": "结果与观察",
        "first_frame_prompt": (
            "回到贯穿全片的核心主体、空间或物件，呈现可观察的结果与仍待关注的信号，"
            "构图有余韵，底部留出字幕安全区，画面中无文字"
        ),
        "motion_prompt": (
            "完成最后一个自然动作后缓慢拉远，让结果与后续观察空间同时可见"
        ),
    },
)


def _storyboard_text(value: Any, fallback: str, max_length: int) -> str:
    text = value.strip() if isinstance(value, str) else ""
    return (text or fallback)[:max_length]


def _normalize_storyboard_rows(
    raw_shots: Any,
    target_segments: list[Any],
) -> list[dict[str, str]]:
    """Align imperfect model output to ordered shots without another API call."""

    candidates = (
        [item for item in raw_shots if isinstance(item, dict)]
        if isinstance(raw_shots, list)
        else []
    )
    exact: dict[str, list[tuple[int, dict]]] = {}
    expected_segment_ids = {segment.id for segment in target_segments}
    for position, candidate in enumerate(candidates):
        segment_id = str(candidate.get("segment_id") or "").strip()
        if segment_id:
            exact.setdefault(segment_id, []).append((position, candidate))

    used_positions: set[int] = set()
    normalized: list[dict[str, str]] = []
    for index, segment in enumerate(target_segments):
        position = -1
        raw: dict = {}
        if index < len(candidates):
            candidate_id = str(
                candidates[index].get("segment_id") or ""
            ).strip()
            if candidate_id in ("", segment.id) and index not in used_positions:
                position = index
                raw = candidates[index]
        if position < 0:
            selected = next(
                (
                    item for item in exact.get(segment.id, [])
                    if item[0] not in used_positions
                ),
                None,
            )
            if selected:
                position, raw = selected
        if position < 0:
            position = next(
                (
                    item for item in range(len(candidates))
                    if item not in used_positions
                    and (
                        not str(
                            candidates[item].get("segment_id") or ""
                        ).strip()
                        or str(
                            candidates[item].get("segment_id") or ""
                        ).strip() not in expected_segment_ids
                    )
                ),
                -1,
            )
            raw = candidates[position] if position >= 0 else {}
        if position >= 0:
            used_positions.add(position)

        fallback_index = round(
            index * (len(_STORYBOARD_FALLBACKS) - 1)
            / max(1, len(target_segments) - 1)
        )
        fallback = _STORYBOARD_FALLBACKS[fallback_index]
        segment_direction = str(
            getattr(segment, "visual_direction", "") or ""
        ).strip()
        semantic_intent = (
            f"{fallback['role']}："
            + (
                segment_direction
                or "用连续视觉叙事承载本段信息变化"
            )
        )
        fallback_frame = (
            f"{segment_direction}；{fallback['first_frame_prompt']}"
            if segment_direction
            else fallback["first_frame_prompt"]
        )
        normalized.append({
            "segment_id": segment.id,
            "visual_intent": _storyboard_text(
                raw.get("visual_intent"), semantic_intent, 600
            ),
            "first_frame_prompt": _storyboard_text(
                raw.get("first_frame_prompt"),
                fallback_frame,
                1800,
            ),
            "motion_prompt": _storyboard_text(
                raw.get("motion_prompt"), fallback["motion_prompt"], 1800
            ),
        })
    return normalized


def _beat_groups_cover_script(
    beat_groups: list[list[str]],
    expected_beat_ids: list[str],
) -> bool:
    flat_ids = [beat_id for group in beat_groups for beat_id in group]
    if flat_ids == expected_beat_ids:
        return True
    if any(len(group) != 1 for group in beat_groups):
        return False
    compressed_ids = [
        beat_id
        for index, beat_id in enumerate(flat_ids)
        if index == 0 or beat_id != flat_ids[index - 1]
    ]
    return compressed_ids == expected_beat_ids


def _chat_url(base_url: str) -> str:
    base = str(base_url or "https://openrouter.ai/api").rstrip("/")
    if base.endswith("/v1"):
        return base + "/chat/completions"
    if base.endswith("/api"):
        return base + "/v1/chat/completions"
    return base + "/api/v1/chat/completions"


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "")
            for item in content
            if isinstance(item, dict)
        )
    return str(content or "")


def _json_object(content: Any) -> dict:
    if isinstance(content, dict):
        return content
    value = _message_text(content).strip()
    if value.startswith("```"):
        lines = value.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        value = "\n".join(lines).strip()
    # A few OpenAI-compatible gateways double-encode structured content as a
    # JSON string. Decode at most twice without trying to guess malformed data.
    for _ in range(2):
        try:
            decoded = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            break
        if isinstance(decoded, dict):
            return decoded
        if not isinstance(decoded, str):
            break
        value = decoded.strip()
    decoder = json.JSONDecoder()
    for index, character in enumerate(value):
        if character != "{":
            continue
        try:
            decoded, _ = decoder.raw_decode(value[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            return decoded
    raise ProviderUnavailable("模型没有返回有效 JSON")


def _normalized_citation_url(value: Any) -> str:
    text = str(value or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return ""
    path = parts.path.rstrip("/") or "/"
    query = urlencode([
        (key, item)
        for key, item in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith("utm_")
        and key.lower() not in {
            "fbclid",
            "gclid",
            "mc_cid",
            "mc_eid",
            "msclkid",
        }
    ])
    return urlunsplit((
        parts.scheme.lower(),
        parts.netloc.lower(),
        path,
        query,
        "",
    ))


def _citation_identity(value: Any) -> tuple[str, str]:
    """Match one canonical article when only tracking/query details differ."""

    normalized = _normalized_citation_url(value)
    if not normalized:
        return "", ""
    parts = urlsplit(normalized)
    host = (parts.hostname or "").lower().removeprefix("www.")
    try:
        port = parts.port
    except ValueError:
        return "", ""
    if port:
        host = f"{host}:{port}"
    return host, parts.path.rstrip("/") or "/"


def _citation_catalog(message: dict) -> dict[str, dict[str, str]]:
    catalog: dict[str, dict[str, str]] = {}
    for annotation in message.get("annotations") or []:
        if not isinstance(annotation, dict):
            continue
        citation = annotation.get("url_citation")
        if not isinstance(citation, dict):
            citation = annotation
        url = str(citation.get("url") or "").strip()
        normalized = _normalized_citation_url(url)
        if not normalized:
            continue
        existing = catalog.get(normalized, {})
        title = str(citation.get("title") or "").strip()
        content = _message_text(citation.get("content")).strip()
        catalog[normalized] = {
            "url": str(existing.get("url") or url),
            "title": title or str(existing.get("title") or ""),
            "content": content or str(existing.get("content") or ""),
        }
    return catalog


def _matched_citation(
    catalog: dict[str, dict[str, str]],
    source_url: Any,
) -> dict[str, str] | None:
    normalized = _normalized_citation_url(source_url)
    exact = catalog.get(normalized)
    if exact:
        return exact
    identity = _citation_identity(source_url)
    if not all(identity):
        return None
    candidates = {
        item["url"]: item
        for item in catalog.values()
        if _citation_identity(item.get("url")) == identity
    }
    return next(iter(candidates.values())) if len(candidates) == 1 else None


def _citation_excerpt(value: Any) -> str:
    """Return a bounded source excerpt suitable for an evidence claim."""

    text = _message_text(value).strip()
    if not text:
        return ""
    text = re.sub(r"\[\s*(?:\.\.\.|…)\s*\]", " ", text)
    text = " ".join(text.split()).strip()
    if not text or _normalized_citation_url(text):
        return ""
    return text[:1200].rstrip()


def _citation_identity_label(value: Any) -> str:
    """Expose only host/path for bounded diagnostics, never query strings."""

    host, path = _citation_identity(value)
    return f"{host}{path}"[:500] if host and path else ""


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _web_search_request_count(body: dict | None) -> int | None:
    payload = body if isinstance(body, dict) else {}
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    server_tool_use = usage.get("server_tool_use")
    server_tool_use = (
        server_tool_use if isinstance(server_tool_use, dict) else {}
    )
    value = server_tool_use.get("web_search_requests")
    if value is None:
        return None
    return _nonnegative_int(value)


def _openrouter_usage_record(
    body: dict | None,
    *,
    usage_id: str,
    operation: str,
    fallback_model: str,
    request_id: str = "",
    http_status_code: int | None = None,
    succeeded: bool = False,
    note: str = "",
) -> ProviderUsageRecord:
    payload = body if isinstance(body, dict) else {}
    usage = payload.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    prompt_details = usage.get("prompt_tokens_details")
    prompt_details = prompt_details if isinstance(prompt_details, dict) else {}
    completion_details = usage.get("completion_tokens_details")
    completion_details = (
        completion_details if isinstance(completion_details, dict) else {}
    )
    web_search_requests = _web_search_request_count(payload)
    raw_cost = usage.get("cost")
    try:
        reported_cost = max(0.0, float(raw_cost)) if raw_cost is not None else None
    except (TypeError, ValueError):
        reported_cost = None
    input_tokens = _nonnegative_int(
        usage.get("prompt_tokens") or usage.get("input_tokens")
    )
    output_tokens = _nonnegative_int(
        usage.get("completion_tokens") or usage.get("output_tokens")
    )
    total_tokens = _nonnegative_int(
        usage.get("total_tokens") or input_tokens + output_tokens
    )
    missing_cost_note = (
        "供应商响应未提供 usage.cost，金额需与 OpenRouter Activity 对账"
        if reported_cost is None
        else ""
    )
    return ProviderUsageRecord(
        usage_id=usage_id,
        operation=operation,
        provider="openrouter",
        model_id=str(payload.get("model") or fallback_model),
        request_id=str(payload.get("id") or request_id),
        succeeded=bool(succeeded),
        http_status_code=http_status_code,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=_nonnegative_int(prompt_details.get("cached_tokens")),
        reasoning_tokens=_nonnegative_int(
            completion_details.get("reasoning_tokens")
        ),
        quantity=1,
        unit="request",
        reported_cost=reported_cost,
        reported_currency="USD" if reported_cost is not None else None,
        pricing_basis=(
            "OpenRouter 非流式响应 usage.cost 供应商回传金额"
            if reported_cost is not None
            else ""
        ),
        note="；".join(
            item
            for item in (
                note,
                (
                    f"联网检索 {web_search_requests} 次"
                    if web_search_requests is not None
                    else ""
                ),
                missing_cost_note,
            )
            if item
        ),
        occurred_at=timestamp(),
    )


async def _record_usage(
    recorder: UsageRecorder | None,
    usage: ProviderUsageRecord,
) -> None:
    if not recorder:
        return
    try:
        await recorder(usage.model_copy(deep=True))
    except Exception as exc:
        raise ProviderUnavailable(
            "模型调用已经发生，但成本账本无法持久化；流程已停止"
        ) from exc


async def _openrouter_json_request(
    *,
    api_key: str,
    base_url: str,
    model: str,
    messages: list[dict],
    label: str,
    schema_name: str,
    response_schema: dict,
    max_completion_tokens: int,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
    operation: str,
    on_usage: UsageRecorder | None = None,
    reasoning_effort: str = OPENROUTER_REASONING_EFFORT,
    tools: list[dict] | None = None,
    tool_choice: str | dict | None = None,
    max_tool_calls: int | None = None,
) -> _OpenRouterJsonResponse:
    usage_id = f"usage_{uuid.uuid4().hex}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Qijia AI Video Workbench",
    }
    payload = {
        "model": model,
        "messages": messages,
        # Reasoning tokens share this ceiling with the visible answer. OpenRouter's
        # Grok endpoints currently advertise `max_tokens`, and `require_parameters`
        # rejects the otherwise preferred `max_completion_tokens` before routing.
        "reasoning": {"effort": reasoning_effort, "exclude": True},
        "max_tokens": max_completion_tokens,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": response_schema,
            },
        },
        "plugins": [{"id": "response-healing"}],
        "provider": {"require_parameters": True},
    }
    if tools:
        payload["tools"] = tools
    if tools and tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if max_tool_calls is not None:
        payload["max_tool_calls"] = max(1, int(max_tool_calls))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=20.0),
        transport=transport,
    ) as client:
        try:
            response = await client.post(
                _chat_url(base_url), headers=headers, json=payload
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            await _record_usage(on_usage, _openrouter_usage_record(
                None,
                usage_id=usage_id,
                operation=operation,
                fallback_model=model,
                succeeded=False,
                note="网络异常后是否计费未知",
            ))
            raise ProviderUnavailable(f"OpenRouter {label}请求失败") from exc
    request_id = response.headers.get("x-request-id", "")
    try:
        body = response.json()
    except ValueError as exc:
        await _record_usage(on_usage, _openrouter_usage_record(
            None,
            usage_id=usage_id,
            operation=operation,
            fallback_model=model,
            request_id=request_id,
            http_status_code=response.status_code,
            succeeded=False,
            note="响应无法解析，是否计费需对账",
        ))
        raise ProviderUnavailable(
            f"OpenRouter {label}返回了无法读取的响应"
            + (f"；request_id={request_id}" if request_id else "")
        ) from exc
    response_succeeded = bool(
        response.status_code < 400
        and isinstance(body, dict)
        and not body.get("error")
        and body.get("choices")
    )
    await _record_usage(on_usage, _openrouter_usage_record(
        body if isinstance(body, dict) else None,
        usage_id=usage_id,
        operation=operation,
        fallback_model=model,
        request_id=request_id,
        http_status_code=response.status_code,
        succeeded=response_succeeded,
    ))
    if response.status_code >= 400:
        try:
            message = str(
                body.get("error", {}).get("message")
                or body.get("message")
                or response.reason_phrase
            )
        except (TypeError, AttributeError):
            message = response.reason_phrase
        suffix = f"；request_id={request_id}" if request_id else ""
        raise ProviderUnavailable(
            f"OpenRouter {label}返回 HTTP {response.status_code}："
            f"{message[:500]}{suffix}"
        )
    top_level_error = body.get("error") if isinstance(body, dict) else None
    if top_level_error:
        message = (
            top_level_error.get("message")
            if isinstance(top_level_error, dict)
            else str(top_level_error)
        )
        raise ProviderUnavailable(
            f"OpenRouter {label}生成失败：{str(message or '未知上游错误')[:500]}"
            + (f"；request_id={request_id}" if request_id else "")
        )
    try:
        choice = body["choices"][0]
        message = choice["message"]
        content = message.get("content")
        finish_reason = str(choice.get("finish_reason") or "unknown")
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderUnavailable(
            f"OpenRouter {label}响应缺少模型结果"
            + (f"；request_id={request_id}" if request_id else "")
        ) from exc
    choice_error = choice.get("error")
    if choice_error:
        error_message = (
            choice_error.get("message")
            if isinstance(choice_error, dict)
            else str(choice_error)
        )
        raise ProviderUnavailable(
            f"OpenRouter {label}生成失败：{str(error_message or '未知上游错误')[:500]}"
            + (f"；request_id={request_id}" if request_id else "")
        )
    refusal = message.get("refusal") if isinstance(message, dict) else None
    if refusal:
        raise ProviderUnavailable(
            f"OpenRouter {label}拒绝了本次请求：{str(refusal)[:300]}"
            + (f"；request_id={request_id}" if request_id else "")
        )
    try:
        return _OpenRouterJsonResponse(
            data=_json_object(content),
            message=message if isinstance(message, dict) else {},
            model_id=str(body.get("model") or model),
            web_search_requests=_web_search_request_count(body),
        )
    except ProviderUnavailable as exc:
        if finish_reason == "length":
            detail = "输出被截断，请从失败阶段重试"
        elif not _message_text(content).strip():
            detail = "返回内容为空，请从失败阶段重试"
        else:
            detail = "没有返回可解析的结构化结果，请从失败阶段重试"
        raise ProviderUnavailable(
            f"OpenRouter {label}{detail}（finish_reason={finish_reason}）"
            + (f"；request_id={request_id}" if request_id else "")
        ) from exc


class OpenRouterScriptProvider:
    """Generate one reviewable screenplay with independent content tracks."""

    name = "openrouter-script"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        research_model: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 300.0,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "https://openrouter.ai/api").strip()
        self.model = str(model or "").strip()
        self.research_model = str(research_model or self.model).strip()
        self.transport = transport
        self.timeout_seconds = max(10.0, float(timeout_seconds))

    @property
    def configured(self) -> bool:
        return bool(
            self.api_key
            and self.model
            and self.research_model
            and self.base_url.startswith("https://")
        )

    async def research_for_skill(
        self,
        card: SourceCard,
        *,
        research_mode: str,
        research_prompt: str,
        research_as_of: str = "",
        on_usage: UsageRecorder | None = None,
    ) -> PersonResearchBrief | NewsResearchBrief:
        """Dispatch research through the workflow frozen on the job."""

        mode = str(research_mode or "").strip()
        if mode == "person_viewpoint_optional":
            return await self.research_person_viewpoint(
                card,
                research_prompt=research_prompt,
                on_usage=on_usage,
            )
        if mode == "recent_news_required":
            return await self.research_recent_news(
                card,
                research_prompt=research_prompt,
                as_of=research_as_of,
                on_usage=on_usage,
            )
        raise ProviderUnavailable(f"脚本 Provider 不支持研究模式：{mode}")

    async def research_person_viewpoint(
        self,
        card: SourceCard,
        *,
        research_prompt: str = "",
        on_usage: UsageRecorder | None = None,
    ) -> PersonResearchBrief:
        """Build a cited editorial brief without turning research into a gate."""

        if not self.configured:
            raise ProviderUnavailable(
                "人物研究未配置：请设置 OPENROUTER_API_KEY"
            )
        person_name = card.subject.name
        viewpoint = card.core_idea
        research_date = timestamp()[:10]
        prompt = (
            "请为一条面向家长的知识短视频完成联网研究简报。必须先检索，再输出中文 JSON。\n\n"
            f"研究日期（UTC）：{research_date}\n"
            f"人物：{person_name}\n"
            f"用户给出的主题观点：{viewpoint}\n\n"
            + (
                f"【本 Skill 的研究规则】\n{research_prompt.strip()}\n\n"
                if research_prompt.strip()
                else ""
            )
            + "研究目标：确认人物的专业背景与该主题相关的概念脉络，找到它对当代家长真正有用的"
            "冲突、边界和现实场景。用户写下的观点只是创作命题，除非来源逐字支持，绝不能把它"
            "写成该人物的原话。不要为了完整而补造履历、引语、书名、研究结论或因果关系。\n\n"
            "检索时优先原始著作的可靠版本、大学/研究机构、专业协会、同行评议论文、权威出版社"
            "或可信的传记资料；至少使用两个彼此独立的查询。忽略营销软文、短视频标题、百科搬运"
            "和无来源的二手总结。资料有冲突或只能间接支持时，写入 uncertainties。\n\n"
            "输出要求：summary 是可直接交给主编的研究结论；core_tension 只写一个最有价值的认知"
            "张力；audience_relevance 写 2-4 个家长能识别的现实关联；content_angles 写 2-4 个"
            "可展开但不夸大的角度；interaction_opportunity 设计一个具体、低压力、无需暴露隐私的"
            "评论讨论点。evidence 写 3-6 条可安全转述的事实或概念，每条 source_url 必须原样使用"
            "本次检索结果中的 URL，source_title 必须与页面一致。只返回 schema 要求的 JSON。"
        )
        response = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.research_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是严谨的教育与心理学内容研究员。"
                        "区分来源事实、合理解释和编辑角度，只返回有效 JSON。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            label="人物主题研究",
            schema_name="qijia_person_research_v1",
            response_schema=_PERSON_RESEARCH_RESPONSE_SCHEMA,
            max_completion_tokens=PERSON_RESEARCH_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation="person_research",
            on_usage=on_usage,
            tools=[{
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": "exa",
                    "mode": "deep-lite",
                    "max_results": 4,
                    "max_uses": 2,
                    "max_total_results": 8,
                    "max_characters": 4000,
                    "excluded_domains": [
                        "douyin.com",
                        "xiaohongshu.com",
                        "zhihu.com",
                    ],
                },
            }],
            max_tool_calls=2,
        )
        citations = _citation_catalog(response.message)
        grounded_evidence: list[dict[str, str]] = []
        for raw in response.data.get("evidence") or []:
            if not isinstance(raw, dict):
                continue
            normalized_url = _normalized_citation_url(raw.get("source_url"))
            citation = citations.get(normalized_url)
            claim = str(raw.get("claim") or "").strip()
            if not citation or not claim:
                continue
            citation_url = citation["url"]
            if len(citation_url) > 2000:
                continue
            grounded_evidence.append({
                "claim": claim[:1200],
                "source_title": (
                    citation.get("title")
                    or str(raw.get("source_title") or "").strip()
                    or citation["url"]
                )[:500],
                "source_url": citation_url,
            })
        if not grounded_evidence:
            raise ProviderUnavailable(
                "人物主题研究没有返回可与检索注释匹配的来源，已降级使用原始观点"
            )
        generated = dict(response.data)

        def bounded_list(key: str, maximum: int) -> list:
            value = generated.get(key)
            return list(value[:maximum]) if isinstance(value, list) else []

        generated.update({
            "schema_version": "1.0",
            "person_name": person_name,
            "viewpoint": viewpoint,
            "audience_relevance": bounded_list("audience_relevance", 6),
            "content_angles": bounded_list("content_angles", 5),
            "evidence": grounded_evidence[:8],
            "uncertainties": bounded_list("uncertainties", 8),
            "model_id": response.model_id,
            "prompt_version": PERSON_RESEARCH_PROMPT_VERSION,
            "generated_at": timestamp(),
        })
        try:
            return PersonResearchBrief.model_validate(generated)
        except (TypeError, ValidationError) as exc:
            raise ProviderUnavailable(
                "人物主题研究返回内容不符合研究简报契约，已降级使用原始观点"
            ) from exc

    async def research_recent_news(
        self,
        card: SourceCard,
        *,
        research_prompt: str = "",
        as_of: str = "",
        on_usage: UsageRecorder | None = None,
    ) -> NewsResearchBrief:
        """Research current news and require at least one cited source."""

        if not self.configured:
            raise ProviderUnavailable(
                "最新新闻研究未配置：请设置 OPENROUTER_API_KEY"
            )
        topic = card.subject.name
        frozen_as_of = str(as_of or timestamp()).strip()
        try:
            frozen_at = datetime.fromisoformat(frozen_as_of)
        except ValueError as exc:
            raise ProviderUnavailable(
                "最新新闻研究截止时间无效，未调用模型"
            ) from exc
        if frozen_at.tzinfo is None or len(frozen_as_of) > 64:
            raise ProviderUnavailable(
                "最新新闻研究截止时间无效，未调用模型"
            )
        prompt = (
            "请为一条中文知识短视频完成最新新闻联网研究。必须先检索，再输出中文 JSON。\n\n"
            f"检索截止时间（Asia/Shanghai）：{frozen_as_of}\n"
            f"主题：{topic}\n"
            f"用户关注角度：{card.core_idea}\n"
            f"目标受众：{card.target_audience}\n\n"
            + (
                f"【本 Skill 的研究规则】\n{research_prompt.strip()}\n\n"
                if research_prompt.strip()
                else ""
            )
            + "至少形成一条与本次检索注释匹配且可追溯的证据。"
            "能确认页面发布时间或事件发生时间时必须填写；不能确认时留空并写入 uncertainties。"
            "优先同时检索官方或原始材料与可信独立来源，但不得为了凑站点而采用低质量转载；"
            "只有一个可追溯站点、缺少官方材料或缺少独立报道时不得猜测，必须写入 uncertainties。"
            "每条 evidence 只写来源能够直接支持的一个事实；claim 必须是非空事实描述，"
            "如果无法写出非空 claim 就不要输出该 evidence，绝不能用空字符串占位。"
            "source_url 必须原样使用本次检索结果中的 URL。published_at 写页面标注的发布时间，"
            "event_at 写事件发生时间；未知时"
            "填写空字符串，不得猜测。source_kind 只能是 official、primary、independent 或"
            " other。summary 概括最新且最重要的变化；core_tension 写清它为什么值得现在关注；"
            "audience_relevance、content_angles 各写 2-4 条；无法确认、来源冲突或可能同名混淆"
            "的内容写入 uncertainties。summary、core_tension、audience_relevance 和"
            "content_angles 均不得为空；只返回 schema 要求的字段和 JSON。"
        )
        response = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.research_model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是严谨的科技与商业新闻研究员。区分事件时间、发布时间、"
                        "既成事实、官方计划、第三方判断和传闻，只返回有效 JSON。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            label="最新新闻研究",
            schema_name="recent_news_research_v5",
            response_schema=_NEWS_RESEARCH_RESPONSE_SCHEMA,
            max_completion_tokens=NEWS_RESEARCH_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation="recent_news_research",
            on_usage=on_usage,
            tools=[{
                "type": "openrouter:web_search",
                "parameters": {
                    "engine": "exa",
                    "mode": "deep-lite",
                    "max_results": 6,
                    "max_uses": 3,
                    "max_total_results": 12,
                    "max_characters": 5000,
                    "excluded_domains": [
                        "douyin.com",
                        "xiaohongshu.com",
                        "zhihu.com",
                    ],
                },
            }],
            tool_choice="required",
            max_tool_calls=3,
        )
        citations = _citation_catalog(response.message)
        grounded_evidence: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        source_hosts: set[str] = set()
        rejected_counts: dict[str, int] = {}
        matched_citation_count = 0
        citation_excerpt_claim_count = 0

        def reject(reason: str) -> None:
            rejected_counts[reason] = rejected_counts.get(reason, 0) + 1

        raw_evidence = response.data.get("evidence") or []
        raw_evidence = raw_evidence if isinstance(raw_evidence, list) else []
        for raw in raw_evidence:
            if not isinstance(raw, dict):
                reject("invalid_item")
                continue
            normalized_url = _normalized_citation_url(raw.get("source_url"))
            if not normalized_url:
                reject("invalid_url")
                continue
            citation = _matched_citation(citations, raw.get("source_url"))
            claim = str(raw.get("claim") or "").strip()
            if not citation:
                reject("citation_not_matched")
                continue
            matched_citation_count += 1
            claim_from_excerpt = False
            if not claim:
                claim = _citation_excerpt(citation.get("content"))
                claim_from_excerpt = bool(claim)
            if not claim:
                reject("missing_claim")
                continue
            citation_key = _normalized_citation_url(citation["url"])
            if citation_key in seen_urls:
                reject("duplicate_url")
                continue
            if len(citation["url"]) > 2000:
                reject("url_too_long")
                continue
            host = (
                urlsplit(citation["url"]).hostname or ""
            ).lower().removeprefix("www.")
            if not host:
                reject("missing_host")
                continue
            source_kind = str(raw.get("source_kind") or "other").strip()
            if source_kind not in {
                "official",
                "primary",
                "independent",
                "other",
            }:
                source_kind = "other"
            seen_urls.add(citation_key)
            source_hosts.add(host)
            grounded_evidence.append({
                "claim": claim[:1200],
                "source_title": (
                    citation.get("title")
                    or str(raw.get("source_title") or "").strip()
                    or citation["url"]
                )[:500],
                "source_url": citation["url"],
                "source_kind": source_kind,
                "published_at": str(raw.get("published_at") or "").strip()[:64],
                "event_at": str(raw.get("event_at") or "").strip()[:64],
            })
            if claim_from_excerpt:
                citation_excerpt_claim_count += 1
        citation_identity_samples = [
            label
            for label in (
                _citation_identity_label(item.get("url"))
                for item in citations.values()
            )
            if label
        ][:5]
        candidate_identity_samples = [
            label
            for label in (
                _citation_identity_label(item.get("source_url"))
                for item in raw_evidence
                if isinstance(item, dict)
            )
            if label
        ][:5]
        expected_response_fields = set(
            _NEWS_RESEARCH_RESPONSE_SCHEMA["properties"]
        )
        unexpected_response_fields = sorted(
            str(key)
            for key in response.data
            if key not in expected_response_fields
        )[:10]
        accepted_timed_evidence_count = sum(
            bool(item["published_at"] or item["event_at"])
            for item in grounded_evidence
        )
        diagnostics = {
            "schema_version": "1.0",
            "operation": "recent_news_research",
            "web_search_requests": response.web_search_requests,
            "citation_count": len(citations),
            "candidate_evidence_count": len(raw_evidence),
            "matched_citation_count": matched_citation_count,
            "accepted_evidence_count": len(grounded_evidence),
            "accepted_site_count": len(source_hosts),
            "accepted_timed_evidence_count": accepted_timed_evidence_count,
            "citation_excerpt_claim_count": citation_excerpt_claim_count,
            "citation_identity_samples": citation_identity_samples,
            "candidate_identity_samples": candidate_identity_samples,
            "unexpected_response_fields": unexpected_response_fields,
            "rejected_counts": rejected_counts,
            "generated_at": timestamp(),
        }
        if not grounded_evidence:
            if not citations and (
                response.web_search_requests is not None
                and response.web_search_requests > 0
            ):
                message = "OpenRouter 已执行联网检索但未返回 citation 注释"
                detail = (
                    f"web_search_requests={response.web_search_requests}，"
                    "citation_count=0"
                )
            elif not citations:
                message = "OpenRouter 未返回可追溯的联网检索引用"
                request_count = (
                    "未回传"
                    if response.web_search_requests is None
                    else str(response.web_search_requests)
                )
                detail = (
                    f"web_search_requests={request_count}，citation_count=0；"
                    "上游可能未调用 web_search 或未回传 annotations"
                )
            elif (
                matched_citation_count > 0
                and rejected_counts.get("missing_claim", 0)
                == matched_citation_count
            ):
                message = "检索证据缺少可用事实描述"
                detail = (
                    f"citation_count={len(citations)}，"
                    f"candidate_evidence_count={len(raw_evidence)}，"
                    f"matched_citation_count={matched_citation_count}，"
                    f"rejected_counts={rejected_counts}"
                )
            elif (
                matched_citation_count == 0
                and rejected_counts.get("citation_not_matched", 0) > 0
            ):
                message = "检索引用与模型 evidence URL 未匹配"
                detail = (
                    f"citation_count={len(citations)}，"
                    f"candidate_evidence_count={len(raw_evidence)}，"
                    f"matched_citation_count=0，"
                    f"rejected_counts={rejected_counts}"
                )
            else:
                message = "检索证据未通过完整性校验"
                detail = (
                    f"citation_count={len(citations)}，"
                    f"candidate_evidence_count={len(raw_evidence)}，"
                    f"matched_citation_count={matched_citation_count}，"
                    f"rejected_counts={rejected_counts}"
                )
            diagnostics["detail"] = detail
            raise ResearchEvidenceUnavailable(
                message,
                diagnostics,
            )
        def bounded_text(key: str, maximum: int) -> str:
            value = response.data.get(key)
            return value.strip()[:maximum] if isinstance(value, str) else ""

        def bounded_strings(
            key: str,
            maximum: int,
            *,
            item_maximum: int = 1200,
        ) -> list[str]:
            value = response.data.get(key)
            if not isinstance(value, list):
                return []
            return [
                item.strip()[:item_maximum]
                for item in value
                if isinstance(item, str) and item.strip()
            ][:maximum]

        model_uncertainties = bounded_strings(
            "uncertainties",
            10,
            item_maximum=1000,
        )
        system_uncertainties: list[str] = []

        def add_system_uncertainty(message: str) -> None:
            if message not in system_uncertainties:
                system_uncertainties.append(message)

        source_kinds = {item["source_kind"] for item in grounded_evidence}
        if accepted_timed_evidence_count == 0:
            add_system_uncertainty(
                "来源未提供明确的事件或发布时间，时效性请在脚本审核时确认。"
            )
        if len(source_hosts) < 2:
            add_system_uncertainty(
                "本次研究只有一个可追溯站点，尚未完成跨站点交叉验证。"
            )
        if not source_kinds.intersection({"official", "primary"}):
            add_system_uncertainty("本次研究尚未找到官方或原始材料。")
        if "independent" not in source_kinds:
            add_system_uncertainty("本次研究尚未找到可信独立报道。")
        if citation_excerpt_claim_count:
            add_system_uncertainty(
                "部分证据的事实描述由检索注释原文摘录补全，请在脚本审核时核对。"
            )

        uncertainties: list[str] = []
        for item in (*system_uncertainties, *model_uncertainties):
            if item not in uncertainties:
                uncertainties.append(item)

        summary = (
            bounded_text("summary", 2000)
            or grounded_evidence[0]["claim"]
        )
        core_tension = (
            bounded_text("core_tension", 1200)
            or "这项变化的已确认信息与后续实际影响仍需区分。"
        )
        audience_relevance = bounded_strings("audience_relevance", 6)
        if not audience_relevance:
            audience_relevance = [
                "需要区分已确认事实、官方计划与仍待验证的后续影响。"
            ]
        content_angles = bounded_strings("content_angles", 5)
        if not content_angles:
            content_angles = [
                "先说明来源已经确认的变化，再说明仍待验证的部分。"
            ]

        # Build the final contract from an explicit allowlist. Provider or
        # response-healing metadata must never leak into the persisted brief.
        generated = {
            "schema_version": "1.0",
            "kind": "recent_news",
            "topic": topic,
            "as_of": frozen_as_of,
            "summary": summary,
            "core_tension": core_tension,
            "audience_relevance": audience_relevance,
            "content_angles": content_angles,
            "interaction_opportunity": bounded_text(
                "interaction_opportunity",
                1000,
            ),
            "evidence": grounded_evidence[:10],
            "uncertainties": uncertainties[:10],
            "model_id": response.model_id[:256],
            "prompt_version": NEWS_RESEARCH_PROMPT_VERSION,
            "generated_at": timestamp(),
        }
        try:
            return NewsResearchBrief.model_validate(generated)
        except (TypeError, ValidationError) as exc:
            if isinstance(exc, ValidationError):
                validation_errors = []
                for item in exc.errors()[:10]:
                    location = ".".join(
                        str(part) for part in item.get("loc") or ()
                    )
                    message = str(item.get("msg") or "字段校验失败")
                    validation_errors.append(
                        f"{location}: {message}" if location else message
                    )
            else:
                validation_errors = [str(exc) or "字段校验失败"]
            diagnostics["validation_errors"] = [
                item[:500] for item in validation_errors
            ]
            diagnostics["detail"] = "；".join(validation_errors)[:1000]
            raise ResearchEvidenceUnavailable(
                "研究简报字段校验失败",
                diagnostics,
            ) from exc

    def _prompt(self, card: SourceCard, prompt: str | None = None) -> str:
        sources = [item.model_dump(mode="json") for item in card.sources]
        facts = [item.model_dump(mode="json") for item in card.verified_facts]
        quotes = [item.model_dump(mode="json") for item in card.verified_quotes]
        script_ref_ids = [
            item.id for item in (*card.verified_facts, *card.verified_quotes)
        ]
        boundaries = [
            item.model_dump(mode="json") for item in card.interpretation_boundary
        ]
        creative_prompt = str(prompt or DEFAULT_SCRIPT_PROMPT).strip()
        return (
            f"{creative_prompt}\n\n"
            f"{SCRIPT_OUTPUT_CONTRACT}\n\n"
            "【本次来源卡】\n"
            f"内容领域：{card.content_domain.value}\n"
            f"内容形式：{card.content_format.value}\n"
            f"主题对象：{json.dumps(card.subject.model_dump(mode='json'), ensure_ascii=False)}\n"
            f"选题：{card.title}\n"
            f"目标受众：{card.target_audience}\n"
            f"受众问题：{card.parent_question}\n"
            f"核心材料：{card.core_idea}\n"
            f"来源信息：{json.dumps(sources, ensure_ascii=False)}\n"
            f"已核验事实：{json.dumps(facts, ensure_ascii=False)}\n"
            f"已核验引文：{json.dumps(quotes, ensure_ascii=False)}\n"
            "可用脚本引用 ID（beats.source_refs 只能从这里选择）："
            f"{json.dumps(script_ref_ids, ensure_ascii=False)}\n"
            "来源信息里的 source ID 只用于材料溯源，绝不能填写到 beats.source_refs。\n"
            f"解释边界：{json.dumps(boundaries, ensure_ascii=False)}"
        )

    @staticmethod
    def _normalize_generated_source_refs(
        card: SourceCard, generated: dict
    ) -> None:
        """Repair an unambiguous provenance ID without weakening validation."""

        segments = generated.get("beats") or generated.get("narration_segments")
        if not isinstance(segments, list):
            return

        claim_ids = {
            item.id for item in (*card.verified_facts, *card.verified_quotes)
        }
        claims_by_source: dict[str, list[str]] = {}
        for fact in card.verified_facts:
            for source_id in fact.source_refs:
                claims_by_source.setdefault(source_id, []).append(fact.id)
        for quote in card.verified_quotes:
            claims_by_source.setdefault(quote.source_id, []).append(quote.id)
        sole_claim_id = next(iter(claim_ids)) if len(claim_ids) == 1 else None

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            raw_refs = segment.get("source_refs")
            if not isinstance(raw_refs, list):
                continue
            normalized: list[str] = []
            for raw_ref in raw_refs:
                ref = str(raw_ref or "").strip()
                if not ref:
                    continue
                if ref in claim_ids:
                    normalized.append(ref)
                    continue
                candidates = list(dict.fromkeys(claims_by_source.get(ref, [])))
                if len(candidates) == 1:
                    normalized.append(candidates[0])
                else:
                    # Preserve genuinely unknown or ambiguous IDs so the
                    # domain validator still rejects them visibly.
                    normalized.append(ref)
            quote_ref = str(segment.get("quote_ref") or "").strip()
            if quote_ref in claim_ids:
                normalized.append(quote_ref)
            if not normalized and sole_claim_id:
                normalized.append(sole_claim_id)
            segment["source_refs"] = list(dict.fromkeys(normalized))

    @staticmethod
    def _generated_narration_text(value: dict) -> str:
        segments = value.get("beats") or value.get("narration_segments")
        if not isinstance(segments, list):
            return ""
        return "\n".join(
            str(item.get("narration") or item.get("text") or "")
            for item in segments
            if isinstance(item, dict)
        )

    def _script_from_generated(
        self, card: SourceCard, generated: dict
    ) -> ScriptDraft:
        try:
            self._normalize_generated_source_refs(card, generated)
            char_count = narration_char_count(
                self._generated_narration_text(generated)
            )
            generated["schema_version"] = "2.0"
            generated["source_card_id"] = card.id
            generated["source_card_revision"] = card.revision
            generated["estimated_duration_seconds"] = max(
                45, min(75, round(char_count / 4.1))
            )
            segments = generated.get("beats") or generated.get("narration_segments")
            if isinstance(segments, list) and segments:
                generated["hook"] = str(
                    segments[0].get("narration") or segments[0].get("text") or ""
                )
                generated["closing"] = str(
                    segments[-1].get("narration") or segments[-1].get("text") or ""
                )
            return ScriptDraft.model_validate(generated)
        except (
            AttributeError,
            KeyError,
            IndexError,
            TypeError,
            ValidationError,
        ) as exc:
            raise ProviderUnavailable(
                "脚本模型返回内容不符合工作台契约，请重新生成"
            ) from exc

    async def generate(
        self, card: SourceCard, prompt: str | None = None
    ) -> ScriptDraft:
        return await self.generate_with_usage(card, prompt)

    async def generate_for_skill(
        self,
        card: SourceCard,
        prompt: str,
        *,
        system_prompt: str,
        on_usage: UsageRecorder | None = None,
    ) -> ScriptDraft:
        return await self.generate_with_usage(
            card,
            prompt,
            system_prompt=system_prompt,
            on_usage=on_usage,
        )

    async def generate_with_usage(
        self,
        card: SourceCard,
        prompt: str | None = None,
        *,
        system_prompt: str | None = None,
        on_usage: UsageRecorder | None = None,
    ) -> ScriptDraft:
        if not self.configured:
            raise ProviderUnavailable(
                "真实脚本生成未配置：请设置 OPENROUTER_API_KEY"
            )
        messages = [
            {
                "role": "system",
                "content": system_prompt or (
                    "你是严谨的知识短视频主编。"
                    "严格遵守来源边界，只返回有效 JSON。"
                ),
            },
            {"role": "user", "content": self._prompt(card, prompt)},
        ]
        response = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=messages,
            label="脚本生成",
            schema_name="qijia_script_draft_v2",
            response_schema=_SCRIPT_RESPONSE_SCHEMA,
            max_completion_tokens=SCRIPT_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation="script_generation",
            on_usage=on_usage,
        )

        return self._script_from_generated(card, response.data)

    async def review(self, card: SourceCard, script: ScriptDraft) -> ScriptReview:
        known_fact_ids = {item.id for item in card.verified_facts}
        known_quote_ids = {item.id for item in card.verified_quotes}
        known_ids = known_fact_ids | known_quote_ids
        blocking: list[str] = []
        referenced: set[str] = set()
        for segment in script.narration_segments:
            referenced.update(segment.source_refs)
            unknown = sorted(set(segment.source_refs) - known_ids)
            if unknown:
                blocking.append(f"段落 {segment.id} 包含未知引用：{unknown}")
            if not segment.source_refs:
                blocking.append(f"段落 {segment.id} 没有来源引用")
        missing_boundaries = [
            item.id
            for item in card.interpretation_boundary
            if item.text not in script.narration_text()
        ]
        return ScriptReview(
            passed=not blocking,
            claim_checks=[
                {
                    "fact_id": item.id,
                    "status": "referenced" if item.id in referenced else "not_used",
                }
                for item in card.verified_facts
            ],
            quote_checks=[
                {
                    "quote_id": item.id,
                    "status": "referenced" if item.id in referenced else "not_used",
                }
                for item in card.verified_quotes
            ],
            boundary_checks=[
                {
                    "boundary_id": item.id,
                    "status": (
                        "included" if item.id not in missing_boundaries else "manual_check"
                    ),
                }
                for item in card.interpretation_boundary
            ],
            warnings=[
                f"解释边界需在脚本确认时人工复核：{item}"
                for item in missing_boundaries
            ],
            blocking_reasons=blocking,
            model_id=f"{self.model}+deterministic-review",
            prompt_version=SCRIPT_PROMPT_VERSION,
            input_hash=content_hash(script),
            reviewed_at=timestamp(),
        )


class OpenRouterStoryboardProvider:
    """Turn an approved script into an ordered provider-neutral shot plan."""

    name = "openrouter-storyboard"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = 300.0,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or "https://openrouter.ai/api").strip()
        self.model = str(model or "").strip()
        self.transport = transport
        self.timeout_seconds = max(10.0, float(timeout_seconds))

    @property
    def configured(self) -> bool:
        return bool(
            self.api_key
            and self.model
            and self.base_url.startswith("https://")
        )

    async def generate(
        self,
        script: ScriptDraft,
        base_style: str,
        beat_groups: list[list[str]],
        visual_types: list[str],
    ) -> StoryboardPlan:
        return await self.generate_with_usage(
            script,
            base_style,
            beat_groups,
            visual_types,
        )

    async def generate_with_usage(
        self,
        script: ScriptDraft,
        base_style: str,
        beat_groups: list[list[str]],
        visual_types: list[str],
        *,
        on_usage: UsageRecorder | None = None,
    ) -> StoryboardPlan:
        if not self.configured:
            raise ProviderUnavailable(
                "真实分镜生成未配置：请设置 OPENROUTER_API_KEY"
            )
        expected_beat_ids = [item.id for item in script.beats]
        shot_count = len(beat_groups)
        if (
            not 5 <= shot_count <= 13
            or any(not group for group in beat_groups)
            or not _beat_groups_cover_script(beat_groups, expected_beat_ids)
            or len(visual_types) != shot_count
            or visual_types.count("video") != 3
            or visual_types.count("image") != shot_count - 3
        ):
            raise ProviderUnavailable(
                "镜头分组和媒介分配必须按顺序完整覆盖全部叙事段"
            )
        beats_by_id = {item.id: item for item in script.beats}
        try:
            grouped_beats = [
                [beats_by_id[beat_id] for beat_id in group]
                for group in beat_groups
            ]
        except KeyError as exc:
            raise ProviderUnavailable("分镜目标与当前脚本不一致") from exc
        target_segments = [group[0] for group in grouped_beats]
        input_payload = {
            "script_hash": content_hash(script),
            "base_style": base_style,
            "beat_groups": beat_groups,
            "visual_types": visual_types,
        }
        output_skeleton = json.dumps({
            "shots": [
                {
                    "segment_id": group[0].id,
                    "beat_ids": [item.id for item in group],
                    "visual_intent": "",
                    "first_frame_prompt": "",
                    "motion_prompt": "",
                }
                for group in grouped_beats
            ],
        }, ensure_ascii=False)
        prompt = (
            "严格执行下方【统一基础规格】中的唯一提示词编排方法，不引入或叠加"
            "第二套导演方法。\n\n"
            f"把完整脚本规划成一条连续的竖屏视觉叙事。脚本已确定性地分成 {shot_count} 个"
            "章节，每章可能承载一个或多个相邻叙事段；不得遗漏、合并或重排。先在内部建立"
            "全片主体、空间、关键物件和状态变化，再逐章推进。相邻章节可以承接同一动作的"
            "不同阶段、景别或视角，但不能重复同一构图或重新发明主体。\n\n"
            "各章媒介已经根据真实旁白时长确定，不得自行更改："
            + "；".join(
                f"第 {index} 章为 {visual_type}"
                for index, visual_type in enumerate(visual_types, 1)
            )
            + "。image 章节需要首帧与后期取景方向；video 章节需要首帧和可直接用于"
            "首帧驱动 I2V 的 motion_prompt，具体写法完全服从统一基础规格。\n\n"
            "第一章直接处在冲突、反常识结果或关键选择中，不先用空镜、主体入场或环境介绍；"
            "动作从第一帧发生，前 2 秒让主体关系和矛盾可见，前 5 秒出现第二层可见信息。"
            "钩子必须来自内容本身，不能用"
            "夸张惊吓、焦虑表演或虚假危机。\n\n"
            "字段职责必须严格分离：visual_intent 只用一句话写观众必须看懂的主体、关系、"
            "变化或结果，不写画风、色彩、材质、构图、景别、运镜和通用禁用项。"
            "first_frame_prompt 是可直接发送给图片模型的自包含正向提示词，按统一基础规格"
            "完整落实本章画面。motion_prompt 对 video 是可直接发送给 I2V 模型的提示词，"
            "对 image 只写后期取景方向。不要在这些字段里复述方法说明、字段标签或整段"
            "通用限制，系统会在媒体提交前统一附加一次硬边界。\n\n"
            "每段 visual_direction 只提供内容语义。你负责依据统一基础规格把语义编译成"
            "完整视觉提示词；on_screen_text 只供后期排版参考，绝不能变成画内文字。"
            "如果基础规格包含参考素材规则，只让参考图约束其中已经定义的视觉属性，不得让"
            "参考图覆盖事实、安全和本章语义。\n\n"
            "严格复制下面 JSON 骨架，只填写三个空字符串字段；不得新增、删除、合并或"
            "重排 shots，不得修改 segment_id 或 beat_ids。最终只返回这一个 JSON 对象：\n"
            f"{output_skeleton}\n\n"
            f"【统一基础规格】{base_style}\n"
            f"【{shot_count} 个视觉章节】\n"
            + "\n".join(
                (
                    f"章节 {index}（{visual_types[index - 1]}，"
                    f"{','.join(item.id for item in group)}）\n"
                    + "\n".join(
                        f"- 旁白：{item.narration}\n"
                        f"  画面意图：{item.visual_direction}\n"
                        f"  后期屏幕文字：{item.on_screen_text or '无'}"
                        for item in group
                    )
                )
                for index, group in enumerate(grouped_beats, 1)
            )
        )
        response = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是执行统一基础规格的 H3 视觉提示词编排器。"
                        "只返回符合要求的 JSON，不在画面中设计任何可读文字。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            label="分镜生成",
            schema_name="qijia_storyboard_v4",
            response_schema=_STORYBOARD_RESPONSE_SCHEMA,
            max_completion_tokens=STORYBOARD_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation="storyboard_generation",
            on_usage=on_usage,
        )
        raw_shots = _normalize_storyboard_rows(
            response.data.get("shots"), target_segments
        )
        shots: list[StoryboardShot] = []
        try:
            for index, (raw, group) in enumerate(
                zip(raw_shots, grouped_beats), 1
            ):
                segment = group[0]
                if not isinstance(raw, dict):
                    raise ValueError("shot payload")
                shots.append(StoryboardShot(
                    shot_id=f"shot_{index:02d}",
                    segment_id=segment.id,
                    beat_ids=[item.id for item in group],
                    narration_excerpt="\n".join(item.narration for item in group),
                    visual_type=visual_types[index - 1],
                    visual_intent=str(raw.get("visual_intent") or ""),
                    first_frame_prompt=str(raw.get("first_frame_prompt") or ""),
                    motion_prompt=str(raw.get("motion_prompt") or ""),
                ))
            return StoryboardPlan(
                shots=shots,
                model_id=self.model,
                prompt_version=STORYBOARD_PROMPT_VERSION,
                input_hash=content_hash(input_payload),
                created_at=timestamp(),
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProviderUnavailable(
                f"分镜模型返回内容不符合 {shot_count} 镜头契约，请重试"
            ) from exc
