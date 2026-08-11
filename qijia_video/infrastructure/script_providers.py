"""真实脚本生成 Provider；只依赖 OpenRouter 的 OpenAI 兼容接口。"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from pydantic import ValidationError

from qijia_video.contracts import (
    CreativeBrief,
    EditorialPlan,
    ProviderUsageRecord,
    ScriptDraft,
    ScriptReview,
    SourceCard,
    StoryboardPlan,
    StoryboardShot,
    ShotContextIR,
    VisualBible,
    content_hash,
    timestamp,
)
from qijia_video.errors import ProviderUnavailable
from qijia_video.prompt_orchestration import compile_legacy_h3_script_prompt
from qijia_video.prompts import (
    SCRIPT_OUTPUT_CONTRACT,
    SCRIPT_SKILL_OUTPUT_CONTRACT,
    narration_char_count,
)
from qijia_video.tts_options import (
    DEFAULT_TTS_SPEED_RATIO,
    TTS_SCRIPT_CHARACTER_TARGETS,
)


SCRIPT_PROMPT_VERSION = "qijia_script_v14_single_creative_brief"
SCRIPT_SKILL_PROMPT_VERSION = 'qijia_script_v15_editorial_plan'
STORYBOARD_PROMPT_VERSION = "qijia_storyboard_v12_semantic_adaptive"
DIRECTOR_PROMPT_VERSION = 'qijia_director_v13_shot_context_ir'
OPENROUTER_REASONING_EFFORT = "high"
SCRIPT_MAX_COMPLETION_TOKENS = 48_000
STORYBOARD_MAX_COMPLETION_TOKENS = 128_000
UsageRecorder = Callable[[ProviderUsageRecord], Awaitable[None]]


_CREATIVE_BRIEF_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "central_question": {"type": "string"},
        "core_thesis": {"type": "string"},
        "audience_promise": {"type": "string"},
        "narrative_arc": {
            "type": "array",
            "items": {"type": "string"},
        },
        "tone": {"type": "string"},
        "visual_concept": {"type": "string"},
        "continuity_anchors": {
            "type": "array",
            "items": {"type": "string"},
        },
        "must_include": {
            "type": "array",
            "items": {"type": "string"},
        },
        "must_avoid": {
            "type": "array",
            "items": {"type": "string"},
        },
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": [
        "central_question",
        "core_thesis",
        "audience_promise",
        "narrative_arc",
        "tone",
        "visual_concept",
        "continuity_anchors",
        "must_include",
        "must_avoid",
        "evidence_refs",
    ],
    "additionalProperties": False,
}


_SCRIPT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "creative_brief": _CREATIVE_BRIEF_RESPONSE_SCHEMA,
        "schema_version": {"type": "string", "enum": ["3.0"]},
        "video_title": {"type": "string"},
        "cover_text": {"type": "string"},
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
                    "on_screen_text",
                    "source_refs",
                    "quote_ref",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "creative_brief",
        "schema_version",
        "video_title",
        "cover_text",
        "caption",
        "hashtags",
        "beats",
    ],
    "additionalProperties": False,
}

_EDITORIAL_PLAN_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'objective': {'type': 'string'},
        'central_question': {'type': 'string'},
        'candidate_angles': {
            'type': 'array',
            'minItems': 2,
            'maxItems': 3,
            'items': {
                'type': 'object',
                'properties': {
                    'angle_id': {'type': 'string'},
                    'premise': {'type': 'string'},
                    'audience_value': {'type': 'string'},
                    'evidence_refs': {
                        'type': 'array',
                        'items': {'type': 'string'},
                    },
                    'risk': {'type': 'string'},
                },
                'required': [
                    'angle_id',
                    'premise',
                    'audience_value',
                    'evidence_refs',
                    'risk',
                ],
                'additionalProperties': False,
            },
        },
        'selected_angle_id': {'type': 'string'},
        'selection_reason': {'type': 'string'},
        'core_thesis': {'type': 'string'},
        'audience_promise': {'type': 'string'},
        'narrative_arc': {'type': 'array', 'items': {'type': 'string'}},
        'tone': {'type': 'string'},
        'must_include': {'type': 'array', 'items': {'type': 'string'}},
        'must_avoid': {'type': 'array', 'items': {'type': 'string'}},
        'evidence_refs': {'type': 'array', 'items': {'type': 'string'}},
        'critic_summary': {'type': 'string'},
    },
    'required': [
        'objective',
        'central_question',
        'candidate_angles',
        'selected_angle_id',
        'selection_reason',
        'core_thesis',
        'audience_promise',
        'narrative_arc',
        'tone',
        'must_include',
        'must_avoid',
        'evidence_refs',
        'critic_summary',
    ],
    'additionalProperties': False,
}

_SCRIPT_SKILL_RESPONSE_SCHEMA = json.loads(json.dumps(_SCRIPT_RESPONSE_SCHEMA))
_SCRIPT_SKILL_RESPONSE_SCHEMA['properties'].pop('creative_brief')
_SCRIPT_SKILL_RESPONSE_SCHEMA['properties']['editorial_plan'] = (
    _EDITORIAL_PLAN_RESPONSE_SCHEMA
)
_SCRIPT_SKILL_RESPONSE_SCHEMA['required'] = [
    'editorial_plan' if item == 'creative_brief' else item
    for item in _SCRIPT_SKILL_RESPONSE_SCHEMA['required']
]


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
                    "visual_type": {
                        "type": "string",
                        "enum": ["image", "video"],
                        "description": (
                            "默认 image；只有连续动作或状态变化对理解不可替代时"
                            "才选择 video，全片最多三段 video"
                        ),
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
                    "visual_type",
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


_SHOT_CONTEXT_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        key: {'type': 'string'}
        for key in (
            'semantic_goal',
            'visual_metaphor',
            'subject',
            'action',
            'environment',
            'composition',
            'continuity_handoff',
            'start_state',
            'end_state',
            'camera_intent',
            'media_rationale',
        )
    },
    'required': [
        'semantic_goal',
        'visual_metaphor',
        'subject',
        'action',
        'environment',
        'composition',
        'continuity_handoff',
        'start_state',
        'end_state',
        'camera_intent',
        'media_rationale',
        'reference_roles',
    ],
    'additionalProperties': False,
}
_SHOT_CONTEXT_RESPONSE_SCHEMA['properties']['reference_roles'] = {
    'type': 'array',
    'items': {'type': 'string'},
}

_VISUAL_BIBLE_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'core_visual_idea': {'type': 'string'},
        'visual_world': {'type': 'string'},
        'recurring_subjects': {'type': 'array', 'items': {'type': 'string'}},
        'scene_anchors': {'type': 'array', 'items': {'type': 'string'}},
        'continuity_rules': {'type': 'array', 'items': {'type': 'string'}},
        'color_material_system': {'type': 'string'},
        'composition_system': {'type': 'string'},
        'reference_strategy': {'type': 'string'},
        'forbidden_elements': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': [
        'core_visual_idea',
        'visual_world',
        'recurring_subjects',
        'scene_anchors',
        'continuity_rules',
        'color_material_system',
        'composition_system',
        'reference_strategy',
        'forbidden_elements',
    ],
    'additionalProperties': False,
}

_DIRECTOR_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'visual_bible': _VISUAL_BIBLE_RESPONSE_SCHEMA,
        'shots': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'segment_id': {'type': 'string'},
                    'beat_ids': {'type': 'array', 'items': {'type': 'string'}},
                    'visual_type': {
                        'type': 'string',
                        'enum': ['image', 'video'],
                    },
                    'context': _SHOT_CONTEXT_RESPONSE_SCHEMA,
                },
                'required': ['segment_id', 'beat_ids', 'visual_type', 'context'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['visual_bible', 'shots'],
    'additionalProperties': False,
}


@dataclass(frozen=True)
class _OpenRouterJsonResponse:
    data: dict
    model_id: str


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
        semantic_intent = (
            f"{fallback['role']}：依据本段旁白提炼一个具体、可观察的语义变化"
        )
        requested_type = str(raw.get("visual_type") or "").strip()
        visual_type = (
            requested_type
            if requested_type in {"image", "video"}
            else ("video" if index == 0 else "image")
        )
        normalized.append({
            "segment_id": segment.id,
            "visual_type": visual_type,
            "visual_intent": _storyboard_text(
                raw.get("visual_intent"), semantic_intent, 600
            ),
            "first_frame_prompt": _storyboard_text(
                raw.get("first_frame_prompt"),
                fallback["first_frame_prompt"],
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


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


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
            model_id=str(body.get("model") or model),
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

    def _prompt(
        self,
        card: SourceCard,
        prompt: str | None = None,
        *,
        output_contract: str = SCRIPT_OUTPUT_CONTRACT,
    ) -> str:
        if prompt is None:
            minimum, maximum = TTS_SCRIPT_CHARACTER_TARGETS[
                DEFAULT_TTS_SPEED_RATIO
            ]
            creative_prompt = compile_legacy_h3_script_prompt(
                card,
                profile=None,
                research_brief=None,
                minimum_characters=minimum,
                maximum_characters=maximum,
            )
        else:
            creative_prompt = str(prompt).strip()
        # The Pipeline v1 compiler already contains the immutable input and the
        # only EvidencePack. Appending the source card again diluted attention.
        return f'{creative_prompt}\n\n{output_contract}'

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
            generated = dict(generated)
            generated.pop("creative_brief", None)
            generated.pop('editorial_plan', None)
            self._normalize_generated_source_refs(card, generated)
            char_count = narration_char_count(
                self._generated_narration_text(generated)
            )
            generated["schema_version"] = "3.0"
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

    def _creative_brief_from_generated(
        self,
        card: SourceCard,
        generated: dict,
        *,
        model_id: str,
        prompt: str,
    ) -> CreativeBrief:
        raw = generated.get("creative_brief")
        if not isinstance(raw, dict):
            raise ProviderUnavailable("脚本模型没有返回 H3 CreativeBrief")
        payload = dict(raw)
        payload.update({
            "schema_version": "1.0",
            "model_id": model_id,
            "prompt_version": SCRIPT_PROMPT_VERSION,
            "input_hash": content_hash({
                "card": card.model_dump(mode="json"),
                "prompt": prompt,
            }),
            "generated_at": timestamp(),
        })
        try:
            brief = CreativeBrief.model_validate(payload)
        except (TypeError, ValidationError) as exc:
            raise ProviderUnavailable(
                "脚本模型返回的 H3 CreativeBrief 不符合契约"
            ) from exc
        allowed_refs = {
            item.id for item in (*card.verified_facts, *card.verified_quotes)
        }
        unknown = sorted(set(brief.evidence_refs) - allowed_refs)
        if unknown:
            raise ProviderUnavailable(
                f"H3 CreativeBrief 引用了未知证据：{unknown}"
            )
        return brief

    def _editorial_plan_from_generated(
        self,
        card: SourceCard,
        generated: dict,
        *,
        model_id: str,
        prompt: str,
    ) -> EditorialPlan:
        raw = generated.get('editorial_plan')
        if not isinstance(raw, dict):
            raise ProviderUnavailable('脚本模型没有返回 EditorialPlan')
        payload = dict(raw)
        payload.update({
            'schema_version': '1.0',
            'model_id': model_id,
            'prompt_version': SCRIPT_SKILL_PROMPT_VERSION,
            'input_hash': content_hash({
                'card': card.model_dump(mode='json'),
                'prompt': prompt,
            }),
            'generated_at': timestamp(),
        })
        try:
            plan = EditorialPlan.model_validate(payload)
        except (TypeError, ValidationError) as exc:
            raise ProviderUnavailable('脚本模型返回的 EditorialPlan 不符合契约') from exc
        allowed_refs = {
            item.id for item in (*card.verified_facts, *card.verified_quotes)
        }
        used_refs = set(plan.evidence_refs)
        for angle in plan.candidate_angles:
            used_refs.update(angle.evidence_refs)
        unknown = sorted(used_refs - allowed_refs)
        if unknown:
            raise ProviderUnavailable(f'EditorialPlan 引用了未知证据：{unknown}')
        return plan

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
        _, script = await self.generate_with_brief(
            card,
            prompt,
            system_prompt=system_prompt,
            on_usage=on_usage,
        )
        return script

    async def generate_with_plan(
        self,
        card: SourceCard,
        prompt: str,
        *,
        on_usage: UsageRecorder | None = None,
    ) -> tuple[EditorialPlan, ScriptDraft]:
        if not self.configured:
            raise ProviderUnavailable(
                '真实脚本生成未配置：请设置 OPENROUTER_API_KEY'
            )
        user_prompt = self._prompt(
            card,
            prompt,
            output_contract=SCRIPT_SKILL_OUTPUT_CONTRACT,
        )
        response = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    'role': 'system',
                    'content': (
                        '你是任务冻结的唯一 Script Skill。只决定内容角度、论证结构和口播，'
                        '禁止设计视觉、镜头或媒体提示词。先比较候选角度并内部审稿，最终只'
                        '返回符合约定的 JSON。'
                    ),
                },
                {'role': 'user', 'content': user_prompt},
            ],
            label='脚本生成',
            schema_name='qijia_editorial_script_v1',
            response_schema=_SCRIPT_SKILL_RESPONSE_SCHEMA,
            max_completion_tokens=SCRIPT_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation='script_generation',
            on_usage=on_usage,
        )
        plan = self._editorial_plan_from_generated(
            card,
            response.data,
            model_id=response.model_id,
            prompt=user_prompt,
        )
        return plan, self._script_from_generated(card, response.data)

    async def generate_with_brief(
        self,
        card: SourceCard,
        prompt: str | None = None,
        *,
        system_prompt: str | None = None,
        on_usage: UsageRecorder | None = None,
    ) -> tuple[CreativeBrief, ScriptDraft]:
        """Resume Pipeline v1 H3 jobs; new jobs call generate_with_plan."""

        if not self.configured:
            raise ProviderUnavailable(
                "真实脚本生成未配置：请设置 OPENROUTER_API_KEY"
            )
        user_prompt = self._prompt(card, prompt)
        messages = [
            {
                "role": "system",
                "content": system_prompt or (
                    "你是本任务唯一的 H3 Creative Director 和知识短视频主编。"
                    "先收敛唯一 CreativeBrief，再按它写完整脚本；严格遵守证据边界，"
                    "不要逐段导演画面，只返回有效 JSON。"
                ),
            },
            {"role": "user", "content": user_prompt},
        ]
        response = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=messages,
            label="脚本生成",
            schema_name="qijia_script_draft_v3",
            response_schema=_SCRIPT_RESPONSE_SCHEMA,
            max_completion_tokens=SCRIPT_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation="script_generation",
            on_usage=on_usage,
        )

        brief = self._creative_brief_from_generated(
            card,
            response.data,
            model_id=response.model_id,
            prompt=user_prompt,
        )
        return brief, self._script_from_generated(card, response.data)

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

    async def generate_with_direction(
        self,
        script: ScriptDraft,
        director_instruction: str,
        beat_groups: list[list[str]],
        visual_types: list[str],
        *,
        director_skill_id: str,
        director_skill_version: str,
        on_usage: UsageRecorder | None = None,
    ) -> tuple[VisualBible, StoryboardPlan]:
        '''Generate neutral direction; media prompts are compiled later.'''

        if not self.configured:
            raise ProviderUnavailable('真实分镜生成未配置：请设置 OPENROUTER_API_KEY')
        expected_beat_ids = [item.id for item in script.beats]
        shot_count = len(beat_groups)
        adaptive_media = not visual_types
        if (
            not 3 <= shot_count <= 12
            or any(not group for group in beat_groups)
            or not _beat_groups_cover_script(beat_groups, expected_beat_ids)
            or (
                not adaptive_media
                and (
                    len(visual_types) != shot_count
                    or any(item not in {'image', 'video'} for item in visual_types)
                )
            )
        ):
            raise ProviderUnavailable('导演章节必须按顺序完整覆盖全部叙事段')
        beats_by_id = {item.id: item for item in script.beats}
        grouped_beats = [
            [beats_by_id[beat_id] for beat_id in group]
            for group in beat_groups
        ]
        input_payload = {
            'script_hash': content_hash(script),
            'base_style': director_instruction,
            'beat_groups': beat_groups,
        }
        if visual_types:
            input_payload['visual_types'] = visual_types
        context_keys = (
            'semantic_goal',
            'visual_metaphor',
            'subject',
            'action',
            'environment',
            'composition',
            'continuity_handoff',
            'start_state',
            'end_state',
            'camera_intent',
            'media_rationale',
        )
        empty_context = {key: '' for key in context_keys}
        empty_context['reference_roles'] = []
        skeleton = {
            'visual_bible': {
                'core_visual_idea': '',
                'visual_world': '',
                'recurring_subjects': [],
                'scene_anchors': [],
                'continuity_rules': [],
                'color_material_system': '',
                'composition_system': '',
                'reference_strategy': '',
                'forbidden_elements': [],
            },
            'shots': [
                {
                    'segment_id': group[0].id,
                    'beat_ids': [item.id for item in group],
                    'visual_type': visual_types[index] if visual_types else 'image',
                    'context': dict(empty_context),
                }
                for index, group in enumerate(grouped_beats)
            ],
        }
        media_instruction = (
            '媒介已冻结，不得更改：'
            + '；'.join(
                f'第 {index} 章为 {kind}'
                for index, kind in enumerate(visual_types, 1)
            )
            if visual_types
            else (
                '媒介由你选择：image 是默认项；只有连续动作、状态转变或镜头运动对理解'
                '不可替代时才用 video，全片最多三段 video，不要求凑满'
            )
        )
        prompt = (
            f'{director_instruction}\n\n'
            f'把已确认脚本规划成 {shot_count} 个连续视觉章节。先输出一份 VisualBible，'
            '再为每章输出 ShotContextIR。脚本是内容唯一真相，不改写旁白、事实或论点。'
            '相邻章节必须写清上一章 end_state 如何成为本章 continuity_handoff，禁止重复'
            '同一视觉隐喻或只替换景别。\n\n'
            f'{media_instruction}。media_rationale 必须解释本章为何需要该媒介。'
            'ShotContextIR 使用可观察描述，不得写 Seedream、Seedance、H3、prompt、参数、'
            '负向提示词或任何可直接提交模型的最终提示词。\n\n'
            '严格复制以下 JSON 骨架，不增删、合并或重排 shots，也不得修改 segment_id 和'
            ' beat_ids；最终只返回 JSON：\n'
            + json.dumps(skeleton, ensure_ascii=False)
            + '\n\n【完整脚本章节】\n'
            + '\n'.join(
                f'章节 {index}（{",".join(item.id for item in group)}）\n'
                + '\n'.join(f'- {item.narration}' for item in group)
                for index, group in enumerate(grouped_beats, 1)
            )
        )
        response = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    'role': 'system',
                    'content': (
                        '你是任务冻结的唯一 Director Skill。只交付 VisualBible 与中立的 '
                        'ShotContextIR，不写媒体模型提示词，不改写脚本。'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            label='分镜生成',
            schema_name='qijia_director_context_v1',
            response_schema=_DIRECTOR_RESPONSE_SCHEMA,
            max_completion_tokens=STORYBOARD_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation='storyboard_generation',
            on_usage=on_usage,
        )
        raw_shots = response.data.get('shots')
        if not isinstance(raw_shots, list) or len(raw_shots) != shot_count:
            raise ProviderUnavailable('Director Skill 返回了错误的章节数量')
        selected_types = list(visual_types) if visual_types else [
            str(item.get('visual_type') or '') for item in raw_shots
        ]
        if not visual_types:
            kept_videos = 0
            for index, kind in enumerate(selected_types):
                if kind == 'video' and kept_videos < 3:
                    kept_videos += 1
                else:
                    selected_types[index] = 'image'
        shots: list[StoryboardShot] = []
        try:
            for index, (raw, group) in enumerate(zip(raw_shots, grouped_beats), 1):
                expected_ids = [item.id for item in group]
                if (
                    not isinstance(raw, dict)
                    or str(raw.get('segment_id') or '') != expected_ids[0]
                    or list(raw.get('beat_ids') or []) != expected_ids
                ):
                    raise ValueError('shot mapping')
                context = ShotContextIR.model_validate(raw.get('context'))
                shots.append(StoryboardShot(
                    shot_id=f'shot_{index:02d}',
                    segment_id=expected_ids[0],
                    beat_ids=expected_ids,
                    narration_excerpt='\n'.join(item.narration for item in group),
                    visual_type=selected_types[index - 1],
                    visual_intent=context.semantic_goal,
                    context=context,
                ))
            metaphors = {
                re.sub(r'\s+', '', item.context.visual_metaphor).lower()
                for item in shots
                if item.context
            }
            if len(metaphors) != len(shots):
                raise ValueError('duplicate visual metaphor')
            bible_payload = dict(response.data.get('visual_bible') or {})
            bible_payload.update({
                'schema_version': '1.0',
                'director_skill_id': director_skill_id,
                'director_skill_version': director_skill_version,
                'model_id': response.model_id,
                'input_hash': content_hash(input_payload),
                'created_at': timestamp(),
            })
            bible = VisualBible.model_validate(bible_payload)
            plan = StoryboardPlan(
                schema_version='2.0',
                shots=shots,
                model_id=response.model_id,
                prompt_version=DIRECTOR_PROMPT_VERSION,
                input_hash=content_hash(input_payload),
                created_at=timestamp(),
            )
            return bible, plan
        except (TypeError, ValueError, ValidationError) as exc:
            raise ProviderUnavailable(
                'Director Skill 返回内容不符合 VisualBible/ShotContextIR 契约，请重试'
            ) from exc

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
        """Resume Pipeline v1 storyboards; v2 calls generate_with_direction."""

        if not self.configured:
            raise ProviderUnavailable(
                "真实分镜生成未配置：请设置 OPENROUTER_API_KEY"
            )
        expected_beat_ids = [item.id for item in script.beats]
        shot_count = len(beat_groups)
        adaptive_media = not visual_types
        if (
            not 3 <= shot_count <= 12
            or any(not group for group in beat_groups)
            or not _beat_groups_cover_script(beat_groups, expected_beat_ids)
            or (
                not adaptive_media
                and (
                    len(visual_types) != shot_count
                    or any(item not in {"image", "video"} for item in visual_types)
                )
            )
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
        }
        if visual_types:
            input_payload["visual_types"] = visual_types
        output_skeleton = json.dumps({
            "shots": [
                {
                    "segment_id": group[0].id,
                    "beat_ids": [item.id for item in group],
                    "visual_type": (
                        visual_types[index] if visual_types else "image"
                    ),
                    "visual_intent": "",
                    "first_frame_prompt": "",
                    "motion_prompt": "",
                }
                for index, group in enumerate(grouped_beats)
            ],
        }, ensure_ascii=False)
        media_instruction = (
            "各章媒介已经由历史任务冻结，不得自行更改："
            + "；".join(
                f"第 {index} 章为 {visual_type}"
                for index, visual_type in enumerate(visual_types, 1)
            )
            if visual_types
            else (
                "请根据每章可见语义选择媒介。image 是默认选择；只有连续动作、"
                "状态转变或镜头运动对理解不可替代时才选择 video。全片最多三段 "
                "video，不需要凑满，抽象解释和静态关系优先 image"
            )
        )
        prompt = (
            "严格执行下方【统一基础规格】中的唯一提示词编排方法，不引入或叠加"
            "第二套导演方法。\n\n"
            f"把完整脚本规划成一条连续的竖屏视觉叙事。脚本已确定性地分成 {shot_count} 个"
            "章节，每章可能承载一个或多个相邻叙事段；不得遗漏、合并或重排。先在内部建立"
            "全片主体、空间、关键物件和状态变化，再逐章推进。相邻章节可以承接同一动作的"
            "不同阶段、景别或视角，但不能重复同一构图或重新发明主体。\n\n"
            f"{media_instruction}。image 章节需要首帧与后期取景方向；"
            "video 章节需要首帧和可直接用于"
            "首帧驱动 I2V 的 motion_prompt，具体写法完全服从统一基础规格。\n\n"
            "第一章直接处在冲突、反常识结果或关键选择中，不先用空镜、主体入场或环境介绍；"
            "从第一帧就让主体关系和矛盾可见。钩子必须来自内容本身，不能用"
            "夸张惊吓、焦虑表演或虚假危机。\n\n"
            "字段职责必须严格分离：visual_intent 只用一句话写观众必须看懂的主体、关系、"
            "变化或结果，不写画风、色彩、材质、构图、景别、运镜和通用禁用项。"
            "first_frame_prompt 是可直接发送给图片模型的自包含正向提示词，按统一基础规格"
            "完整落实本章画面。motion_prompt 对 video 是可直接发送给 I2V 模型的提示词，"
            "对 image 只写后期取景方向。不要在这些字段里复述方法说明、字段标签或整段"
            "通用限制，系统会在媒体提交前统一附加一次硬边界。\n\n"
            "直接从完整旁白与冻结的 CreativeBrief 提炼本章可见语义；"
            "on_screen_text 只供后期排版参考，绝不能变成画内文字。"
            "如果基础规格包含参考素材规则，只让参考图约束其中已经定义的视觉属性，不得让"
            "参考图覆盖事实、安全和本章语义。\n\n"
            "严格复制下面 JSON 骨架；不得新增、删除、合并或重排 shots，不得修改 "
            "segment_id 或 beat_ids。自适应任务可修改 visual_type，其他字段完整填写。"
            "最终只返回这一个 JSON 对象：\n"
            f"{output_skeleton}\n\n"
            f"【统一基础规格】{base_style}\n"
            f"【{shot_count} 个视觉章节】\n"
            + "\n".join(
                (
                    f"章节 {index}（"
                    f"{visual_types[index - 1] if visual_types else '媒介待规划'}，"
                    f"{','.join(item.id for item in group)}）\n"
                    + "\n".join(
                        f"- 旁白：{item.narration}\n"
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
            schema_name="qijia_storyboard_v5",
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
        selected_types = (
            list(visual_types)
            if visual_types
            else [str(item.get("visual_type") or "image") for item in raw_shots]
        )
        if not visual_types:
            kept_videos = 0
            for index, value in enumerate(selected_types):
                if value == "video" and kept_videos < 3:
                    kept_videos += 1
                else:
                    selected_types[index] = "image"
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
                    visual_type=selected_types[index - 1],
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
