"""Code-owned production model selection.

Deployment environments provide credentials and infrastructure only. Concrete
model IDs live here so one reviewed code change updates every backend caller,
the capability API, and the read-only frontend model panel together.
"""
from __future__ import annotations

from dataclasses import dataclass

MODEL_REGISTRY_SOURCE = "qijia_video.model_registry"
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
    script="openai/gpt-5.6-sol",
    director="openai/gpt-5.6-sol",
    topic_editor="openai/gpt-5.6-sol",
    image="doubao-seedream-5-0-lite-260128",
    video=SEEDANCE_BALANCED_MODEL,
    tts="seed-tts-2.0",
)


MODEL_DISPLAY_NAMES = {
    "openai/gpt-5.6-sol": "GPT-5.6 Sol",
    "openai/gpt-5.6-terra": "GPT-5.6 Terra",
    "doubao-seedream-5-0-lite-260128": "Seedream 5.0 Lite",
    SEEDANCE_EFFICIENT_MODEL: "Seedance 1.0 Pro Fast",
    SEEDANCE_BALANCED_MODEL: "Seedance 1.5 Pro",
    SEEDANCE_FLAGSHIP_MODEL: "Seedance 2.0",
    "seed-tts-2.0": "Seed-TTS 2.0",
}


def model_display_name(model_id: str) -> str:
    normalized = str(model_id or "").strip()
    return MODEL_DISPLAY_NAMES.get(normalized, normalized or "未配置")
