"""FastAPI 边界；未来迁移时领域和应用层无需改动。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import mimetypes
import secrets
import shutil
import tempfile
import time
from functools import wraps
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, ConfigDict, Field

from qijia_video import MODULE_VERSION
from qijia_video.contracts import (
    AssetRef,
    CreativeRequestInput,
    DEFAULT_PROVIDER_ADAPTER_ID,
    DEFAULT_SCRIPT_SKILL_ID,
    DEFAULT_VISUAL_STYLE_ID,
    GenerationSettings,
    NewsTopicInput,
    PersonViewpointInput,
    PreGenerationMediaMode,
    QuickSourceCardInput,
    SEEDANCE_EFFICIENT_MODEL,
    SeedanceModelId,
    ScriptDraft,
    SourceCardInput,
)
from qijia_video.errors import ProviderUnavailable, QijiaVideoError
from qijia_video.infrastructure.storage import (
    TOS_DIRECT_UPLOAD_EXPIRES_SECONDS,
    LocalArtifactStorage,
)
from qijia_video.runtime import actor_from_user, runtime, start_run
from qijia_video.settings import settings
from qijia_video.topic_runtime import topic_runtime
from qijia_video.service import RELEASE_ARCHIVE_NAME
from qijia_video.tts_options import TtsSpeedRatio, TtsVoiceId
from qijia_video.auth import get_current_user, require_permission
from qijia_video.upload_media import (
    MAX_SHOT_IMAGE_BYTES,
    MAX_SHOT_VIDEO_BYTES,
    declared_shot_media_format,
    detect_shot_media_format,
    safe_upload_filename,
    validate_shot_media_size,
)


WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024
SHOT_MEDIA_UPLOAD_TOKEN_VERSION = 1
logger = logging.getLogger(__name__)
api_router = APIRouter(
    prefix="/api/qijia-video",
    tags=["齐家 AI 短视频"],
    dependencies=[Depends(require_permission("qijia_video"))],
)
page_router = APIRouter(tags=["齐家 AI 短视频页面"])


def ok(data=None, message: str = "") -> dict:
    return {"code": 0, "data": data, "message": message}


def can_edit_resource(resource, user: dict) -> bool:
    return bool(
        user.get("role") == "admin"
        or str(getattr(resource, "created_by", "") or "")
        == str(user.get("username") or "")
    )


def public_resource_payload(resource, user: dict) -> dict:
    payload = resource.model_dump(mode="json")
    payload["can_edit"] = can_edit_resource(resource, user)
    return payload


def public_job_payload(job, user: dict) -> dict:
    """Keep provider download URLs inside the persisted aggregate."""

    payload = public_resource_payload(job, user)
    for candidate in payload.get("first_frame_candidates") or []:
        candidate.pop("source_url", None)
    payload["douyin_performance_analysis"] = (
        runtime.service.douyin_performance_analysis(job)
    )
    return payload


def boundary(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except QijiaVideoError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return wrapped


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RevisionRequest(StrictRequest):
    expected_revision: int = Field(ge=1)


class ShotMediaUploadInitiateRequest(RevisionRequest):
    original_filename: str = Field(min_length=1, max_length=255)
    media_kind: Literal["image", "video"]
    size_bytes: int = Field(ge=1)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ShotMediaUploadTokenRequest(StrictRequest):
    upload_token: str = Field(min_length=32, max_length=8192)


class ShotMediaUploadClaims(StrictRequest):
    version: Literal[SHOT_MEDIA_UPLOAD_TOKEN_VERSION]
    user_id: int | None
    username: str = Field(min_length=1, max_length=160)
    job_id: str = Field(min_length=1, max_length=64)
    shot_id: str = Field(min_length=1, max_length=64)
    media_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=96)
    object_key: str = Field(min_length=1, max_length=1024)
    media_kind: Literal["image", "video"]
    media_type: Literal[
        "image/jpeg",
        "image/png",
        "image/webp",
        "video/mp4",
        "video/quicktime",
        "video/webm",
    ]
    original_filename: str = Field(min_length=1, max_length=255)
    size_bytes: int = Field(ge=1, le=MAX_SHOT_VIDEO_BYTES)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    expected_revision: int = Field(ge=1)
    expected_selected_media_id: str = Field(default="", max_length=96)
    expires_at: int = Field(gt=0)


class CreateGenerationSettings(StrictRequest):
    """Public v2 selections; legacy execution fields are intentionally absent."""

    skill_id: str = Field(
        default="",
        max_length=64,
        pattern=r"^$|^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    skill_version: str = Field(
        default="",
        max_length=32,
        pattern=r"^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    script_skill_id: str = Field(
        default=DEFAULT_SCRIPT_SKILL_ID,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    script_skill_version: str = Field(
        default="",
        max_length=32,
        pattern=r"^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    director_skill_id: str = Field(
        default=DEFAULT_VISUAL_STYLE_ID,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    director_skill_version: str = Field(
        default="",
        max_length=32,
        pattern=r"^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    provider_adapter_id: str = Field(
        default=DEFAULT_PROVIDER_ADAPTER_ID,
        max_length=64,
        pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$",
    )
    provider_adapter_version: str = Field(
        default="",
        max_length=32,
        pattern=r"^$|^[0-9]+[.][0-9]+[.][0-9]+(?:-[a-z0-9.-]+)?$",
    )
    video_resolution: Literal["480p", "720p", "1080p"] = "1080p"
    tts_voice_id: TtsVoiceId = "zh_female_vv_uranus_bigtts"
    tts_speed_ratio: TtsSpeedRatio = 1.2
    seedance_model: SeedanceModelId = SEEDANCE_EFFICIENT_MODEL

    def to_internal(self) -> GenerationSettings:
        return GenerationSettings.model_validate(self.model_dump(mode="json"))


class CreateJobRequest(StrictRequest):
    source_card_id: str = Field(min_length=1, max_length=64)
    generation_settings: CreateGenerationSettings = Field(
        default_factory=CreateGenerationSettings
    )


class SourceCardUpdateRequest(RevisionRequest):
    source_card: SourceCardInput


class QuickSourceCardUpdateRequest(RevisionRequest):
    source_card: QuickSourceCardInput


class ScriptUpdateRequest(RevisionRequest):
    script: ScriptDraft
    tts_voice_id: TtsVoiceId | None = None
    tts_speed_ratio: TtsSpeedRatio | None = None


class NarrationPreviewRequest(RevisionRequest):
    confirm_cost: Literal[True]


class NewsResearchRetryRequest(RevisionRequest):
    confirm_cost: Literal[True]


class ScriptApprovalRequest(RevisionRequest):
    script_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    prepare_media_first: bool = False


class FinalApprovalRequest(RevisionRequest):
    review_bundle_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ShotRegenerationRequest(RevisionRequest):
    revision_intent: str = Field(min_length=1, max_length=600)
    first_frame_candidate_id: str = Field(default="", max_length=96)
    seedance_model: SeedanceModelId | Literal[""] = ""


class ShotVersionSelectionRequest(RevisionRequest):
    pass


class DouyinPerformanceBindRequest(RevisionRequest):
    douyin_url: str = Field(min_length=1, max_length=4000)
    confirm_cost: Literal[True]


class DouyinPerformanceRefreshRequest(RevisionRequest):
    confirm_cost: Literal[True]


def _shot_media_upload_signature(payload: bytes) -> str:
    secret = str(settings.SESSION_SECRET or "")
    if len(secret) < 32:
        raise ProviderUnavailable("上传签名配置不可用")
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def _create_shot_media_upload_token(claims: ShotMediaUploadClaims) -> str:
    payload = json.dumps(
        claims.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{_shot_media_upload_signature(payload)}"


def _decode_shot_media_upload_token(token: str) -> ShotMediaUploadClaims:
    try:
        encoded, signature = str(token or "").split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if not hmac.compare_digest(
            signature,
            _shot_media_upload_signature(payload),
        ):
            raise ValueError("signature mismatch")
        claims = ShotMediaUploadClaims.model_validate_json(payload)
        if claims.expires_at < int(time.time()):
            raise ValueError("expired")
        return claims
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=422,
            detail="上传凭证无效或已过期，请重新选择文件",
        ) from exc


def _bound_shot_media_upload_claims(
    token: str,
    *,
    job_id: str,
    shot_id: str,
    actor,
) -> ShotMediaUploadClaims:
    claims = _decode_shot_media_upload_token(token)
    if (
        claims.job_id != job_id
        or claims.shot_id != shot_id
        or claims.user_id != actor.user_id
        or claims.username != actor.username
    ):
        raise HTTPException(status_code=403, detail="上传凭证不属于当前任务或用户")
    return claims


async def asset_response(
    asset: AssetRef,
    *,
    filename: str,
    download: bool = False,
):
    if isinstance(runtime.storage, LocalArtifactStorage):
        path = runtime.storage.path_for(asset.object_key)
        if not path.is_file():
            raise HTTPException(status_code=404, detail="媒体文件不存在")
        return FileResponse(
            path,
            media_type=asset.media_type or mimetypes.guess_type(path.name)[0],
            filename=filename,
            content_disposition_type="attachment" if download else "inline",
            headers={"Cache-Control": "private, max-age=60"},
        )
    url = await runtime.storage.signed_get_url(asset, expires=3600)
    return RedirectResponse(url=url, status_code=307)


def _uploaded_image_format(path: Path) -> tuple[str, str]:
    with path.open("rb") as handle:
        header = handle.read(16)
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return ".webp", "image/webp"
    raise HTTPException(status_code=422, detail="参考图只支持 JPG、PNG 或 WebP 格式")


async def _store_reference_image(upload: UploadFile) -> AssetRef:
    with tempfile.TemporaryDirectory(prefix="qijia-video-reference-") as directory:
        local_path = Path(directory) / "reference.image"
        size = 0
        with local_path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_REFERENCE_IMAGE_BYTES:
                    raise HTTPException(status_code=413, detail="参考图不能超过 10 MB")
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="参考图不能为空")
        extension, media_type = _uploaded_image_format(local_path)
        token = secrets.token_hex(16)
        return await runtime.storage.put_file(
            object_key=f"qijia-video/reference-images/{token}{extension}",
            path=local_path,
            asset_id=f"reference_image_{token}",
            media_type=media_type,
        )


def _uploaded_shot_media_format(path: Path) -> tuple[str, str, str]:
    try:
        return detect_shot_media_format(path)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _safe_upload_filename(value: str | None) -> str:
    return safe_upload_filename(value)


async def _store_shot_media(
    upload: UploadFile,
    *,
    job_id: str,
    shot_id: str,
    media_id: str,
) -> tuple[AssetRef, str, str]:
    with tempfile.TemporaryDirectory(prefix="qijia-video-shot-upload-") as directory:
        local_path = Path(directory) / "source.media"
        size = 0
        with local_path.open("wb") as handle:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_SHOT_VIDEO_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="上传视频不能超过 200 MB",
                    )
                handle.write(chunk)
        if size == 0:
            raise HTTPException(status_code=422, detail="上传素材不能为空")
        media_kind, extension, media_type = _uploaded_shot_media_format(local_path)
        if media_kind == "image" and size > MAX_SHOT_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail="上传图片不能超过 20 MB",
            )
        asset = await runtime.storage.put_file(
            object_key=(
                f"qijia-video/{job_id}/uploads/{shot_id}/"
                f"raw-{media_id}{extension}"
            ),
            path=local_path,
            asset_id=f"raw_shot_media_{media_id}",
            media_type=media_type,
        )
        return asset, media_kind, _safe_upload_filename(upload.filename)


def _asset_extension(asset: AssetRef) -> str:
    return {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
        "video/mp4": ".mp4",
        "video/quicktime": ".mov",
        "video/webm": ".webm",
    }.get(asset.media_type, ".media")


@page_router.get(
    "/qijia-video",
    include_in_schema=False,
    dependencies=[Depends(require_permission("qijia_video"))],
)
async def qijia_video_page(user: dict = Depends(get_current_user)):
    page = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    if user.get("role") == "admin":
        page = page.replace(
            " data-admin-only hidden",
            " data-admin-only",
            1,
        )
    return HTMLResponse(
        page,
        headers={"Cache-Control": "no-store"},
    )


@api_router.get("/capabilities")
@boundary
async def capabilities(user: dict = Depends(get_current_user)):
    return ok({
        "version": MODULE_VERSION,
        "actor": actor_from_user(user).model_dump(mode="json"),
        "topic_research": topic_runtime.capabilities(),
        **runtime.capabilities(),
    })


@api_router.get("/skills")
@boundary
async def list_content_skills(user: dict = Depends(get_current_user)):
    del user
    return ok(runtime.service.content_skills())


@api_router.get("/visual-styles", deprecated=True)
@boundary
async def list_visual_styles(user: dict = Depends(get_current_user)):
    del user
    return ok(runtime.service.visual_styles())


@api_router.get('/script-skills')
@boundary
async def list_script_skills(user: dict = Depends(get_current_user)):
    del user
    return ok(runtime.service.script_skills())


@api_router.get('/director-skills')
@boundary
async def list_director_skills(user: dict = Depends(get_current_user)):
    del user
    return ok(runtime.service.director_skills())


@api_router.get('/provider-adapter')
@boundary
async def get_provider_adapter(user: dict = Depends(get_current_user)):
    del user
    return ok(runtime.service.provider_adapter())


@api_router.post("/source-cards")
@boundary
async def create_source_card(
    body: SourceCardInput, user: dict = Depends(get_current_user)
):
    card = await runtime.service.create_source_card(body, actor_from_user(user))
    return ok(card.model_dump(mode="json"), "来源卡已创建")


@api_router.post("/source-cards/quick")
@boundary
async def create_quick_source_card(
    body: QuickSourceCardInput, user: dict = Depends(get_current_user)
):
    actor = actor_from_user(user)
    card = await runtime.service.create_source_card(
        body.to_source_card_input(), actor
    )
    # 快速表单已经包含“材料已核对且可引用”的明确确认，因此直接完成
    # 来源卡核验，不再额外要求第三次人工点击。
    card = await runtime.service.verify_source_card(card.id, card.revision, actor)
    return ok(card.model_dump(mode="json"), "创作材料已确认可用")


@api_router.post("/source-cards/idea")
@boundary
async def create_person_viewpoint(
    body: PersonViewpointInput, user: dict = Depends(get_current_user)
):
    actor = actor_from_user(user)
    card = await runtime.service.create_source_card(
        body.to_source_card_input(), actor
    )
    card = await runtime.service.verify_source_card(card.id, card.revision, actor)
    return ok(card.model_dump(mode="json"), "人物观点已确认，开始创作")


@api_router.post("/source-cards/creative-request")
@boundary
async def create_creative_request(
    body: CreativeRequestInput, user: dict = Depends(get_current_user)
):
    actor = actor_from_user(user)
    card = await runtime.service.create_source_card(
        body.to_source_card_input(), actor
    )
    card = await runtime.service.verify_source_card(card.id, card.revision, actor)
    return ok(card.model_dump(mode="json"), "原始创作请求已冻结，开始研究与创作")


@api_router.post("/source-cards/news-topic")
@boundary
async def create_news_topic(
    body: NewsTopicInput, user: dict = Depends(get_current_user)
):
    actor = actor_from_user(user)
    card = await runtime.service.create_source_card(
        body.to_source_card_input(), actor
    )
    card = await runtime.service.verify_source_card(
        card.id, card.revision, actor
    )
    return ok(
        card.model_dump(mode="json"),
        "新闻主题已冻结，任务创建后将先检索并核验最新公开来源",
    )


@api_router.post("/source-cards/idea-with-reference")
@boundary
async def create_person_viewpoint_with_reference(
    person_name: str = Form(..., min_length=1, max_length=120),
    viewpoint: str = Form(..., min_length=10, max_length=1800),
    reference_image: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    person_name = person_name.strip()
    viewpoint = viewpoint.strip()
    if not person_name or len(viewpoint) < 10:
        raise HTTPException(status_code=422, detail="请输入人物和至少 10 个字的观点")
    idea = PersonViewpointInput(
        person_name=person_name,
        viewpoint=viewpoint,
    )
    reference_asset = await _store_reference_image(reference_image)
    source_card = idea.to_source_card_input()
    source_card.reference_assets = [reference_asset.model_dump(mode="json")]
    actor = actor_from_user(user)
    card = await runtime.service.create_source_card(source_card, actor)
    card = await runtime.service.verify_source_card(card.id, card.revision, actor)
    return ok(card.model_dump(mode="json"), "人物观点和全局参考图已确认，开始创作")


@api_router.post("/source-cards/creative-request-with-reference")
@boundary
async def create_creative_request_with_reference(
    creative_request: str = Form(..., min_length=10, max_length=1800),
    reference_image: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    request = CreativeRequestInput(creative_request=creative_request)
    reference_asset = await _store_reference_image(reference_image)
    source_card = request.to_source_card_input()
    source_card.reference_assets = [reference_asset.model_dump(mode="json")]
    actor = actor_from_user(user)
    card = await runtime.service.create_source_card(source_card, actor)
    card = await runtime.service.verify_source_card(card.id, card.revision, actor)
    return ok(
        card.model_dump(mode="json"),
        "原始创作请求和全局参考图已冻结，开始研究与创作",
    )


@api_router.get("/source-cards")
@boundary
async def list_source_cards(
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(get_current_user),
):
    rows = await runtime.service.list_source_cards(actor_from_user(user), limit=limit)
    return ok([public_resource_payload(item, user) for item in rows])


@api_router.get("/source-cards/{card_id}")
@boundary
async def get_source_card(card_id: str, user: dict = Depends(get_current_user)):
    card = await runtime.service.view_source_card(card_id, actor_from_user(user))
    return ok(public_resource_payload(card, user))


@api_router.put("/source-cards/{card_id}")
@boundary
async def update_source_card(
    card_id: str,
    body: SourceCardUpdateRequest,
    user: dict = Depends(get_current_user),
):
    card = await runtime.service.update_source_card(
        card_id,
        body.source_card,
        body.expected_revision,
        actor_from_user(user),
    )
    return ok(card.model_dump(mode="json"), "来源卡已更新")


@api_router.put("/source-cards/{card_id}/quick")
@boundary
async def update_quick_source_card(
    card_id: str,
    body: QuickSourceCardUpdateRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    card = await runtime.service.update_source_card(
        card_id,
        body.source_card.to_source_card_input(),
        body.expected_revision,
        actor,
    )
    card = await runtime.service.verify_source_card(card.id, card.revision, actor)
    return ok(card.model_dump(mode="json"), "创作材料已更新并确认可用")


@api_router.post("/source-cards/{card_id}/actions/verify")
@boundary
async def verify_source_card(
    card_id: str,
    body: RevisionRequest,
    user: dict = Depends(get_current_user),
):
    card = await runtime.service.verify_source_card(
        card_id, body.expected_revision, actor_from_user(user)
    )
    return ok(card.model_dump(mode="json"), "来源卡已核验")


@api_router.post("/jobs")
@boundary
async def create_job(body: CreateJobRequest, user: dict = Depends(get_current_user)):
    capability = runtime.capabilities()
    if not capability["real_generation_ready"]:
        missing = "、".join(capability.get("missing_configuration") or [])
        raise ProviderUnavailable(
            "真实短视频链路尚未配置完成" + (f"：{missing}" if missing else "")
        )
    actor = actor_from_user(user)
    job = await runtime.service.create_job(
        body.source_card_id,
        actor,
        body.generation_settings.to_internal(),
    )
    run = await start_run("generate_script", job.id, actor)
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job.id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "视频任务已创建")


@api_router.get("/jobs")
@boundary
async def list_jobs(
    limit: int = Query(100, ge=1, le=500),
    user: dict = Depends(get_current_user),
):
    rows = await runtime.service.list_jobs(actor_from_user(user), limit=limit)
    return ok([public_job_payload(item, user) for item in rows])


@api_router.get("/jobs/{job_id}")
@boundary
async def get_job(job_id: str, user: dict = Depends(get_current_user)):
    job = await runtime.service.view_job(job_id, actor_from_user(user))
    return ok(public_job_payload(job, user))


@api_router.put("/jobs/{job_id}/douyin-performance")
@boundary
async def bind_douyin_performance(
    job_id: str,
    body: DouyinPerformanceBindRequest,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.bind_douyin_performance(
        job_id,
        body.douyin_url,
        body.expected_revision,
        actor_from_user(user),
    )
    return ok(
        public_job_payload(job, user),
        "抖音作品已绑定，并保存了本次作品数据",
    )


@api_router.post("/jobs/{job_id}/douyin-performance/actions/refresh")
@boundary
async def refresh_douyin_performance(
    job_id: str,
    body: DouyinPerformanceRefreshRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    started_at = time.perf_counter()
    try:
        job = await runtime.service.refresh_douyin_performance(
            job_id,
            body.expected_revision,
            actor,
        )
    except Exception as exc:
        logger.warning(
            "Douyin performance refresh failed job=%s actor_id=%s "
            "elapsed_ms=%s error_type=%s error=%s",
            job_id,
            actor.user_id,
            max(0, round((time.perf_counter() - started_at) * 1000)),
            type(exc).__name__,
            str(exc)[:500],
            exc_info=True,
        )
        raise
    latest_snapshot = (
        job.douyin_performance.snapshots[-1]
        if job.douyin_performance and job.douyin_performance.snapshots
        else None
    )
    logger.info(
        "Douyin performance refresh succeeded job=%s actor_id=%s "
        "elapsed_ms=%s play_count=%s request_id=%s",
        job_id,
        actor.user_id,
        max(0, round((time.perf_counter() - started_at) * 1000)),
        latest_snapshot.play_count if latest_snapshot else None,
        latest_snapshot.request_id if latest_snapshot else "",
    )
    return ok(
        public_job_payload(job, user),
        "抖音作品数据已刷新",
    )


@api_router.get("/jobs/{job_id}/reference-image")
@boundary
async def preview_reference_image(
    job_id: str,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.view_job(job_id, actor_from_user(user))
    assets = job.source_card_snapshot.get("reference_assets") or []
    if not assets:
        raise HTTPException(status_code=404, detail="该任务没有全局参考图")
    asset = AssetRef.model_validate(assets[0])
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(asset.media_type, ".image")
    return await asset_response(
        asset,
        filename=f"global-reference{extension}",
    )


@api_router.put("/jobs/{job_id}/script")
@boundary
async def update_script(
    job_id: str,
    body: ScriptUpdateRequest,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.update_script(
        job_id,
        body.script,
        body.expected_revision,
        actor_from_user(user),
        tts_voice_id=body.tts_voice_id,
        tts_speed_ratio=body.tts_speed_ratio,
    )
    return ok(public_job_payload(job, user), "脚本已保存，原确认已失效")


@api_router.post("/jobs/{job_id}/narration-preview")
@boundary
async def preview_narration(
    job_id: str,
    body: NarrationPreviewRequest,
    user: dict = Depends(get_current_user),
):
    job, audio, media_type, duration, text = (
        await runtime.service.preview_narration(
            job_id,
            body.expected_revision,
            actor_from_user(user),
        )
    )
    usage = next(
        (
            item
            for item in reversed(job.usage_records)
            if item.operation == "tts_preview" and item.succeeded
        ),
        None,
    )
    return ok({
        "job": public_job_payload(job, user),
        "audio_base64": base64.b64encode(audio).decode("ascii"),
        "media_type": media_type,
        "duration_seconds": round(duration, 3),
        "preview_text": text,
        "estimated_cost_cny": (
            usage.estimated_cost
            if usage and usage.estimated_currency == "CNY"
            else None
        ),
    }, "配音试听已生成，费用已计入本任务")


@api_router.post("/jobs/{job_id}/actions/approve-script")
@boundary
async def approve_script(
    job_id: str,
    body: ScriptApprovalRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    job = await runtime.service.approve_script(
        job_id,
        body.expected_revision,
        body.script_hash,
        actor,
        prepare_media_first=body.prepare_media_first,
    )
    action = (
        "prepare_media_review"
        if body.prepare_media_first
        else "produce"
    )
    run = await start_run(action, job.id, actor)
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job.id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
        "requires_review": False,
        "media_review_requested": body.prepare_media_first,
    }, (
        "脚本已确认，先生成旁白和文字分镜"
        if body.prepare_media_first
        else "脚本已确认，开始生成成片"
    ))


@api_router.post("/jobs/{job_id}/actions/confirm-media-plan")
@boundary
async def confirm_pre_generation_media(
    job_id: str,
    body: RevisionRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    job = await runtime.service.confirm_pre_generation_media(
        job_id,
        body.expected_revision,
        actor,
    )
    run = await start_run("produce", job.id, actor)
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job.id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "素材安排已确认，只生成剩余 AI 画面")


@api_router.post("/jobs/{job_id}/actions/retry")
@boundary
async def retry_job(job_id: str, user: dict = Depends(get_current_user)):
    actor = actor_from_user(user)
    job = await runtime.service.get_job(job_id, actor)
    if runtime.service.needs_script_revision(job):
        reopened = await runtime.service.reopen_script_review(
            job.id,
            job.revision,
            actor,
        )
        return ok({
            "job": public_job_payload(reopened, user),
            "task_id": "",
            "reused": False,
            "requires_review": True,
        }, "已返回脚本修改，请调整后重新确认")
    action = {
        "script": "generate_script",
        "package": "package",
    }.get(job.failed_stage, "produce")
    if (
        action == "produce"
        and job.pre_generation_media_mode
        == PreGenerationMediaMode.REVIEW_BEFORE_GENERATION
    ):
        action = "prepare_media_review"
    run = await start_run(action, job.id, actor)
    return ok({
        "task_id": run.task_id,
        "reused": run.reused,
        "requires_review": False,
    })


@api_router.post("/jobs/{job_id}/actions/retry-news-research")
@boundary
async def retry_news_research(
    job_id: str,
    body: NewsResearchRetryRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    job = await runtime.service.authorize_news_research_retry(
        job_id,
        body.expected_revision,
        actor,
    )
    run = await start_run("generate_script", job.id, actor)
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job.id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "已由编辑确认重新研究，正在生成脚本")


@api_router.post("/jobs/{job_id}/actions/revise-script")
@boundary
async def revise_script_after_narration_failure(
    job_id: str,
    body: RevisionRequest,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.reopen_script_review(
        job_id,
        body.expected_revision,
        actor_from_user(user),
    )
    return ok(
        public_job_payload(job, user),
        (
            "已返回脚本修改；已生成的 AI 画面将继续复用"
            if job.visual_requests
            else "已返回脚本修改"
        ),
    )


@api_router.post("/jobs/{job_id}/actions/approve-final")
@boundary
async def approve_final(
    job_id: str,
    body: FinalApprovalRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    job = await runtime.service.approve_final(
        job_id,
        body.expected_revision,
        body.review_bundle_hash,
        actor,
        package_immediately=False,
    )
    run = await start_run("package", job.id, actor)
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job.id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "成片已确认，开始生成发布包")


@api_router.post("/jobs/{job_id}/shots/{shot_id}/actions/regenerate")
@boundary
async def regenerate_shot(
    job_id: str,
    shot_id: str,
    body: ShotRegenerationRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    selected_fingerprint = await runtime.service.validate_shot_action(
        job_id,
        shot_id,
        body.expected_revision,
        actor,
        first_frame_candidate_id=body.first_frame_candidate_id,
    )
    run = await start_run(
        "regenerate_shot",
        job_id,
        actor,
        {
            "shot_id": shot_id,
            "revision_intent": body.revision_intent,
            "first_frame_candidate_id": body.first_frame_candidate_id,
            "seedance_model": body.seedance_model,
            "expected_selected_fingerprint": selected_fingerprint,
        },
    )
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job_id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "已开始生成这个镜头的新版本")


@api_router.post("/jobs/{job_id}/shots/{shot_id}/media/uploads")
@boundary
async def initiate_shot_media_upload(
    job_id: str,
    shot_id: str,
    body: ShotMediaUploadInitiateRequest,
    user: dict = Depends(get_current_user),
):
    """Validate first, then issue a short-lived direct-to-TOS upload grant."""

    actor = actor_from_user(user)
    original_filename = safe_upload_filename(body.original_filename)
    try:
        validate_shot_media_size(body.media_kind, body.size_bytes)
        _, extension, media_type = declared_shot_media_format(
            original_filename,
            body.media_kind,
        )
    except ValueError as exc:
        status_code = 413 if "不能超过" in str(exc) else 422
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
    selected_media_id = await runtime.service.validate_shot_media_action(
        job_id,
        shot_id,
        body.expected_revision,
        actor,
    )
    create_direct_upload = getattr(
        runtime.storage,
        "create_direct_upload",
        None,
    )
    if not callable(create_direct_upload):
        return ok({"upload_mode": "multipart"})

    media_id = f"upload_{secrets.token_hex(12)}"
    asset_id = f"raw_shot_media_{media_id}"
    object_key = (
        f"qijia-video/staged-uploads/{job_id}/{shot_id}/"
        f"{media_id}{extension}"
    )
    grant = await create_direct_upload(
        object_key=object_key,
        asset_id=asset_id,
        media_type=media_type,
        sha256=body.sha256,
        size_bytes=body.size_bytes,
        expires=TOS_DIRECT_UPLOAD_EXPIRES_SECONDS,
    )
    if grant is None:
        return ok({"upload_mode": "multipart"})
    expires_in_seconds = int(
        grant.get("expires_in_seconds") or TOS_DIRECT_UPLOAD_EXPIRES_SECONDS
    )
    claims = ShotMediaUploadClaims(
        version=SHOT_MEDIA_UPLOAD_TOKEN_VERSION,
        user_id=actor.user_id,
        username=actor.username,
        job_id=job_id,
        shot_id=shot_id,
        media_id=media_id,
        object_key=object_key,
        media_kind=body.media_kind,
        media_type=media_type,
        original_filename=original_filename,
        size_bytes=body.size_bytes,
        sha256=body.sha256,
        expected_revision=body.expected_revision,
        expected_selected_media_id=selected_media_id,
        expires_at=int(time.time()) + expires_in_seconds,
    )
    return ok({
        "upload_mode": "direct",
        "upload_url": grant["url"],
        "upload_method": grant.get("method") or "PUT",
        "upload_headers": grant.get("headers") or {},
        "upload_token": _create_shot_media_upload_token(claims),
        "expires_in_seconds": expires_in_seconds,
    })


@api_router.post("/jobs/{job_id}/shots/{shot_id}/media/uploads/complete")
@boundary
async def complete_shot_media_upload(
    job_id: str,
    shot_id: str,
    body: ShotMediaUploadTokenRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    claims = _bound_shot_media_upload_claims(
        body.upload_token,
        job_id=job_id,
        shot_id=shot_id,
        actor=actor,
    )
    selected_media_id = await runtime.service.validate_shot_media_action(
        job_id,
        shot_id,
        claims.expected_revision,
        actor,
    )
    if selected_media_id != claims.expected_selected_media_id:
        raise HTTPException(
            status_code=409,
            detail="该镜头素材已被其他操作更新，请刷新后重试",
        )
    complete_direct_upload = getattr(
        runtime.storage,
        "complete_direct_upload",
        None,
    )
    if not callable(complete_direct_upload):
        raise ProviderUnavailable("当前存储不支持浏览器直传确认")
    raw_asset = await complete_direct_upload(
        object_key=claims.object_key,
        asset_id=f"raw_shot_media_{claims.media_id}",
        media_type=claims.media_type,
        sha256=claims.sha256,
        size_bytes=claims.size_bytes,
    )
    run = await start_run(
        "prepare_shot_media",
        job_id,
        actor,
        {
            "shot_id": shot_id,
            "raw_asset": raw_asset.model_dump(mode="json"),
            "media_kind": claims.media_kind,
            "media_id": claims.media_id,
            "original_filename": claims.original_filename,
            "expected_selected_media_id": selected_media_id,
        },
    )
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job_id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "素材已安全上传，正在校验并暂存")


@api_router.post("/jobs/{job_id}/shots/{shot_id}/media/uploads/cancel")
@boundary
async def cancel_shot_media_upload(
    job_id: str,
    shot_id: str,
    body: ShotMediaUploadTokenRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    claims = _bound_shot_media_upload_claims(
        body.upload_token,
        job_id=job_id,
        shot_id=shot_id,
        actor=actor,
    )
    # Refuse deletion once any shot edit has moved the aggregate into producing.
    await runtime.service.validate_shot_media_action(
        job_id,
        shot_id,
        claims.expected_revision,
        actor,
    )
    delete_object = getattr(runtime.storage, "delete_object", None)
    if callable(delete_object):
        await delete_object(claims.object_key)
    return ok(None, "未完成的临时上传已清理")


@api_router.post("/jobs/{job_id}/shots/{shot_id}/media")
@boundary
async def upload_shot_media(
    job_id: str,
    shot_id: str,
    expected_revision: int = Form(...),
    media: UploadFile = File(...),
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    if getattr(runtime.storage, "name", "") == "tos":
        raise HTTPException(
            status_code=409,
            detail="上传链路已升级，请刷新页面后重新选择素材",
        )
    selected_media_id = await runtime.service.validate_shot_media_action(
        job_id,
        shot_id,
        expected_revision,
        actor,
    )
    media_id = f"upload_{secrets.token_hex(12)}"
    try:
        raw_asset, media_kind, original_filename = await _store_shot_media(
            media,
            job_id=job_id,
            shot_id=shot_id,
            media_id=media_id,
        )
    finally:
        await media.close()
    run = await start_run(
        "prepare_shot_media",
        job_id,
        actor,
        {
            "shot_id": shot_id,
            "raw_asset": raw_asset.model_dump(mode="json"),
            "media_kind": media_kind,
            "media_id": media_id,
            "original_filename": original_filename,
            "expected_selected_media_id": selected_media_id,
        },
    )
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job_id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "素材已上传，正在校验并暂存")


@api_router.post("/jobs/{job_id}/shot-media/pending/actions/apply")
@boundary
async def apply_pending_shot_media(
    job_id: str,
    body: RevisionRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    fingerprint = await runtime.service.validate_pending_shot_media_action(
        job_id,
        body.expected_revision,
        actor,
    )
    batch_id = f"batch_{secrets.token_hex(12)}"
    run = await start_run(
        "apply_pending_shot_media",
        job_id,
        actor,
        {
            "expected_pending_fingerprint": fingerprint,
            "batch_id": batch_id,
        },
    )
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job_id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "已开始一次应用全部待处理镜头素材")


@api_router.post("/jobs/{job_id}/shot-media/pending/actions/discard")
@boundary
async def discard_all_pending_shot_media(
    job_id: str,
    body: RevisionRequest,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.discard_pending_shot_media_edits(
        job_id,
        body.expected_revision,
        actor_from_user(user),
    )
    return ok(public_job_payload(job, user), "已撤销全部待应用素材修改")


@api_router.post(
    "/jobs/{job_id}/shots/{shot_id}/media/pending/actions/discard"
)
@boundary
async def discard_pending_shot_media(
    job_id: str,
    shot_id: str,
    body: RevisionRequest,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.discard_pending_shot_media_edits(
        job_id,
        body.expected_revision,
        actor_from_user(user),
        shot_id=shot_id,
    )
    return ok(public_job_payload(job, user), "已撤销这个镜头的待应用修改")


@api_router.post(
    "/jobs/{job_id}/shots/{shot_id}/uploads/{media_id}/actions/select"
)
@boundary
async def select_uploaded_shot_media(
    job_id: str,
    shot_id: str,
    media_id: str,
    body: ShotVersionSelectionRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    job = await runtime.service.stage_shot_media_selection(
        job_id,
        shot_id,
        media_id,
        body.expected_revision,
        actor,
    )
    return ok(
        public_job_payload(job, user),
        "素材版本已加入待应用修改",
    )


@api_router.post(
    "/jobs/{job_id}/shots/{shot_id}/actions/restore-generated-media"
)
@boundary
async def restore_generated_shot_media(
    job_id: str,
    shot_id: str,
    body: ShotVersionSelectionRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    job = await runtime.service.stage_shot_media_selection(
        job_id,
        shot_id,
        "",
        body.expected_revision,
        actor,
    )
    return ok(
        public_job_payload(job, user),
        "恢复 AI 素材已加入待应用修改",
    )


@api_router.post(
    "/jobs/{job_id}/shots/{shot_id}/versions/{version_id}/actions/select"
)
@boundary
async def select_shot_version(
    job_id: str,
    shot_id: str,
    version_id: str,
    body: ShotVersionSelectionRequest,
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    selected_fingerprint = await runtime.service.validate_shot_action(
        job_id,
        shot_id,
        body.expected_revision,
        actor,
        version_id=version_id,
    )
    run = await start_run(
        "select_shot_version",
        job_id,
        actor,
        {
            "shot_id": shot_id,
            "version_id": version_id,
            "expected_selected_fingerprint": selected_fingerprint,
        },
    )
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job_id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
    }, "已开始切换镜头版本")


@api_router.get("/jobs/{job_id}/shots/{shot_id}/media")
@boundary
async def preview_selected_shot(
    job_id: str,
    shot_id: str,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.view_job(job_id, actor_from_user(user))
    asset = runtime.service.visual_asset_for_shot(job, shot_id)
    if not asset:
        raise HTTPException(status_code=404, detail="镜头预览尚未就绪")
    return await asset_response(
        asset, filename=f"{shot_id}{_asset_extension(asset)}"
    )


@api_router.get(
    "/jobs/{job_id}/shots/{shot_id}/frames/{candidate_id}/media"
)
@boundary
async def preview_first_frame_candidate(
    job_id: str,
    shot_id: str,
    candidate_id: str,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.view_job(job_id, actor_from_user(user))
    asset = runtime.service.first_frame_asset_for_shot(
        job, shot_id, candidate_id
    )
    if not asset:
        raise HTTPException(status_code=404, detail="首帧候选不存在")
    extension = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/webp": ".webp",
    }.get(asset.media_type, ".image")
    return await asset_response(
        asset, filename=f"{candidate_id}{extension}"
    )


@api_router.get(
    "/jobs/{job_id}/shots/{shot_id}/uploads/{media_id}/media"
)
@boundary
async def preview_uploaded_shot_media(
    job_id: str,
    shot_id: str,
    media_id: str,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.view_job(job_id, actor_from_user(user))
    version = runtime.service.shot_media_for_shot(
        job, shot_id, media_id=media_id
    )
    if not version:
        raise HTTPException(status_code=404, detail="上传素材版本不存在")
    return await asset_response(
        version.asset,
        filename=f"{media_id}{_asset_extension(version.asset)}",
    )


@api_router.get(
    "/jobs/{job_id}/shots/{shot_id}/versions/{version_id}/media"
)
@boundary
async def preview_shot_version(
    job_id: str,
    shot_id: str,
    version_id: str,
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.view_job(job_id, actor_from_user(user))
    asset = runtime.service.visual_asset_for_shot(
        job, shot_id, version_id=version_id
    )
    if not asset:
        raise HTTPException(status_code=404, detail="镜头版本预览不存在")
    return await asset_response(asset, filename=f"{version_id}.mp4")


@api_router.get("/jobs/{job_id}/artifacts/{artifact_name}")
@boundary
async def download_artifact(
    job_id: str,
    artifact_name: str,
    download: bool = Query(False),
    user: dict = Depends(get_current_user),
):
    job = await runtime.service.view_job(job_id, actor_from_user(user))
    artifact = next((item for item in job.artifacts if item.name == artifact_name), None)
    if not artifact:
        raise HTTPException(status_code=404, detail="产物不存在")
    return await asset_response(
        artifact.asset,
        filename=artifact.name,
        download=download,
    )


@api_router.get("/jobs/{job_id}/release-package.zip")
@boundary
async def download_release_package(
    job_id: str,
    user: dict = Depends(get_current_user),
):
    workspace = Path(tempfile.mkdtemp(
        prefix=f"{job_id}-download-",
        dir=runtime.service.work_root,
    ))
    archive = workspace / RELEASE_ARCHIVE_NAME
    try:
        await runtime.service.build_release_archive(
            job_id,
            actor_from_user(user),
            archive,
            shared_read=True,
        )
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
    return FileResponse(
        archive,
        media_type="application/zip",
        filename=RELEASE_ARCHIVE_NAME,
        content_disposition_type="attachment",
        background=BackgroundTask(shutil.rmtree, workspace, True),
    )
