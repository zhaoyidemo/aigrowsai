"""FastAPI 边界；未来迁移时领域和应用层无需改动。"""
from __future__ import annotations

import base64
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
    GenerationSettings,
    NewsTopicInput,
    PersonViewpointInput,
    QuickSourceCardInput,
    SeedanceModelId,
    ScriptDraft,
    SourceCardInput,
)
from qijia_video.errors import ProviderUnavailable, QijiaVideoError
from qijia_video.infrastructure.storage import LocalArtifactStorage
from qijia_video.runtime import actor_from_user, runtime, start_run
from qijia_video.topic_runtime import topic_runtime
from qijia_video.service import RELEASE_ARCHIVE_NAME
from qijia_video.tts_options import TtsSpeedRatio, TtsVoiceId
from qijia_video.auth import get_current_user, require_permission


WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_REFERENCE_IMAGE_BYTES = 10 * 1024 * 1024
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


class CreateJobRequest(StrictRequest):
    source_card_id: str = Field(min_length=1, max_length=64)
    generation_settings: GenerationSettings = Field(
        default_factory=GenerationSettings
    )


class SourceCardUpdateRequest(RevisionRequest):
    source_card: SourceCardInput


class QuickSourceCardUpdateRequest(RevisionRequest):
    source_card: QuickSourceCardInput


class ScriptUpdateRequest(RevisionRequest):
    script: ScriptDraft
    seedance_prompt: str | None = Field(
        default=None,
        min_length=1,
        max_length=3200,
    )
    tts_voice_id: TtsVoiceId | None = None
    tts_speed_ratio: TtsSpeedRatio | None = None


class NarrationPreviewRequest(RevisionRequest):
    confirm_cost: Literal[True]


class NewsResearchRetryRequest(RevisionRequest):
    confirm_cost: Literal[True]


class ScriptApprovalRequest(RevisionRequest):
    script_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class FinalApprovalRequest(RevisionRequest):
    review_bundle_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ShotRegenerationRequest(RevisionRequest):
    prompt: str = Field(min_length=1, max_length=4000)
    first_frame_candidate_id: str = Field(default="", max_length=96)
    seedance_model: SeedanceModelId | Literal[""] = ""


class ShotVersionSelectionRequest(RevisionRequest):
    pass


class DouyinPerformanceBindRequest(RevisionRequest):
    douyin_url: str = Field(min_length=1, max_length=4000)
    confirm_cost: Literal[True]


class DouyinPerformanceRefreshRequest(RevisionRequest):
    confirm_cost: Literal[True]


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
        body.generation_settings,
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
        seedance_prompt=body.seedance_prompt,
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
        job_id, body.expected_revision, body.script_hash, actor
    )
    run = await start_run("produce", job.id, actor)
    return ok({
        "job": public_job_payload(
            await runtime.service.get_job(job.id, actor), user
        ),
        "task_id": run.task_id,
        "reused": run.reused,
        "requires_review": False,
    }, "脚本已确认，开始生成成片")


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
            "prompt": body.prompt,
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
    return await asset_response(asset, filename=f"{shot_id}.mp4")


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
