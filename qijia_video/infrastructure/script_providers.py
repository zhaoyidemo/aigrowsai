"""真实脚本与导演 Provider；生产链使用 DGrid 的兼容接口。"""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from qijia_video.contracts import (
    AssetBible,
    CreativeBrief,
    DirectorReview,
    DirectorTreatment,
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
    storyboard_review_hash,
    timestamp,
)
from qijia_video.director_prompting import assert_provider_neutral_runtime_prompt
from qijia_video.errors import ProviderUnavailable, UsageLedgerUnavailable
from qijia_video.infrastructure.dgrid_gateway import (
    DGRID_DEFAULT_BASE_URL,
    dgrid_billing_snapshot,
    dgrid_headers,
    dgrid_model_access,
    dgrid_request_id,
)
from qijia_video.model_registry import (
    PRODUCTION_TEXT_INPUT_USD_PER_MILLION,
    PRODUCTION_TEXT_MODEL,
    PRODUCTION_TEXT_OUTPUT_USD_PER_MILLION,
)
from qijia_video.prompt_orchestration import compile_legacy_h3_script_prompt
from qijia_video.prompts import (
    DIRECT_SCRIPT_OUTPUT_CONTRACT,
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
DIRECT_SCRIPT_PROMPT_VERSION = 'qijia_script_v16_direct_draft'
QUALITY_SCRIPT_PROMPT_VERSION = 'qijia_script_v23_editorial_collaboration'
STORYBOARD_PROMPT_VERSION = "qijia_storyboard_v12_semantic_adaptive"
DIRECTOR_PROMPT_VERSION = 'qijia_director_v13_shot_context_ir'
DIRECTOR_V3_PROMPT_VERSION = 'qijia_director_v20_concrete_event'
DIRECTOR_QUALITY_PROMPT_VERSION = 'qijia_director_v34_independent_review'
OPENROUTER_REASONING_EFFORT = "high"
OPENROUTER_REQUEST_TIMEOUT_SECONDS = 600.0
SCRIPT_MAX_COMPLETION_TOKENS = 48_000
STORYBOARD_MAX_COMPLETION_TOKENS = 128_000
UsageRecorder = Callable[[ProviderUsageRecord], Awaitable[None]]
ModelT = TypeVar('ModelT', bound=BaseModel)


def _completion_limit_key(model: str) -> str:
    """Use the documented Chat Completions token-limit field."""

    # Keep the argument for the request-builder contract and future models.
    del model
    return "max_tokens"


def _openrouter_completion_limit_key(model: str) -> str:
    """Compatibility alias for historical tests and adapters."""

    return _completion_limit_key(model)


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

_DIRECT_SCRIPT_RESPONSE_SCHEMA = json.loads(json.dumps(_SCRIPT_RESPONSE_SCHEMA))
_DIRECT_SCRIPT_RESPONSE_SCHEMA['properties'].pop('creative_brief')
_DIRECT_SCRIPT_RESPONSE_SCHEMA['required'] = [
    item
    for item in _DIRECT_SCRIPT_RESPONSE_SCHEMA['required']
    if item != 'creative_brief'
]

_DIRECTOR_QUALITY_SCORE_KEYS = (
    'script_fidelity',
    'visual_thesis_execution',
    'event_specificity',
    'narrative_progression',
    'continuity',
    'camera_readability',
    'media_discipline',
    'producibility',
)


_SCRIPT_EDITOR_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'preserve': {
            'type': 'array',
            'maxItems': 4,
            'items': {'type': 'string'},
        },
        'improvements': {
            'type': 'array',
            'maxItems': 6,
            'items': {'type': 'string'},
        },
    },
    'required': ['preserve', 'improvements'],
    'additionalProperties': False,
}


def _editorial_texts(value: Any, *, limit: int) -> list[str]:
    """Normalize useful editor prose without turning its shape into a gate."""

    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            issue = str(item.get('issue') or '').strip()
            instruction = str(
                item.get('instruction')
                or item.get('suggestion')
                or item.get('text')
                or ''
            ).strip()
            text = (
                f'{issue}：{instruction}'
                if issue and instruction
                else instruction or issue
            )
        else:
            continue
        normalized = re.sub(r'\s+', ' ', text).strip()[:600]
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _normalized_script_editor_feedback(payload: Any) -> dict[str, list[str]]:
    """Accept partial or provider-deviant advice; editor output is advisory."""

    if not isinstance(payload, dict):
        return {'preserve': [], 'improvements': []}
    preserve = _editorial_texts(
        payload.get('preserve')
        or payload.get('keep')
        or payload.get('strengths'),
        limit=4,
    )
    improvements = _editorial_texts(
        payload.get('improvements')
        or payload.get('revision_requests')
        or payload.get('suggestions')
        or payload.get('editor_notes'),
        limit=6,
    )
    return {'preserve': preserve, 'improvements': improvements}


def _director_review_response_schema(chapter_ids: list[str]) -> dict:
    """Bind semantic review findings to the exact locked chapter slots."""

    score_keys = _DIRECTOR_QUALITY_SCORE_KEYS
    return {
        'type': 'object',
        'properties': {
            'verdict': {'type': 'string', 'enum': ['pass', 'revise']},
            'quality_scores': {
                'type': 'object',
                'properties': {
                    key: {'type': 'integer', 'minimum': 1, 'maximum': 10}
                    for key in score_keys
                },
                'required': list(score_keys),
                'additionalProperties': False,
            },
            'strengths': {
                'type': 'array',
                'maxItems': 5,
                'items': {'type': 'string'},
            },
            'revision_requests': {
                'type': 'array',
                'maxItems': 10,
                'items': {
                    'type': 'object',
                    'properties': {
                        'priority': {
                            'type': 'string',
                            'enum': ['critical', 'important', 'polish'],
                        },
                        'chapter_id': {
                            'type': 'string',
                            'enum': list(chapter_ids),
                        },
                        'issue': {'type': 'string'},
                        'instruction': {'type': 'string'},
                    },
                    'required': [
                        'priority',
                        'chapter_id',
                        'issue',
                        'instruction',
                    ],
                    'additionalProperties': False,
                },
            },
        },
        'required': [
            'verdict',
            'quality_scores',
            'strengths',
            'revision_requests',
        ],
        'additionalProperties': False,
    }


def _validated_review_payload(
    payload: dict,
    *,
    label: str,
    score_keys: tuple[str, ...],
    verdicts: set[str],
    chapter_ids: set[str] | None = None,
) -> dict:
    """Validate Director review output that can trigger a bounded repair."""

    expected_fields = {
        'verdict',
        'quality_scores',
        'strengths',
        'revision_requests',
    }
    try:
        if not isinstance(payload, dict) or set(payload) != expected_fields:
            raise ValueError('root fields')
        if payload.get('verdict') not in verdicts:
            raise ValueError('verdict')

        scores = payload.get('quality_scores')
        if not isinstance(scores, dict) or set(scores) != set(score_keys):
            raise ValueError('quality score fields')
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= 10
            for value in scores.values()
        ):
            raise ValueError('quality score values')

        strengths = payload.get('strengths')
        if (
            not isinstance(strengths, list)
            or len(strengths) > 5
            or any(
                not isinstance(item, str) or not item.strip()
                for item in strengths
            )
        ):
            raise ValueError('strengths')

        revisions = payload.get('revision_requests')
        if not isinstance(revisions, list) or len(revisions) > 10:
            raise ValueError('revision requests')
        revision_fields = {'priority', 'issue', 'instruction'}
        if chapter_ids is not None:
            revision_fields.add('chapter_id')
        for item in revisions:
            if not isinstance(item, dict) or set(item) != revision_fields:
                raise ValueError('revision request fields')
            if item.get('priority') not in {
                'critical',
                'important',
                'polish',
            }:
                raise ValueError('revision priority')
            if (
                chapter_ids is not None
                and item.get('chapter_id') not in chapter_ids
            ):
                raise ValueError('revision chapter')
            if any(
                not isinstance(item.get(key), str) or not item[key].strip()
                for key in ('issue', 'instruction')
            ):
                raise ValueError('revision text')
        has_blocking_revision = any(
            item['priority'] in {'critical', 'important'}
            for item in revisions
        )
        if payload['verdict'] == 'pass' and has_blocking_revision:
            raise ValueError('pass with blocking revision')
        if (
            chapter_ids is not None
            and payload['verdict'] == 'revise'
            and not has_blocking_revision
        ):
            raise ValueError('director revise without blocking revision')

    except (KeyError, TypeError, ValueError) as exc:
        raise ProviderUnavailable(
            f'{label}返回内容不符合质量审查契约'
        ) from exc
    return payload


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

