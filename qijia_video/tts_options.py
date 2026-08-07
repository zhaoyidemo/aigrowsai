"""Curated Seed-TTS 2.0 options exposed by the Qijia video workflow."""
from __future__ import annotations

from typing import Final, Literal


TtsVoiceId = Literal[
    "zh_female_vv_uranus_bigtts",
    "zh_female_santongyongns_saturn_bigtts",
    "zh_male_ruyayichen_saturn_bigtts",
]
TtsSpeedRatio = Literal[1.0, 1.1, 1.2]

DEFAULT_TTS_VOICE_ID: Final[TtsVoiceId] = "zh_female_vv_uranus_bigtts"
DEFAULT_TTS_SPEED_RATIO: Final[TtsSpeedRatio] = 1.2
LEGACY_TTS_SPEED_RATIO: Final[TtsSpeedRatio] = 1.0

TTS_VOICE_OPTIONS: Final[tuple[dict[str, str | bool], ...]] = (
    {
        "id": "zh_female_vv_uranus_bigtts",
        "label": "Vivi 2.0",
        "description": "亲和自然，适合家庭教育主叙事",
        "gender": "female",
        "default": True,
    },
    {
        "id": "zh_female_santongyongns_saturn_bigtts",
        "label": "流畅女声",
        "description": "清晰利落，适合知识口播",
        "gender": "female",
        "default": False,
    },
    {
        "id": "zh_male_ruyayichen_saturn_bigtts",
        "label": "儒雅逸辰",
        "description": "沉稳克制，适合分析型内容",
        "gender": "male",
        "default": False,
    },
)
TTS_VOICE_IDS: Final[frozenset[str]] = frozenset(
    str(item["id"]) for item in TTS_VOICE_OPTIONS
)
TTS_SPEED_OPTIONS: Final[tuple[TtsSpeedRatio, ...]] = (1.0, 1.1, 1.2)
TTS_SPEED_TO_PROVIDER_RATE: Final[dict[float, int]] = {
    1.0: 0,
    1.1: 10,
    1.2: 20,
}
TTS_SCRIPT_CHARACTER_TARGETS: Final[dict[float, tuple[int, int]]] = {
    1.0: (220, 300),
    1.1: (245, 325),
    1.2: (265, 355),
}


def provider_speech_rate(speed_ratio: float) -> int:
    """Map the product-level multiplier to Seed-TTS ``speech_rate``."""

    raw = float(speed_ratio)
    normalized = round(raw, 1)
    if (
        abs(raw - normalized) > 1e-9
        or normalized not in TTS_SPEED_TO_PROVIDER_RATE
    ):
        raise ValueError("配音语速只支持 1.0x、1.1x 或 1.2x")
    return TTS_SPEED_TO_PROVIDER_RATE[normalized]
