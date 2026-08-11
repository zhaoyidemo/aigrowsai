"""齐家短视频模块自己的配置边界，可随模块一起迁移。"""
from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class QijiaVideoSettings(BaseSettings):
    DATABASE_URL: str = ""
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = ""
    SESSION_SECRET: str = ""
    AUTH_COOKIE_SECURE: bool = True

    QIJIA_VIDEO_STORAGE: str = "local"
    QIJIA_VIDEO_LOCAL_STORAGE: str = "tmp/qijia_video_storage"
    QIJIA_VIDEO_WORK_ROOT: str = "tmp/qijia_video_work"
    QIJIA_VIDEO_NODE_BINARY: str = "node"
    QIJIA_VIDEO_RENDER_TIMEOUT: int = 1800
    QIJIA_VIDEO_EXECUTION_MODE: str = "auto"
    REMOTION_CONCURRENCY: str = "1"

    OPENROUTER_API_KEY: str = ""
    OPENROUTER_BASE_URL: str = "https://openrouter.ai/api"
    QIJIA_VIDEO_SCRIPT_MODEL: str = "x-ai/grok-4.5"

    # 家庭教育选题研究：仅使用 TikHub 的抖音读接口。中国大陆默认走 .dev。
    TIKHUB_API_KEY: str = ""
    TIKHUB_BASE_URL: str = "https://api.tikhub.dev"
    QIJIA_TOPIC_RESEARCH_MODEL: str = ""
    # 当前固定流程按下方基础价计划 13 次，折合约 ¥0.0871，不超过约 ¥0.10。
    # 100 次只是异常保护的请求硬上限，流程不会为了用满它而额外调用。
    QIJIA_TOPIC_TIKHUB_REQUEST_BUDGET: int = 100
    # TikHub 文档给出的常见基础价为 $0.001/成功请求；具体端点和折扣以账单为准。
    QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS: float = 0.001
    # 星图 V2 单视频完整指标端点固定价；与选题研究基础价分开计账。
    QIJIA_VIDEO_TIKHUB_PERFORMANCE_USD_PER_SUCCESS: float = 0.002

    ARK_API_KEY: str = ""
    QIJIA_VIDEO_SEEDANCE_BASE_URL: str = (
        "https://ark.cn-beijing.volces.com/api/v3"
    )
    # Provider fallback for legacy requests without a frozen model. New tasks
    # always send the model stored in GenerationSettings/VisualGenerationRequest.
    QIJIA_VIDEO_SEEDANCE_MODEL: str = "doubao-seedance-1-0-pro-fast-251015"
    # Legacy fallback price. Model-specific prices below are authoritative for
    # all new requests and allow mixed current / historical cost accounting.
    QIJIA_VIDEO_SEEDANCE_PRICE_PER_MILLION: float = 4.2
    QIJIA_VIDEO_SEEDANCE_10_FAST_PRICE_PER_MILLION: float = 4.2
    # Retained only for historical 1.5 Pro tasks and their missing snapshots.
    QIJIA_VIDEO_SEEDANCE_15_PRICE_PER_MILLION: float = 8.0
    QIJIA_VIDEO_SEEDANCE_20_PRICE_PER_MILLION: float = 46.0
    QIJIA_VIDEO_SEEDANCE_DOWNLOAD_HOSTS: str = (
        ".volces.com,.volccdn.com,.byteimg.com"
    )
    QIJIA_VIDEO_SEEDREAM_BASE_URL: str = (
        "https://ark.cn-beijing.volces.com/api/v3"
    )
    QIJIA_VIDEO_SEEDREAM_MODEL: str = "doubao-seedream-5-0-lite-260128"
    QIJIA_VIDEO_SEEDREAM_SIZE: str = "1440x2560"
    QIJIA_VIDEO_SEEDREAM_DOWNLOAD_HOSTS: str = (
        ".volces.com,.volccdn.com,.byteimg.com"
    )
    # Saved as a ledger snapshot; Ark billing remains authoritative.
    QIJIA_VIDEO_SEEDREAM_PRICE_PER_IMAGE: float = 0.22

    # 新版豆包语音控制台优先使用单个 API Key；旧版账号仍可使用
    # App ID + Access Token。SPEECH_* 回退用于复用主站已有语音应用。
    VOLCENGINE_TTS_API_KEY: str = ""
    VOLCENGINE_TTS_APP_ID: str = ""
    VOLCENGINE_TTS_ACCESS_TOKEN: str = ""
    VOLCENGINE_SPEECH_API_KEY: str = ""
    VOLCENGINE_SPEECH_APP_ID: str = ""
    VOLCENGINE_SPEECH_ACCESS_TOKEN: str = ""
    QIJIA_VIDEO_TTS_ENDPOINT: str = (
        "https://openspeech.bytedance.com/api/v3/tts/unidirectional"
    )
    QIJIA_VIDEO_TTS_RESOURCE_ID: str = "seed-tts-2.0"
    # 豆包语音合成官网按量刊例价；套餐、赠送额度与账单优惠不在此估算中。
    QIJIA_VIDEO_TTS_PRICE_PER_10000_CHARACTERS: float = 5.0
    # Provider fallback only. Product requests are restricted to the curated
    # three-voice Seed-TTS 2.0 catalog in qijia_video.tts_options.
    QIJIA_VIDEO_TTS_VOICE_ID: str = "zh_female_vv_uranus_bigtts"

    VOLCENGINE_TOS_ACCESS_KEY_ID: str = ""
    VOLCENGINE_TOS_SECRET_ACCESS_KEY: str = ""
    VOLCENGINE_TOS_BUCKET: str = ""
    VOLCENGINE_TOS_REGION: str = "cn-shanghai"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def standalone_configuration_errors(self) -> list[str]:
        missing: list[str] = []
        if not self.DATABASE_URL.strip():
            missing.append("DATABASE_URL")
        if not self.ADMIN_USERNAME.strip():
            missing.append("ADMIN_USERNAME")
        if len(self.ADMIN_PASSWORD) < 12:
            missing.append("ADMIN_PASSWORD（至少 12 个字符）")
        if len(self.SESSION_SECRET) < 32:
            missing.append("SESSION_SECRET（至少 32 个字符）")
        return missing

    def local_storage_path(self, project_root: Path) -> Path:
        value = Path(self.QIJIA_VIDEO_LOCAL_STORAGE)
        return value if value.is_absolute() else project_root / value

    def work_root_path(self, project_root: Path) -> Path:
        value = Path(self.QIJIA_VIDEO_WORK_ROOT)
        return value if value.is_absolute() else project_root / value


settings = QijiaVideoSettings()