_SCHEMA_ANNOTATION_KEYS = {'default', 'description', 'examples', 'title'}


def _inline_schema_refs(value: Any, definitions: dict[str, Any]) -> Any:
    """Inline Pydantic definitions and remove non-contract annotations."""

    if isinstance(value, list):
        return [_inline_schema_refs(item, definitions) for item in value]
    if not isinstance(value, dict):
        return value
    reference = value.get('$ref')
    if isinstance(reference, str) and reference.startswith('#/$defs/'):
        name = reference.removeprefix('#/$defs/')
        resolved = deepcopy(definitions[name])
        resolved.update({key: item for key, item in value.items() if key != '$ref'})
        return _inline_schema_refs(resolved, definitions)
    return {
        key: _inline_schema_refs(item, definitions)
        for key, item in value.items()
        if key not in _SCHEMA_ANNOTATION_KEYS
    }


def _domain_output_schema(
    model_type: type[BaseModel],
    required_fields: tuple[str, ...],
) -> dict:
    """Compile provider JSON Schema from the canonical domain contract."""

    source = model_type.model_json_schema()
    definitions = dict(source.get('$defs') or {})
    properties = dict(source.get('properties') or {})
    return {
        'type': 'object',
        'properties': {
            field: _inline_schema_refs(deepcopy(properties[field]), definitions)
            for field in required_fields
        },
        'required': list(required_fields),
        'additionalProperties': False,
    }


_VISUAL_BIBLE_FIELDS = (
    'core_visual_idea',
    'visual_world',
    'recurring_subjects',
    'scene_anchors',
    'continuity_rules',
    'color_material_system',
    'composition_system',
    'reference_strategy',
    'forbidden_elements',
)
_DIRECTOR_TREATMENT_FIELDS = (
    'visual_thesis',
    'audience_experience',
    'chapter_progression',
    'motif_system',
    'rhythm_strategy',
    'edit_pattern',
    'style_application',
)
_ASSET_BIBLE_FIELDS = (
    'subjects',
    'locations',
    'props',
    'identity_locks',
    'material_locks',
    'allowed_variations',
    'motion_grammar',
    'review_criteria',
    'references',
)

_VISUAL_BIBLE_RESPONSE_SCHEMA = _domain_output_schema(
    VisualBible,
    _VISUAL_BIBLE_FIELDS,
)
_DIRECTOR_TREATMENT_SCHEMA = _domain_output_schema(
    DirectorTreatment,
    _DIRECTOR_TREATMENT_FIELDS,
)
_ASSET_BIBLE_SCHEMA = _domain_output_schema(
    AssetBible,
    _ASSET_BIBLE_FIELDS,
)

_DIRECTOR_TREATMENT_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'director_treatment': _DIRECTOR_TREATMENT_SCHEMA,
        'visual_bible': _VISUAL_BIBLE_RESPONSE_SCHEMA,
        'asset_bible': _ASSET_BIBLE_SCHEMA,
    },
    'required': ['director_treatment', 'visual_bible', 'asset_bible'],
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

_SHOT_CONTEXT_V3_FIELDS = (
    'semantic_goal',
    'concrete_event',
    'blocking',
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
)
_SHOT_CONTEXT_V3_RESPONSE_SCHEMA = _domain_output_schema(
    ShotContextIR,
    _SHOT_CONTEXT_V3_FIELDS,
)
for _required_event_field in ('concrete_event', 'blocking'):
    _SHOT_CONTEXT_V3_RESPONSE_SCHEMA['properties'][_required_event_field][
        'minLength'
    ] = 1
_SHOT_CONTEXT_V3_RESPONSE_SCHEMA['properties']['reference_roles']['items'] = {
    'type': 'string',
    'enum': [
        'identity',
        'wardrobe',
        'object',
        'location',
        'style',
        'composition',
    ],
}

