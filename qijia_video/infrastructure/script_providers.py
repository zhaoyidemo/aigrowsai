"""真实脚本生成 Provider；只依赖 OpenRouter 的 OpenAI 兼容接口。"""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import ValidationError

from qijia_video.contracts import (
    ProviderUsageRecord,
    ScriptDraft,
    ScriptReview,
    SourceCard,
    StoryboardPlan,
    StoryboardShot,
    content_hash,
    timestamp,
)
from qijia_video.errors import ProviderUnavailable
from qijia_video.prompts import (
    DEFAULT_SCRIPT_PROMPT,
    SCRIPT_OUTPUT_CONTRACT,
    narration_char_count,
)


SCRIPT_PROMPT_VERSION = "qijia_script_v11_reference_normalization"
STORYBOARD_PROMPT_VERSION = "qijia_storyboard_v7_variable_images"
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
                    "visual_intent": {"type": "string"},
                    "first_frame_prompt": {"type": "string"},
                    "motion_prompt": {"type": "string"},
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

_STORYBOARD_FALLBACKS = (
    {
        "role": "冲突钩子",
        "first_frame_prompt": (
            "同一组虚构东亚家庭成员，傍晚的家庭学习区，孩子面对尚未完成的任务，"
            "家长俯身伸手准备接管，孩子身体微微后退；中景双人构图，前景手势形成张力，"
            "暖色侧光，底部留出字幕安全区，画面中无文字"
        ),
        "motion_prompt": (
            "开场即发生动作：家长伸出的手立即在半空停住，孩子马上抬眼回应；"
            "镜头克制推近，在前两秒内让冲突关系清楚"
        ),
    },
    {
        "role": "情绪特写",
        "first_frame_prompt": (
            "延续同一家庭、服装和学习区，孩子低头握住手中的物件，家长的手停在前景；"
            "近景聚焦孩子含蓄而复杂的表情，前中后景清楚，柔和窗光，画面中无文字"
        ),
        "motion_prompt": "从家长停住的手缓慢推进到孩子的眼神，保持静态呼吸感",
    },
    {
        "role": "心理机制",
        "first_frame_prompt": (
            "延续同一家庭空间，前景是家长逐渐收回的手，中景留出一块让孩子自己尝试的"
            "空间，孩子专注处理眼前任务；门框与桌沿形成温和边界，层次分明，画面中无文字"
        ),
        "motion_prompt": "镜头从前景的手缓慢横移到独立尝试的孩子，轻微景深变化",
    },
    {
        "role": "行为改变",
        "first_frame_prompt": (
            "延续同一人物、服装与光线，家长退后半步并放松双手，孩子重新抬头准备自己"
            "完成下一步；双人中景，空间关系从压迫转为支持，画面中无文字"
        ),
        "motion_prompt": (
            "家长自然退后并放下手，孩子开始完成一个明确动作；镜头轻缓跟随孩子"
        ),
    },
    {
        "role": "结果回报",
        "first_frame_prompt": (
            "延续同一家庭故事，孩子完成任务后抬头与家长对视，家长站在稍远处露出克制的"
            "认可表情；室内暖光变得开阔，构图有余韵，底部留出字幕安全区，画面中无文字"
        ),
        "motion_prompt": (
            "孩子完成最后一个动作并抬头，家长轻轻点头；镜头缓慢拉远呈现更松弛的空间"
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
            f"{fallback['role']}：用连续家庭互动承载本段观点的语义转折"
        )
        normalized.append({
            "segment_id": segment.id,
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
        note="；".join(item for item in (note, missing_cost_note) if item),
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
) -> dict:
    usage_id = f"usage_{uuid.uuid4().hex}"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": "Qijia AI Video Workbench",
    }
    payload = {
        "model": model,
        "messages": messages,
        # GPT-5 reasoning tokens share this ceiling with the visible answer.
        # Low effort preserves enough budget for the complete JSON document.
        "reasoning": {"effort": "low", "exclude": True},
        "max_completion_tokens": max_completion_tokens,
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
        return _json_object(content)
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
        timeout_seconds: float = 120.0,
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
            f"家长问题：{card.parent_question}\n"
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

    async def generate_with_usage(
        self,
        card: SourceCard,
        prompt: str | None = None,
        *,
        on_usage: UsageRecorder | None = None,
    ) -> ScriptDraft:
        if not self.configured:
            raise ProviderUnavailable(
                "真实脚本生成未配置：请设置 OPENROUTER_API_KEY"
            )
        messages = [
            {
                "role": "system",
                "content": (
                    "你是面向家长的教育与心理学短视频主编。"
                    "严格遵守来源边界，只返回有效 JSON。"
                ),
            },
            {"role": "user", "content": self._prompt(card, prompt)},
        ]
        generated = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=messages,
            label="脚本生成",
            schema_name="qijia_script_draft_v2",
            response_schema=_SCRIPT_RESPONSE_SCHEMA,
            max_completion_tokens=4800,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation="script_generation",
            on_usage=on_usage,
        )

        return self._script_from_generated(card, generated)

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
        timeout_seconds: float = 120.0,
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
            f"把下面的完整脚本设计成一条连续的竖屏家庭微故事。脚本已经被确定性地分成 {shot_count} 个"
            "视觉章节，每章可能承载一个或多个相邻叙事段；不得遗漏任何叙事段。所有章节使用同一组虚构"
            "东亚家庭成员，人物外貌、年龄、发型、服装、家庭空间、光线和配色保持一致，"
            "但每个章节仍须单独成立、无需依赖字幕才能看懂。\n\n"
            "各章媒介已经根据真实旁白时长确定，不得自行更改："
            + "；".join(
                f"第 {index} 章为 {visual_type}"
                for index, visual_type in enumerate(visual_types, 1)
            )
            + "。image 章节要设计有景深、可供缓慢推进或横移的静态构图；video 章节"
            "只安排一个清楚可信的动作和一种克制运镜。\n\n"
            "同一个叙事段可能连续分配给多个视觉章节；这些章节必须沿时间顺序设计成"
            "不同动作阶段、景别或视角，彼此承接但不得重复同一构图。\n\n"
            "第一章同时承担抖音开场：首帧就要看见正在发生的冲突、反常识结果或关键选择，"
            "不能先给空镜、人物入场或环境介绍。动作从第一帧立即开始，前 2 秒内让人物关系"
            "和矛盾一眼可懂，前 5 秒内通过动作反应或构图变化再提供一层新信息；画面不能依赖"
            "字幕解释，也不能用夸张惊吓、焦虑表演或虚假危机吸引注意。\n\n"
            "优先使用每段给出的 visual_direction，把同一章内相邻段落合并成一个清楚、可拍摄的视觉动作。"
            "on_screen_text 由 Remotion 后期叠加，只帮助理解编辑意图，绝不能让图片或视频模型生成它。\n\n"
            "first_frame_prompt 只写首帧能看见的主体、动作起点、场景、前中后景、构图、"
            "光线和色彩；motion_prompt 对图片章节写适合 Remotion 的取景方向，对视频章节"
            "只写从首帧开始的自然动作、镜头运动和结尾状态。不得要求出现文字、字幕、"
            "书写内容、Logo、界面、名人或真实品牌，避免逐字图解口播。统一基础风格只是"
            "全片视觉边界，各镜头字段不要机械重复它；如果基础风格声明参考图优先，就不要"
            "另行发明艺术媒介、配色、材质或人物造型。\n\n"
            "严格复制下面 JSON 骨架，只填写三个空字符串字段；不得新增、删除、合并或"
            "重排 shots，不得修改 segment_id 或 beat_ids。最终只返回这一个 JSON 对象：\n"
            f"{output_skeleton}\n\n"
            f"【统一基础风格】{base_style}\n"
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
        generated = await _openrouter_json_request(
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是教育与心理学短视频的分镜导演。"
                        "只返回符合要求的 JSON，不在画面中设计任何可读文字。"
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            label="分镜生成",
            schema_name="qijia_storyboard_v3",
            response_schema=_STORYBOARD_RESPONSE_SCHEMA,
            max_completion_tokens=8000,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation="storyboard_generation",
            on_usage=on_usage,
        )
        raw_shots = _normalize_storyboard_rows(
            generated.get("shots"), target_segments
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
