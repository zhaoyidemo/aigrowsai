"""Code-owned production model selection.

Deployment environments provide credentials and infrastructure only. Concrete
model IDs live here so one reviewed code change updates every backend caller,
the capability API, and the read-only frontend model panel together.
"""
from __future__ import annotations

from dataclasses import dataclass

MODEL_REGISTRY_SOURCE = "qijia_video.model_registry"
PRODUCTION_TEXT_MODEL = "deepseek/deepseek-v4-pro"
# OpenRouter's public model catalog price for the code-owned production text
# model. Actual requests are accounted from response usage.cost because the
# selected upstream route can have a different price.
PRODUCTION_TEXT_INPUT_USD_PER_MILLION = 0.435
PRODUCTION_TEXT_OUTPUT_USD_PER_MILLION = 0.87
# At script review, the three Script Skill requests have already happened. A
# fresh v4 job still has two xhigh Director requests ahead. The lower bound is
# a normal structured response; the upper bound reserves both configured
# completion limits plus a bounded allowance for their combined inputs.
QUALITY_DIRECTOR_REQUEST_COUNT = 2
QUALITY_DIRECTOR_INPUT_TOKEN_RANGE = (12_000, 80_000)
QUALITY_DIRECTOR_OUTPUT_TOKEN_RANGE = (24_000, 192_000)
SEEDANCE_EFFICIENT_MODEL = "doubao-seedance-1-0-pro-fast-251015"
SEEDANCE_BALANCED_MODEL = "doubao-seedance-1-5-pro-251215"
SEEDANCE_FLAGSHIP_MODEL = "doubao-seedance-2-0-260128"
DEFAULT_SEEDANCE_MODEL = SEEDANCE_BALANCED_MODEL


@dataclass(frozen=True)
class ProductionModelRegistry:
    script: str
    director: str
    topic_editor: str
    image: str
    video: str
    tts: str


PRODUCTION_MODELS = ProductionModelRegistry(
    script=PRODUCTION_TEXT_MODEL,
    director=PRODUCTION_TEXT_MODEL,
    topic_editor=PRODUCTION_TEXT_MODEL,
    image="doubao-seedream-5-0-lite-260128",
    video=SEEDANCE_BALANCED_MODEL,
    tts="seed-tts-2.0",
)


MODEL_DISPLAY_NAMES = {
    PRODUCTION_TEXT_MODEL: "DeepSeek V4 Pro",
    "doubao-seedream-5-0-lite-260128": "Seedream 5.0 Lite",
    SEEDANCE_EFFICIENT_MODEL: "Seedance 1.0 Pro Fast",
    SEEDANCE_BALANCED_MODEL: "Seedance 1.5 Pro",
    SEEDANCE_FLAGSHIP_MODEL: "Seedance 2.0",
    "seed-tts-2.0": "Seed-TTS 2.0",
}


def model_display_name(model_id: str) -> str:
    normalized = str(model_id or "").strip()
    return MODEL_DISPLAY_NAMES.get(normalized, normalized or "未配置")