_DIRECTOR_V3_RESPONSE_SCHEMA = {
    'type': 'object',
    'properties': {
        'visual_bible': _VISUAL_BIBLE_RESPONSE_SCHEMA,
        'shots': {
            'type': 'array',
            'minItems': 3,
            'maxItems': 12,
            'items': {
                'type': 'object',
                'properties': {
                    'beat_ids': {
                        'type': 'array',
                        'minItems': 1,
                        'maxItems': 8,
                        'items': {'type': 'string'},
                    },
                    'visual_type': {
                        'type': 'string',
                        'enum': ['image', 'video'],
                    },
                    'context': _SHOT_CONTEXT_V3_RESPONSE_SCHEMA,
                },
                'required': ['beat_ids', 'visual_type', 'context'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['visual_bible', 'shots'],
    'additionalProperties': False,
}


def _director_treatment_response_schema(max_chapters: int) -> dict:
    """Bind the Director's chapter decision to the available semantic beats."""

    schema = deepcopy(_DIRECTOR_TREATMENT_RESPONSE_SCHEMA)
    progression = schema['properties']['director_treatment']['properties'][
        'chapter_progression'
    ]
    progression['minItems'] = 3
    progression['maxItems'] = max(3, min(10, int(max_chapters)))
    return schema


def _director_shot_plan_response_schema(chapter_ids: list[str]) -> dict:
    """Require one and only one structured payload for every locked chapter."""

    shot_schema = _DIRECTOR_V3_RESPONSE_SCHEMA['properties']['shots']['items']
    return {
        'type': 'object',
        'properties': {
            'chapters': {
                'type': 'object',
                'properties': {
                    chapter_id: deepcopy(shot_schema)
                    for chapter_id in chapter_ids
                },
                'required': list(chapter_ids),
                'additionalProperties': False,
            },
        },
        'required': ['chapters'],
        'additionalProperties': False,
    }


def _validation_error_fields(exc: ValidationError) -> str:
    """Return bounded field/type diagnostics without echoing model content."""

    fields: list[str] = []
    for item in exc.errors(include_input=False, include_url=False)[:8]:
        location = '.'.join(str(part) for part in item.get('loc') or ())
        error_type = str(item.get('type') or 'validation_error')
        fields.append(f'{location or "root"}:{error_type}')
    return '、'.join(fields) or 'root:validation_error'


def _validate_director_artifact(
    model_type: type[ModelT],
    raw: Any,
    metadata: dict[str, Any],
    *,
    artifact_name: str,
) -> ModelT:
    """Validate one Director artifact with safe, actionable diagnostics."""

    if not isinstance(raw, dict):
        raise ProviderUnavailable(
            f'Director 第一阶段的 {artifact_name} 不是 JSON 对象'
        )
    try:
        return model_type.model_validate({**raw, **metadata})
    except ValidationError as exc:
        raise ProviderUnavailable(
            f'Director 第一阶段的 {artifact_name} 不符合领域契约'
            f'（fields={_validation_error_fields(exc)}）'
        ) from exc


@dataclass(frozen=True)
class _OpenRouterJsonResponse:
    data: dict
    model_id: str


@dataclass(frozen=True)
class _OpenRouterErrorContext:
    status_code: int
    message: str
    model: str
    request_id: str = ""
    generation_id: str = ""
    error_code: str = ""
    error_type: str = ""
    provider_code: str = ""
    provider_name: str = ""
    model_slug: str = ""
    route_summary: str = ""
    region: str = ""
    route_attempt: int | None = None
    endpoint_total: int | None = None
    candidate_providers: tuple[str, ...] = ()
    selected_providers: tuple[str, ...] = ()
    attempt_summaries: tuple[str, ...] = ()
    pipeline_stages: tuple[str, ...] = ()
    model_access: str = ""
    reasons: tuple[str, ...] = ()
    guardrail_blocked: bool = False

    def diagnostic_suffix(self) -> str:
        parts = [f"model={self.model}"]
        if self.error_code:
            parts.append(f"error_code={self.error_code}")
        if self.error_type:
            parts.append(f"error_type={self.error_type}")
        if self.provider_code:
            parts.append(f"provider_code={self.provider_code}")
        if self.provider_name:
            parts.append(f"provider={self.provider_name}")
        if self.model_slug and self.model_slug != self.model:
            parts.append(f"model_slug={self.model_slug}")
        if self.route_summary:
            parts.append(f"route={self.route_summary}")
        if self.region:
            parts.append(f"region={self.region}")
        if self.route_attempt is not None:
            parts.append(f"attempt={self.route_attempt}")
        if self.endpoint_total is not None:
            parts.append(f"endpoints={self.endpoint_total}")
        if self.candidate_providers:
            parts.append("candidates=" + "|".join(self.candidate_providers))
        if self.endpoint_total is not None:
            parts.append(
                "selected="
                + ("|".join(self.selected_providers) or "none")
            )
        if self.attempt_summaries:
            parts.append("attempts=" + "|".join(self.attempt_summaries))
        if self.pipeline_stages:
            parts.append("pipeline=" + "|".join(self.pipeline_stages))
        if self.model_access:
            parts.append(f"model_access={self.model_access}")
        if self.reasons:
            parts.append("reasons=" + "|".join(self.reasons))
        if self.request_id:
            parts.append(f"request_id={self.request_id}")
        if self.generation_id:
            parts.append(f"generation_id={self.generation_id}")
        return "（" + "，".join(parts) + "）"

    def ledger_note(self, provider_label: str = "OpenRouter") -> str:
        return (
            f"{provider_label} 失败诊断" + self.diagnostic_suffix()
        )[:480]


class _OpenRouterRequestError(ProviderUnavailable):
    def __init__(self, label: str, context: _OpenRouterErrorContext):
        self.context = context
        if context.guardrail_blocked:
            classification = "请求在 OpenRouter Guardrail 阶段被拦截"
        elif context.model_access == "not_listed_for_key":
            classification = "同一 API Key 的可用模型目录未列出该模型"
        elif (
            context.status_code == 403
            and context.route_attempt == 0
            and bool(context.endpoint_total)
            and not context.selected_providers
        ):
            classification = (
                "OpenRouter 在选择任何上游前拒绝了当前模型请求；"
                "系统未切换或调用备用模型"
            )
        elif context.model_access == "listed_for_key":
            classification = "同一 API Key 已识别该模型，但当前请求仍被拒绝"
        else:
            classification = "OpenRouter 拒绝了当前请求"
        super().__init__(
            f"OpenRouter {label}返回 HTTP {context.status_code}："
            f"{classification}；{context.message[:300]}"
            f"{context.diagnostic_suffix()}"
        )


class _DGridRequestError(ProviderUnavailable):
    def __init__(self, label: str, context: _OpenRouterErrorContext):
        self.context = context
        if context.status_code == 401:
            classification = "API Key 未通过认证"
        elif context.model_access == "not_listed_for_key":
            classification = "当前 API Key 的模型目录未列出该模型"
        elif context.model_access == "listed_for_key":
            classification = "当前 API Key 已识别该模型，但请求参数或上游仍被拒绝"
        elif context.status_code == 429:
            classification = "请求达到 DGrid 速率或余额限制"
        else:
            classification = "DGrid 拒绝了当前单模型请求"
        super().__init__(
            f"DGrid {label}返回 HTTP {context.status_code}："
            f"{classification}；{context.message[:300]}"
            f"{context.diagnostic_suffix()}"
        )


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


def _gateway_api_url(base_url: str, path: str, *, gateway: str) -> str:
    default_url = (
        DGRID_DEFAULT_BASE_URL
        if gateway == "dgrid"
        else "https://openrouter.ai/api"
    )
    base = str(base_url or default_url).rstrip("/")
    if base.endswith("/v1"):
        root = base
    elif base.endswith("/api"):
        root = base + "/v1"
    elif gateway == "dgrid":
        root = base + "/v1"
    else:
        root = base + "/api/v1"
    return root + "/" + path.lstrip("/")


def _chat_url(base_url: str, *, gateway: str = "openrouter") -> str:
    return _gateway_api_url(base_url, "chat/completions", gateway=gateway)


def _models_user_url(base_url: str) -> str:
    return _gateway_api_url(base_url, "models/user", gateway="openrouter")


def _models_url(base_url: str, *, gateway: str) -> str:
    return _gateway_api_url(base_url, "models", gateway=gateway)


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


def _diagnostic_text(value: Any, max_length: int = 160) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    return " ".join(str(value).split())[:max_length]


def _diagnostic_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _unique_diagnostics(values: list[str], *, limit: int = 8) -> tuple[str, ...]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
        if len(unique) >= limit:
            break
    return tuple(unique)


def _openrouter_error_context(
    body: Any,
    *,
    status_code: int,
    request_id: str,
    generation_id: str,
    model: str,
    fallback_message: str,
    model_access: str = "",
) -> _OpenRouterErrorContext:
    payload = body if isinstance(body, dict) else {}
    raw_error = payload.get("error")
    error = raw_error if isinstance(raw_error, dict) else {}
    raw_metadata = error.get("metadata")
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    raw_router = payload.get("openrouter_metadata")
    router = raw_router if isinstance(raw_router, dict) else {}
    raw_pipeline = router.get("pipeline")
    pipeline = raw_pipeline if isinstance(raw_pipeline, list) else []
    guardrail_blocked = bool(metadata.get("patterns"))
    pipeline_stages: list[str] = []
    for raw_stage in pipeline:
        stage = raw_stage if isinstance(raw_stage, dict) else {}
        stage_type = _diagnostic_text(stage.get("type"), 40)
        name = _diagnostic_text(stage.get("name"), 60)
        raw_data = stage.get("data")
        data = raw_data if isinstance(raw_data, dict) else {}
        action = _diagnostic_text(data.get("action"), 40).casefold()
        summary = _diagnostic_text(stage.get("summary"), 100)
        stage_parts = [item for item in (stage_type, name, action or summary) if item]
        if stage_parts:
            pipeline_stages.append(":".join(stage_parts))
        if stage_type != "guardrail":
            continue
        if (
            action in {"block", "blocked", "deny", "denied"}
            or "block" in summary.casefold()
        ):
            guardrail_blocked = True
    raw_reasons = metadata.get("reasons")
    reasons = tuple(
        item
        for item in (
            _diagnostic_text(value, 100)
            for value in (raw_reasons if isinstance(raw_reasons, list) else [])[:3]
        )
        if item
    )
    provider_name = _diagnostic_text(metadata.get("provider_name"), 80)
    raw_attempts = router.get("attempts")
    attempts = raw_attempts if isinstance(raw_attempts, list) else []
    attempt_summaries: list[str] = []
    for raw_attempt in attempts:
        attempt = raw_attempt if isinstance(raw_attempt, dict) else {}
        attempt_provider = _diagnostic_text(attempt.get("provider"), 80)
        attempt_model = _diagnostic_text(attempt.get("model"), 120)
        attempt_status = _diagnostic_text(attempt.get("status"), 20)
        identity = attempt_provider or attempt_model
        if identity:
            attempt_summaries.append(
                identity + (f":{attempt_status}" if attempt_status else "")
            )
            provider_name = provider_name or attempt_provider
    raw_endpoints = router.get("endpoints")
    endpoints = raw_endpoints if isinstance(raw_endpoints, dict) else {}
    raw_available = endpoints.get("available")
    available = raw_available if isinstance(raw_available, list) else []
    candidate_providers: list[str] = []
    selected_providers: list[str] = []
    for raw_endpoint in available:
        endpoint = raw_endpoint if isinstance(raw_endpoint, dict) else {}
        endpoint_provider = _diagnostic_text(endpoint.get("provider"), 80)
        if endpoint_provider:
            candidate_providers.append(endpoint_provider)
            if endpoint.get("selected") is True:
                selected_providers.append(endpoint_provider)
    endpoint_total = (
        _diagnostic_int(endpoints.get("total"))
        if endpoints
        else None
    )
    if endpoint_total is None and available:
        endpoint_total = len(available)
    return _OpenRouterErrorContext(
        status_code=int(status_code),
        message=_diagnostic_text(
            error.get("message")
            or payload.get("message")
            or raw_error
            or fallback_message,
            500,
        ) or "未知上游错误",
        model=model,
        request_id=request_id,
        generation_id=generation_id,
        error_code=_diagnostic_text(error.get("code"), 80),
        error_type=_diagnostic_text(
            metadata.get("error_type")
            or error.get("error_type")
            or payload.get("error_type"),
            80,
        ),
        provider_code=_diagnostic_text(metadata.get("provider_code"), 100),
        provider_name=provider_name,
        model_slug=_diagnostic_text(metadata.get("model_slug"), 120),
        route_summary=_diagnostic_text(router.get("summary"), 160),
        region=_diagnostic_text(router.get("region"), 40),
        route_attempt=_diagnostic_int(router.get("attempt")),
        endpoint_total=endpoint_total,
        candidate_providers=_unique_diagnostics(candidate_providers),
        selected_providers=_unique_diagnostics(selected_providers),
        attempt_summaries=_unique_diagnostics(attempt_summaries),
        pipeline_stages=_unique_diagnostics(pipeline_stages),
        model_access=model_access,
        reasons=reasons,
        guardrail_blocked=guardrail_blocked,
    )


async def _openrouter_model_access_diagnostic(
    *,
    api_key: str,
    base_url: str,
    model: str,
    timeout_seconds: float,
    transport: httpx.AsyncBaseTransport | None,
) -> str:
    """Check the same key's filtered model catalog without another generation."""

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-OpenRouter-Title": "Qijia AI Video Workbench",
    }
    timeout = min(15.0, max(5.0, float(timeout_seconds)))
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            transport=transport,
        ) as client:
            response = await client.get(
                _models_user_url(base_url),
                headers=headers,
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
    available_ids: set[str] = set()
    for raw_item in raw_models:
        item = raw_item if isinstance(raw_item, dict) else {}
        for field in ("id", "canonical_slug"):
            value = _diagnostic_text(item.get(field), 256)
            if value:
                available_ids.add(value)
        raw_alias = item.get("alias_target")
        alias = raw_alias if isinstance(raw_alias, dict) else {}
        alias_slug = _diagnostic_text(alias.get("slug"), 256)
        if alias_slug:
            available_ids.add(alias_slug)
    return (
        "listed_for_key"
        if model in available_ids
        else "not_listed_for_key"
    )


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _gateway_usage_record(
    body: dict | None,
    *,
    gateway: str,
    usage_id: str,
    operation: str,
    requested_model: str,
    billing_snapshot: dict[str, Any] | None = None,
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
    billing = (
        billing_snapshot if isinstance(billing_snapshot, dict) else {}
    )
    raw_cost = (
        billing.get("total_cost")
        if "total_cost" in billing
        else usage.get("cost")
    )
    try:
        reported_cost = max(0.0, float(raw_cost)) if raw_cost is not None else None
    except (TypeError, ValueError):
        reported_cost = None
    input_tokens = _nonnegative_int(
        billing.get("input_tokens")
        or usage.get("prompt_tokens")
        or usage.get("input_tokens")
    )
    output_tokens = _nonnegative_int(
        billing.get("output_tokens")
        or usage.get("completion_tokens")
        or usage.get("output_tokens")
    )
    total_tokens = _nonnegative_int(
        usage.get("total_tokens") or input_tokens + output_tokens
    )
    estimated_cost = None
    if (
        gateway == "dgrid"
        and reported_cost is None
        and succeeded
        and requested_model == PRODUCTION_TEXT_MODEL
        and (input_tokens or output_tokens)
    ):
        estimated_cost = round(
            (
                input_tokens * PRODUCTION_TEXT_INPUT_USD_PER_MILLION
                + output_tokens * PRODUCTION_TEXT_OUTPUT_USD_PER_MILLION
            )
            / 1_000_000,
            10,
        )
    provider_label = "DGrid" if gateway == "dgrid" else "OpenRouter"
    missing_cost_note = ""
    if reported_cost is None and estimated_cost is not None:
        missing_cost_note = (
            "DGrid billing-json 暂未返回，先按 Claude Fable 5 公开价估算"
        )
    elif reported_cost is None:
        missing_cost_note = (
            "DGrid billing-json 暂无可用金额，需到 DGrid 用量账单核对"
            if gateway == "dgrid"
            else "供应商响应未提供 usage.cost，金额需与 OpenRouter Activity 对账"
        )
    pricing_basis = ""
    if reported_cost is not None:
        pricing_basis = (
            "DGrid billing-json 不可变计费快照"
            if billing
            else f"{provider_label} 非流式响应 usage.cost 供应商回传金额"
        )
    elif estimated_cost is not None:
        pricing_basis = (
            f"Claude Fable 5 公开价 ${PRODUCTION_TEXT_INPUT_USD_PER_MILLION:g}"
            "/百万输入 tokens + "
            f"${PRODUCTION_TEXT_OUTPUT_USD_PER_MILLION:g}/百万输出 tokens；"
            "DGrid 账单优先"
        )
    cached_tokens = _nonnegative_int(
        billing.get("cache_read_tokens")
        or prompt_details.get("cached_tokens")
    )
    return ProviderUsageRecord(
        usage_id=usage_id,
        operation=operation,
        provider=gateway,
        model_id=str(
            billing.get("model") or payload.get("model") or requested_model
        ),
        request_id=str(
            request_id
            if gateway == "dgrid"
            else payload.get("id") or request_id
        ),
        succeeded=bool(succeeded),
        http_status_code=http_status_code,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=_nonnegative_int(
            completion_details.get("reasoning_tokens")
        ),
        quantity=1,
        unit="request",
        reported_cost=reported_cost,
        reported_currency="USD" if reported_cost is not None else None,
        estimated_cost=estimated_cost,
        estimated_currency="USD" if estimated_cost is not None else None,
        pricing_basis=pricing_basis,
        note="；".join(
            item
            for item in (
                note,
                missing_cost_note,
            )
            if item
        )[:500],
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
        raise UsageLedgerUnavailable(
            "模型调用已经发生，但成本账本无法持久化；流程已停止"
        ) from exc


async def _gateway_json_request(
    *,
    gateway: str,
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
    provider_label = "DGrid" if gateway == "dgrid" else "OpenRouter"
    headers = (
        dgrid_headers(api_key, title="Qijia AI Video Workbench")
        if gateway == "dgrid"
        else {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "X-OpenRouter-Title": "Qijia AI Video Workbench",
            "X-OpenRouter-Metadata": "enabled",
        }
    )
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": schema_name,
                "strict": True,
                "schema": response_schema,
            },
        },
    }
    if gateway == "openrouter":
        payload["reasoning"] = {
            "effort": reasoning_effort,
            "exclude": True,
        }
        payload["provider"] = {"require_parameters": True}
    completion_limit_key = _completion_limit_key(model)
    payload[completion_limit_key] = max_completion_tokens
    if tools:
        payload["tools"] = tools
    if tools and tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if gateway == "openrouter" and max_tool_calls is not None:
        payload["max_tool_calls"] = max(1, int(max_tool_calls))
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds, connect=20.0),
        transport=transport,
    ) as client:
        try:
            response = await client.post(
                _chat_url(base_url, gateway=gateway),
                headers=headers,
                json=payload,
            )
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            await _record_usage(on_usage, _gateway_usage_record(
                None,
                gateway=gateway,
                usage_id=usage_id,
                operation=operation,
                requested_model=model,
                succeeded=False,
                note="网络异常后是否计费未知",
            ))
            raise ProviderUnavailable(
                f"{provider_label} {label}请求失败"
            ) from exc
    request_id = (
        dgrid_request_id(response)
        if gateway == "dgrid"
        else response.headers.get("x-request-id", "")
    )
    generation_id = response.headers.get("x-generation-id", "")
    trace_id = (
        request_id
        if gateway == "dgrid"
        else generation_id or request_id
    )
    try:
        body = response.json()
    except ValueError as exc:
        billing = (
            await dgrid_billing_snapshot(
                api_key=api_key,
                base_url=base_url,
                request_id=request_id,
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
            if gateway == "dgrid"
            else None
        )
        await _record_usage(on_usage, _gateway_usage_record(
            None,
            gateway=gateway,
            usage_id=usage_id,
            operation=operation,
            requested_model=model,
            billing_snapshot=billing,
            request_id=trace_id,
            http_status_code=response.status_code,
            succeeded=False,
            note="响应无法解析，是否计费需对账",
        ))
        raise ProviderUnavailable(
            f"{provider_label} {label}返回了无法读取的响应"
            + (f"；request_id={trace_id}" if trace_id else "")
        ) from exc
    if isinstance(body, dict) and not generation_id:
        generation_id = _diagnostic_text(body.get("id"), 160)
    trace_id = (
        request_id
        if gateway == "dgrid"
        else generation_id or request_id
    )
    error_context = (
        _openrouter_error_context(
            body,
            status_code=response.status_code,
            request_id=request_id,
            generation_id=generation_id,
            model=model,
            fallback_message=response.reason_phrase,
        )
        if response.status_code >= 400
        else None
    )
    if error_context:
        if gateway == "dgrid":
            model_access = await dgrid_model_access(
                api_key=api_key,
                models_url=_models_url(base_url, gateway=gateway),
                model=model,
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
            error_context = replace(error_context, model_access=model_access)
        elif (
            error_context.status_code == 403
            and not error_context.guardrail_blocked
            and not error_context.reasons
        ):
            model_access = await _openrouter_model_access_diagnostic(
                api_key=api_key,
                base_url=base_url,
                model=model,
                timeout_seconds=timeout_seconds,
                transport=transport,
            )
            error_context = replace(error_context, model_access=model_access)
    response_succeeded = bool(
        response.status_code < 400
        and isinstance(body, dict)
        and not body.get("error")
        and body.get("choices")
    )
    billing = (
        await dgrid_billing_snapshot(
            api_key=api_key,
            base_url=base_url,
            request_id=request_id,
            timeout_seconds=timeout_seconds,
            transport=transport,
        )
        if gateway == "dgrid"
        else None
    )
    await _record_usage(on_usage, _gateway_usage_record(
        body if isinstance(body, dict) else None,
        gateway=gateway,
        usage_id=usage_id,
        operation=operation,
        requested_model=model,
        billing_snapshot=billing,
        request_id=trace_id,
        http_status_code=response.status_code,
        succeeded=response_succeeded,
        note=(
            error_context.ledger_note(provider_label)
            if error_context
            else ""
        ),
    ))
    if error_context:
        if gateway == "dgrid":
            raise _DGridRequestError(label, error_context)
        raise _OpenRouterRequestError(label, error_context)
    top_level_error = body.get("error") if isinstance(body, dict) else None
    if top_level_error:
        message = (
            top_level_error.get("message")
            if isinstance(top_level_error, dict)
            else str(top_level_error)
        )
        raise ProviderUnavailable(
            f"{provider_label} {label}生成失败："
            f"{str(message or '未知上游错误')[:500]}"
            + (f"；request_id={trace_id}" if trace_id else "")
        )
    try:
        choice = body["choices"][0]
        message = choice["message"]
        content = message.get("content")
        finish_reason = str(choice.get("finish_reason") or "unknown")
    except (KeyError, IndexError, TypeError) as exc:
        raise ProviderUnavailable(
            f"{provider_label} {label}响应缺少模型结果"
            + (f"；request_id={trace_id}" if trace_id else "")
        ) from exc
    choice_error = choice.get("error")
    if choice_error:
        error_message = (
            choice_error.get("message")
            if isinstance(choice_error, dict)
            else str(choice_error)
        )
        raise ProviderUnavailable(
            f"{provider_label} {label}生成失败："
            f"{str(error_message or '未知上游错误')[:500]}"
            + (f"；request_id={trace_id}" if trace_id else "")
        )
    refusal = message.get("refusal") if isinstance(message, dict) else None
    if refusal:
        raise ProviderUnavailable(
            f"{provider_label} {label}拒绝了本次请求：{str(refusal)[:300]}"
            + (f"；request_id={trace_id}" if trace_id else "")
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
            f"{provider_label} {label}{detail}（finish_reason={finish_reason}）"
            + (f"；request_id={trace_id}" if trace_id else "")
        ) from exc


async def _openrouter_json_request(
    *,
    gateway: str = "openrouter",
    **kwargs,
) -> _OpenRouterJsonResponse:
    """Compatibility entry point with an explicit production gateway."""

    return await _gateway_json_request(gateway=gateway, **kwargs)


class OpenRouterScriptProvider:
    """Generate one reviewable screenplay with independent content tracks."""

    name = "openrouter-script"
    gateway = "openrouter"
    credential_setting = "OPENROUTER_API_KEY"
    default_base_url = "https://openrouter.ai/api"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = OPENROUTER_REQUEST_TIMEOUT_SECONDS,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or self.default_base_url).strip()
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
                f'真实脚本生成未配置：请设置 {self.credential_setting}'
            )
        user_prompt = self._prompt(
            card,
            prompt,
            output_contract=SCRIPT_SKILL_OUTPUT_CONTRACT,
        )
        response = await _openrouter_json_request(
            gateway=self.gateway,
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

    async def generate_direct_script(
        self,
        card: SourceCard,
        prompt: str,
        *,
        on_usage: UsageRecorder | None = None,
    ) -> ScriptDraft:
        """Generate the v3 deliverable without exposing a planning artifact."""

        if not self.configured:
            raise ProviderUnavailable(
                f'真实脚本生成未配置：请设置 {self.credential_setting}'
            )
        user_prompt = self._prompt(
            card,
            prompt,
            output_contract=DIRECT_SCRIPT_OUTPUT_CONTRACT,
        )
        response = await _openrouter_json_request(
            gateway=self.gateway,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    'role': 'system',
                    'content': (
                        '你是本任务唯一的 Script Skill。请在内部完成角度取舍、结构规划与审稿，'
                        '只返回最终 ScriptDraft JSON。禁止输出候选方案、中间计划、思维过程、'
                        '视觉方案、镜头或媒体提示词。'
                    ),
                },
                {'role': 'user', 'content': user_prompt},
            ],
            label='口播稿生成',
            schema_name='qijia_direct_script_draft_v1',
            response_schema=_DIRECT_SCRIPT_RESPONSE_SCHEMA,
            max_completion_tokens=SCRIPT_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation='script_generation',
            on_usage=on_usage,
        )
        return self._script_from_generated(card, response.data)

    async def generate_quality_script(
        self,
        card: SourceCard,
        prompt: str,
        *,
        on_usage: UsageRecorder | None = None,
    ) -> tuple[ScriptDraft, ScriptReview]:
        """Run a writer/editor/writer collaboration with one human quality gate."""

        if not self.configured:
            raise ProviderUnavailable(
                f'真实脚本生成未配置：请设置 {self.credential_setting}'
            )
        writer_prompt = self._prompt(
            card,
            prompt,
            output_contract=DIRECT_SCRIPT_OUTPUT_CONTRACT,
        )
        draft_response = await _openrouter_json_request(
            gateway=self.gateway,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    'role': 'system',
                    'content': (
                        '你是这篇作品的脚本主编。直接理解用户原始表达与其主动提供的'
                        '材料，完成必要取舍。写出判断鲜明、'
                        '论证持续推进、适合真实朗读的完整作品，只返回 ScriptDraft JSON。'
                    ),
                },
                {'role': 'user', 'content': writer_prompt},
            ],
            label='脚本初稿生成',
            schema_name='qijia_quality_script_draft_v1',
            response_schema=_DIRECT_SCRIPT_RESPONSE_SCHEMA,
            max_completion_tokens=SCRIPT_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation='script_draft_generation',
            on_usage=on_usage,
            reasoning_effort='xhigh',
        )
        draft = self._script_from_generated(card, draft_response.data)
        editor_prompt = (
            '阅读下面这篇口播初稿，给主编少量、具体、可执行的编辑建议。保护作品最有力量'
            '的判断和语言，不要要求中立、全面或四平八稳。重点看中心判断、论证推进、具体'
            '处境、口语节奏和套话重复。不要评分，不要给 pass/revise 裁决，不要制作风险清单，'
            '也不要重写全文。只返回 preserve 与 improvements 两组短建议。\n\n'
            + prompt
            + '\n\n【待审初稿】\n'
            + json.dumps(draft.model_dump(mode='json'), ensure_ascii=False)
        )
        editor_feedback = {'preserve': [], 'improvements': []}
        editor_model_id = ''
        editor_warning = ''
        try:
            editor_response = await _openrouter_json_request(
                gateway=self.gateway,
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                messages=[
                    {
                        'role': 'system',
                        'content': (
                            '你是服务于主编的资深脚本编辑。只提出能让作品更准确、更具体、'
                            '更有推进感的建议；你没有否决权，也不改变作者立场。'
                        ),
                    },
                    {'role': 'user', 'content': editor_prompt},
                ],
                label='脚本编辑建议',
                schema_name='qijia_script_editor_notes_v1',
                response_schema=_SCRIPT_EDITOR_RESPONSE_SCHEMA,
                max_completion_tokens=8_000,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
                operation='script_critique',
                on_usage=on_usage,
                reasoning_effort='high',
            )
            editor_feedback = _normalized_script_editor_feedback(
                editor_response.data
            )
            editor_model_id = editor_response.model_id
        except UsageLedgerUnavailable:
            raise
        except ProviderUnavailable:
            editor_warning = (
                '编辑建议本次不可用；主编已依据原始请求和冻结的脚本方法独立完成终稿'
            )
        revision_prompt = (
            prompt
            + '\n\n【你的初稿】\n'
            + json.dumps(draft.model_dump(mode='json'), ensure_ascii=False)
            + '\n\n【编辑建议｜仅供主编判断】\n'
            + json.dumps(editor_feedback, ensure_ascii=False)
            + '\n\n你仍对终稿全权负责。只采纳确实让作品更好的建议；保留初稿中真正'
            '有力量的判断和表达。即使建议为空，也要自行完成一次扎实的终稿打磨。'
            '不要在正文解释修改过程，不要增加风险声明，只返回最终 ScriptDraft JSON。\n\n'
            + DIRECT_SCRIPT_OUTPUT_CONTRACT
        )
        final_response = await _openrouter_json_request(
            gateway=self.gateway,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    'role': 'system',
                    'content': (
                        '你仍是这篇作品唯一的脚本主编。完成最终重写，保持鲜明判断、'
                        '思想张力和自然口语；禁止输出计划、评论、视觉方案或修改说明。'
                    ),
                },
                {'role': 'user', 'content': revision_prompt},
            ],
            label='脚本主编终稿',
            schema_name='qijia_quality_script_final_v1',
            response_schema=_DIRECT_SCRIPT_RESPONSE_SCHEMA,
            max_completion_tokens=SCRIPT_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation='script_revision',
            on_usage=on_usage,
            reasoning_effort='xhigh',
        )
        final_script = self._script_from_generated(card, final_response.data)
        final_hash = content_hash(final_script)
        review = await self.review(card, final_script)
        review.strengths = list(editor_feedback['preserve'])
        review.preserve = list(editor_feedback['preserve'])
        review.reviewed_draft_hash = final_hash
        if editor_warning:
            review.warnings.append(editor_warning)
        collaborators = [
            f'{draft_response.model_id} writer',
            *([f'{editor_model_id} editor'] if editor_model_id else []),
            f'{final_response.model_id} final-writer',
        ]
        review.model_id = ' + '.join(collaborators)
        review.prompt_version = QUALITY_SCRIPT_PROMPT_VERSION
        review.input_hash = final_hash
        review.reviewed_at = timestamp()
        return final_script, review

    async def generate_with_brief(
        self,
        card: SourceCard,
        prompt: str | None = None,
        *,
        system_prompt: str | None = None,
        on_usage: UsageRecorder | None = None,
    ) -> tuple[CreativeBrief, ScriptDraft]:
        """Resume Pipeline v1 H3 jobs; v3 jobs call generate_direct_script."""

        if not self.configured:
            raise ProviderUnavailable(
                f"真实脚本生成未配置：请设置 {self.credential_setting}"
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
            gateway=self.gateway,
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
    gateway = "openrouter"
    credential_setting = "OPENROUTER_API_KEY"
    default_base_url = "https://openrouter.ai/api"

    async def generate_quality_director_plan(
        self,
        script: ScriptDraft,
        director_instruction: str,
        narration_durations: dict[str, float],
        *,
        director_skill_id: str,
        director_skill_version: str,
        input_hash: str,
        reference_image_url: str = '',
        on_usage: UsageRecorder | None = None,
    ) -> tuple[DirectorTreatment, VisualBible, AssetBible, StoryboardPlan]:
        """Develop and plan in isolated calls, then audit with one bounded revision."""

        if not self.configured:
            raise ProviderUnavailable(
                f'真实分镜生成未配置：请设置 {self.credential_setting}'
            )
        expected_beat_ids = [item.id for item in script.beats]
        if (
            not 3 <= len(expected_beat_ids) <= 12
            or set(narration_durations) != set(expected_beat_ids)
            or any(float(narration_durations[item]) <= 0 for item in expected_beat_ids)
        ):
            raise ProviderUnavailable('Director v4 需要完整脚本与逐段旁白时长')
        max_director_chapters = min(10, len(expected_beat_ids))
        beat_payload = [
            {
                'beat_id': item.id,
                'role': item.role,
                'duration_seconds': round(float(narration_durations[item.id]), 3),
                'narration': item.narration,
            }
            for item in script.beats
        ]
        reference_instruction = (
            '本次附有一张 global_reference。请真实观察图片后，只声明它实际适合'
            '承担的 identity、wardrobe、object、location、style 或 composition '
            '职责；不得默认继承全部属性。references 中 reference_id 固定写 '
            'global_reference，并明确 preserve、allow_change 与 forbidden_transfer。'
            if reference_image_url
            else '本次没有参考图，返回结果中的 references 必须为空数组。'
        )
        treatment_system_prompt = (
            '你是知识短视频视觉导演。当前只做全片视觉开发：建立导演处理、'
            '视觉世界和可复用资产，并锁定章节推进数量；不写脚本，也不设计具体事件。\n\n'
            + director_instruction
            + '\n\n'
            '【第一阶段：视觉开发】先不要拆镜头。根据完整口播和真实时长，建立一条'
            '能够随论证推进的视觉命题，而不是逐句配图。锁定重复主体、场景、道具、'
            '材质、视觉母题、章节递进、剪辑节奏、运动规则和验收标准。'
            'chapter_progression 的每一项对应第二阶段的一个视觉章节；数量必须落在输入给定'
            '范围内，本阶段只锁定每章的叙事任务，不设计具体事件、调度或摄影机。\n\n'
            '【完整交付要求】必须同时交付三组可以直接约束下一阶段的结果：'
            '全片视觉方案要写清视觉命题、观众体验、章节递进、重复母题、节奏、剪辑和风格落地；'
            '全片视觉规则要写清视觉世界、重复主体、场景锚点、连续性、色彩材质、构图、参考策略和禁用元素；'
            '资产规则要列出可复用人物、地点、道具、身份锁、材质锁、允许变化、运动规则和至少两条可判定的验收标准。'
            '所有必填文字和必填列表都必须有实质内容，不得用“同上”“保持一致”“按脚本”代替；'
            '没有参考图时 references 必须是空数组。\n\n'
            '参考素材采用职责分离：'
            + reference_instruction
        )
        try:
            assert_provider_neutral_runtime_prompt(treatment_system_prompt)
        except ValueError as exc:
            raise ProviderUnavailable('导演运行时指令编译失败') from exc
        treatment_input = json.dumps(
            {
                'input_type': 'visual_development',
                'reference_image_attached': bool(reference_image_url),
                'chapter_count_bounds': {
                    'minimum': 3,
                    'maximum': max_director_chapters,
                },
                'script_beats': beat_payload,
            },
            ensure_ascii=False,
            indent=2,
        )
        treatment_user_content: str | list[dict] = treatment_input
        if reference_image_url:
            treatment_user_content = [
                {'type': 'text', 'text': treatment_input},
                {
                    'type': 'image_url',
                    'image_url': {'url': reference_image_url},
                },
            ]
        treatment_response = await _openrouter_json_request(
            gateway=self.gateway,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    'role': 'system',
                    'content': treatment_system_prompt,
                },
                {'role': 'user', 'content': treatment_user_content},
            ],
            label='导演视觉开发',
            schema_name='qijia_director_treatment_v3',
            response_schema=_director_treatment_response_schema(
                max_director_chapters
            ),
            max_completion_tokens=64_000,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation='director_treatment',
            on_usage=on_usage,
            reasoning_effort='xhigh',
        )
        now = timestamp()
        treatment = _validate_director_artifact(
            DirectorTreatment,
            treatment_response.data.get('director_treatment'),
            {
                'schema_version': '1.0',
                'model_id': treatment_response.model_id,
                'input_hash': input_hash,
                'created_at': now,
            },
            artifact_name='DirectorTreatment',
        )
        visual_bible = _validate_director_artifact(
            VisualBible,
            treatment_response.data.get('visual_bible'),
            {
                'schema_version': '1.0',
                'director_skill_id': director_skill_id,
                'director_skill_version': director_skill_version,
                'model_id': treatment_response.model_id,
                'input_hash': input_hash,
                'created_at': now,
            },
            artifact_name='VisualBible',
        )
        asset_bible = _validate_director_artifact(
            AssetBible,
            treatment_response.data.get('asset_bible'),
            {
                'schema_version': '1.0',
                'model_id': treatment_response.model_id,
                'input_hash': input_hash,
                'created_at': now,
            },
            artifact_name='AssetBible',
        )
        if bool(asset_bible.references) != bool(reference_image_url):
            raise ProviderUnavailable('Director 返回的参考素材职责与实际输入不一致')
        if reference_image_url and any(
            item.reference_id != 'global_reference'
            for item in asset_bible.references
        ):
            raise ProviderUnavailable('Director 返回了未知参考素材 ID')
        chapter_count = len(treatment.chapter_progression)
        if not 3 <= chapter_count <= max_director_chapters:
            raise ProviderUnavailable(
                'Director 第一阶段返回了超出脚本边界的章节推进数量'
            )
        chapter_ids = [
            f'chapter_{index:02d}' for index in range(1, chapter_count + 1)
        ]

        shot_system_prompt = (
            '你仍是同一位导演。严格服从已经锁定的视觉方案和资产，'
            '现在只交付具体、可读、可生产的视觉章节。\n\n'
            + director_instruction
            + '\n\n'
            '【第二阶段：正式分镜】下面的全片视觉方案、视觉规则与资产规则已经锁定，'
            '不能重新发明视觉世界。章节数量也已锁定；必须且只能逐一填充这些槽位：'
            + '、'.join(chapter_ids)
            + '。不得增加、删除、合并或跳过槽位。根据完整口播与真实旁白时长，把'
            '相邻 beats 连续分配到这些章节；每个 beat_id 必须且只能出现一次并'
            '保持顺序。每章写清 concrete_event、blocking、主体、动作、环境、构图、'
            '起止状态、连续性承接和可执行摄影机。图片是默认媒介；仅在连续动作不可'
            '替代、对应旁白不超过十秒且八秒内能完成时使用 video，全片最多三段。'
            '只返回章节结果，不重复输出已经锁定的视觉方案。'
        )
        try:
            assert_provider_neutral_runtime_prompt(shot_system_prompt)
        except ValueError as exc:
            raise ProviderUnavailable('导演运行时指令编译失败') from exc
        shot_input = json.dumps(
            {
                'input_type': 'chapter_planning',
                'locked_visual_direction': treatment.model_dump(mode='json'),
                'locked_visual_world': visual_bible.model_dump(mode='json'),
                'locked_assets': asset_bible.model_dump(mode='json'),
                'locked_chapter_slots': [
                    {
                        'chapter_id': chapter_id,
                        'narrative_task': treatment.chapter_progression[index],
                    }
                    for index, chapter_id in enumerate(chapter_ids)
                ],
                'script_beats': beat_payload,
            },
            ensure_ascii=False,
            indent=2,
        )
        shot_response = await _openrouter_json_request(
            gateway=self.gateway,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    'role': 'system',
                    'content': shot_system_prompt,
                },
                {'role': 'user', 'content': shot_input},
            ],
            label='导演正式分镜',
            schema_name='qijia_director_shot_plan_v3',
            response_schema=_director_shot_plan_response_schema(chapter_ids),
            max_completion_tokens=STORYBOARD_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation='storyboard_generation',
            on_usage=on_usage,
            reasoning_effort='xhigh',
        )
        beats_by_id = {item.id: item for item in script.beats}

        def build_plan(payload: dict, *, model_id: str) -> StoryboardPlan:
            raw_chapters = payload.get('chapters')
            if (
                not isinstance(raw_chapters, dict)
                or set(raw_chapters) != set(chapter_ids)
            ):
                actual_count = (
                    len(raw_chapters) if isinstance(raw_chapters, dict) else 0
                )
                raise ProviderUnavailable(
                    'Director 未完整交付已锁定的章节槽位'
                    f'（expected={chapter_count}，actual={actual_count}）'
                )
            raw_shots = [raw_chapters[chapter_id] for chapter_id in chapter_ids]
            returned_groups = [
                list(item.get('beat_ids') or []) if isinstance(item, dict) else []
                for item in raw_shots
            ]
            if not _beat_groups_cover_script(returned_groups, expected_beat_ids):
                raise ProviderUnavailable('Director 未按顺序完整覆盖确认脚本')
            selected_types = [
                str(item.get('visual_type') or '') for item in raw_shots
            ]
            if (
                any(item not in {'image', 'video'} for item in selected_types)
                or sum(item == 'video' for item in selected_types) > 3
            ):
                raise ProviderUnavailable('Director 返回了无效的图片/视频分配')
            shots: list[StoryboardShot] = []
            try:
                event_keys: set[str] = set()
                for index, (raw, beat_ids, visual_type) in enumerate(
                    zip(raw_shots, returned_groups, selected_types),
                    1,
                ):
                    chapter_duration = sum(
                        float(narration_durations[beat_id]) for beat_id in beat_ids
                    )
                    if visual_type == 'video' and chapter_duration > 10.0:
                        raise ValueError('video chapter exceeds narration limit')
                    context = ShotContextIR.model_validate(raw.get('context'))
                    event_key = re.sub(
                        r'\s+', '', context.concrete_event
                    ).casefold()
                    if not event_key or event_key in event_keys:
                        raise ValueError('duplicate concrete event')
                    event_keys.add(event_key)
                    if (
                        re.sub(r'\s+', '', context.start_state).casefold()
                        == re.sub(r'\s+', '', context.end_state).casefold()
                    ):
                        raise ValueError('identical start and end states')
                    shots.append(StoryboardShot(
                        shot_id=f'shot_{index:02d}',
                        segment_id=beat_ids[0],
                        beat_ids=beat_ids,
                        narration_excerpt='\n'.join(
                            beats_by_id[beat_id].narration for beat_id in beat_ids
                        ),
                        visual_type=visual_type,
                        visual_intent=context.semantic_goal,
                        context=context,
                    ))
                return StoryboardPlan(
                    schema_version='3.0',
                    shots=shots,
                    model_id=model_id,
                    prompt_version=DIRECTOR_QUALITY_PROMPT_VERSION,
                    input_hash=input_hash,
                    created_at=timestamp(),
                )
            except (KeyError, TypeError, ValueError, ValidationError) as exc:
                raise ProviderUnavailable(
                    'Director 返回内容不符合可执行分镜契约'
                ) from exc

        def plan_chapters(candidate: StoryboardPlan) -> dict[str, dict]:
            return {
                chapter_id: {
                    'beat_ids': list(shot.beat_ids),
                    'visual_type': shot.visual_type,
                    'context': shot.context.model_dump(mode='json'),
                }
                for chapter_id, shot in zip(chapter_ids, candidate.shots)
            }

        async def audit_plan(
            candidate: StoryboardPlan,
            *,
            audit_round: int,
        ) -> tuple[dict, _OpenRouterJsonResponse, str]:
            reviewed_hash = storyboard_review_hash(candidate)
            audit_input = json.dumps(
                {
                    'input_type': 'independent_storyboard_review',
                    'locked_visual_direction': treatment.model_dump(mode='json'),
                    'locked_visual_world': visual_bible.model_dump(mode='json'),
                    'locked_assets': asset_bible.model_dump(mode='json'),
                    'script_beats': beat_payload,
                    'chapters': plan_chapters(candidate),
                },
                ensure_ascii=False,
                indent=2,
            )
            audit_response = await _openrouter_json_request(
                gateway=self.gateway,
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                messages=[
                    {
                        'role': 'system',
                        'content': (
                            '你是与前两阶段上下文隔离的资深审片导演。只审查已经交付的'
                            '章节方案，不重写脚本。检查每章是否把旁白推进转化为具体事件，'
                            '主体调度、起止变化和摄影机是否可读，章节是否真正递进，连续性'
                            '是否成立，图片与视频选择是否必要且可生产。保护鲜明、有风险但'
                            '成立的导演决定；不要用安全、抽象、通用的画面替代它们。只有'
                            '关键或重要问题会实质降低成片时才 verdict=revise；纯润色意见'
                            '仍 verdict=pass。只返回审片 JSON。'
                        ),
                    },
                    {'role': 'user', 'content': audit_input},
                ],
                label=(
                    '导演独立审片'
                    if audit_round == 1
                    else '导演修订后复审'
                ),
                schema_name='qijia_director_review_v1',
                response_schema=_director_review_response_schema(chapter_ids),
                max_completion_tokens=24_000,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
                operation='director_critique',
                on_usage=on_usage,
                reasoning_effort='high',
            )
            audit = _validated_review_payload(
                audit_response.data,
                label=(
                    '导演独立审片'
                    if audit_round == 1
                    else '导演修订后复审'
                ),
                score_keys=_DIRECTOR_QUALITY_SCORE_KEYS,
                verdicts={'pass', 'revise'},
                chapter_ids=set(chapter_ids),
            )
            return audit, audit_response, reviewed_hash

        plan = build_plan(shot_response.data, model_id=shot_response.model_id)
        audit, audit_response, reviewed_hash = await audit_plan(
            plan,
            audit_round=1,
        )
        revision_count = 0
        if audit.get('verdict') == 'revise':
            revision_input = json.dumps(
                {
                    'input_type': 'storyboard_revision',
                    'locked_visual_direction': treatment.model_dump(mode='json'),
                    'locked_visual_world': visual_bible.model_dump(mode='json'),
                    'locked_assets': asset_bible.model_dump(mode='json'),
                    'locked_chapter_slots': [
                        {
                            'chapter_id': chapter_id,
                            'narrative_task': treatment.chapter_progression[index],
                        }
                        for index, chapter_id in enumerate(chapter_ids)
                    ],
                    'script_beats': beat_payload,
                    'current_chapters': plan_chapters(plan),
                    'independent_review': audit,
                },
                ensure_ascii=False,
                indent=2,
            )
            revision_response = await _openrouter_json_request(
                gateway=self.gateway,
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                messages=[
                    {
                        'role': 'system',
                        'content': (
                            '你是正式分镜的修订导演。视觉方案、资产、章节数量、旁白归属'
                            '和已有优点全部锁定；只修复独立审片指出的关键或重要问题。'
                            '每个 beat_id 仍必须按原顺序出现且只出现一次，视频不超过三段，'
                            '不要把具体事件改回抽象象征或通用配图。只返回完整章节 JSON。'
                        ),
                    },
                    {'role': 'user', 'content': revision_input},
                ],
                label='导演分镜修订',
                schema_name='qijia_director_shot_revision_v1',
                response_schema=_director_shot_plan_response_schema(chapter_ids),
                max_completion_tokens=STORYBOARD_MAX_COMPLETION_TOKENS,
                timeout_seconds=self.timeout_seconds,
                transport=self.transport,
                operation='storyboard_revision',
                on_usage=on_usage,
                reasoning_effort='xhigh',
            )
            plan = build_plan(
                revision_response.data,
                model_id=revision_response.model_id,
            )
            revision_count = 1
            audit, audit_response, reviewed_hash = await audit_plan(
                plan,
                audit_round=2,
            )
            if audit.get('verdict') != 'pass':
                raise ProviderUnavailable(
                    'Director 独立审片在一次受控修订后仍未通过，请重新生成视觉方案'
                )
        plan.director_review = DirectorReview(
            passed=True,
            quality_scores={
                str(key): int(value)
                for key, value in dict(audit.get('quality_scores') or {}).items()
            },
            strengths=list(audit.get('strengths') or []),
            revision_requests=list(audit.get('revision_requests') or []),
            reviewed_plan_hash=reviewed_hash,
            revision_count=revision_count,
            model_id=audit_response.model_id,
            prompt_version=DIRECTOR_QUALITY_PROMPT_VERSION,
            reviewed_at=timestamp(),
        )
        return treatment, visual_bible, asset_bible, plan

    async def generate_director_plan(
        self,
        script: ScriptDraft,
        director_instruction: str,
        narration_durations: dict[str, float],
        *,
        director_skill_id: str,
        director_skill_version: str,
        input_hash: str,
        on_usage: UsageRecorder | None = None,
    ) -> tuple[VisualBible, StoryboardPlan]:
        '''Let the v3 Director choose chapter boundaries and media.'''

        if not self.configured:
            raise ProviderUnavailable(
                f'真实分镜生成未配置：请设置 {self.credential_setting}'
            )
        expected_beat_ids = [item.id for item in script.beats]
        if (
            not 3 <= len(expected_beat_ids) <= 12
            or set(narration_durations) != set(expected_beat_ids)
            or any(float(narration_durations[item]) <= 0 for item in expected_beat_ids)
        ):
            raise ProviderUnavailable('Director v3 需要完整脚本与逐段旁白时长')
        beat_payload = [
            {
                'beat_id': item.id,
                'role': item.role,
                'duration_seconds': round(float(narration_durations[item.id]), 3),
                'narration': item.narration,
            }
            for item in script.beats
        ]
        prompt = (
            f'{director_instruction}\n\n'
            '【本次导演任务】根据完整脚本和真实 TTS 时长，自主决定 3—12 个视觉章节。'
            '每个 beat_id 必须且只能出现一次，保持原顺序；只允许合并相邻 ScriptBeat，'
            '不得改写或遗漏旁白。图片是默认媒介；只有连续动作不可替代、章节旁白不超过'
            '十秒且动作能在八秒内完成时才选择 video，全片最多三段 video。\n\n'
            '每章先写 concrete_event，再写 blocking、主体、动作、环境、构图、起止状态、'
            '连续性承接和可执行 camera_intent。concrete_event 必须让不了解旁白的人也能'
            '说清谁在什么地方做了什么、产生什么反馈和结果。visual_metaphor 可以为空，'
            '不得用抽象符号替代具体事件。独立场景允许硬切，不强造空间承接。\n\n'
            '只返回符合 JSON Schema 的 VisualBible 与 shots；不得输出首帧提示词、I2V '
            '提示词、模型参数、供应商名称或解释过程。\n\n'
            '【完整脚本与真实时长】\n'
            + json.dumps(beat_payload, ensure_ascii=False, indent=2)
        )
        response = await _openrouter_json_request(
            gateway=self.gateway,
            api_key=self.api_key,
            base_url=self.base_url,
            model=self.model,
            messages=[
                {
                    'role': 'system',
                    'content': (
                        '你是任务冻结的唯一 Animated Explainer Director。'
                        '以具体事件、可执行调度和摄影机方案交付 VisualBible 与 '
                        'StoryboardPlan v3；不写脚本，不写供应商提示词。'
                    ),
                },
                {'role': 'user', 'content': prompt},
            ],
            label='导演方案生成',
            schema_name='qijia_director_concrete_event_v3',
            response_schema=_DIRECTOR_V3_RESPONSE_SCHEMA,
            max_completion_tokens=STORYBOARD_MAX_COMPLETION_TOKENS,
            timeout_seconds=self.timeout_seconds,
            transport=self.transport,
            operation='storyboard_generation',
            on_usage=on_usage,
        )
        raw_shots = response.data.get('shots')
        if not isinstance(raw_shots, list) or not 3 <= len(raw_shots) <= 12:
            raise ProviderUnavailable('Director Skill 返回了错误的章节数量')
        beats_by_id = {item.id: item for item in script.beats}
        returned_groups = [
            list(item.get('beat_ids') or []) if isinstance(item, dict) else []
            for item in raw_shots
        ]
        if not _beat_groups_cover_script(returned_groups, expected_beat_ids):
            raise ProviderUnavailable('Director Skill 未按顺序完整覆盖确认脚本')
        selected_types = [
            str(item.get('visual_type') or '') for item in raw_shots
        ]
        if (
            any(item not in {'image', 'video'} for item in selected_types)
            or sum(item == 'video' for item in selected_types) > 3
        ):
            raise ProviderUnavailable('Director Skill 返回了无效的图片/视频分配')
        shots: list[StoryboardShot] = []
        try:
            normalized_events: set[str] = set()
            for index, (raw, beat_ids, visual_type) in enumerate(
                zip(raw_shots, returned_groups, selected_types),
                1,
            ):
                chapter_duration = sum(
                    float(narration_durations[beat_id]) for beat_id in beat_ids
                )
                if visual_type == 'video' and chapter_duration > 10.0:
                    raise ValueError('video chapter exceeds narration limit')
                context = ShotContextIR.model_validate(raw.get('context'))
                event_key = re.sub(
                    r'\s+', '', context.concrete_event
                ).casefold()
                if not event_key or event_key in normalized_events:
                    raise ValueError('duplicate or empty concrete event')
                normalized_events.add(event_key)
                if (
                    re.sub(r'\s+', '', context.start_state).casefold()
                    == re.sub(r'\s+', '', context.end_state).casefold()
                ):
                    raise ValueError('start and end states are identical')
                shots.append(StoryboardShot(
                    shot_id=f'shot_{index:02d}',
                    segment_id=beat_ids[0],
                    beat_ids=beat_ids,
                    narration_excerpt='\n'.join(
                        beats_by_id[beat_id].narration for beat_id in beat_ids
                    ),
                    visual_type=visual_type,
                    visual_intent=context.semantic_goal,
                    context=context,
                ))
            bible_payload = dict(response.data.get('visual_bible') or {})
            bible_payload.update({
                'schema_version': '1.0',
                'director_skill_id': director_skill_id,
                'director_skill_version': director_skill_version,
                'model_id': response.model_id,
                'input_hash': input_hash,
                'created_at': timestamp(),
            })
            bible = VisualBible.model_validate(bible_payload)
            plan = StoryboardPlan(
                schema_version='3.0',
                shots=shots,
                model_id=response.model_id,
                prompt_version=DIRECTOR_V3_PROMPT_VERSION,
                input_hash=input_hash,
                created_at=timestamp(),
            )
            return bible, plan
        except (KeyError, TypeError, ValueError, ValidationError) as exc:
            raise ProviderUnavailable(
                'Director Skill 返回内容不符合具体事件与可执行调度契约，请重试'
            ) from exc

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
            raise ProviderUnavailable(
                f'真实分镜生成未配置：请设置 {self.credential_setting}'
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
            gateway=self.gateway,
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
        timeout_seconds: float = OPENROUTER_REQUEST_TIMEOUT_SECONDS,
    ):
        self.api_key = str(api_key or "").strip()
        self.base_url = str(base_url or self.default_base_url).strip()
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
                f"真实分镜生成未配置：请设置 {self.credential_setting}"
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
            gateway=self.gateway,
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


class DGridScriptProvider(OpenRouterScriptProvider):
    """Production Script Skill adapter for DGrid-hosted Claude Fable 5."""

    name = "dgrid-script"
    gateway = "dgrid"
    credential_setting = "DGRID_API_KEY"
    default_base_url = DGRID_DEFAULT_BASE_URL


class DGridStoryboardProvider(OpenRouterStoryboardProvider):
    """Production Director Skill adapter for DGrid-hosted Claude Fable 5."""

    name = "dgrid-storyboard"
    gateway = "dgrid"
    credential_setting = "DGRID_API_KEY"
    default_base_url = DGRID_DEFAULT_BASE_URL
