"""主站装配边界与后台任务协调器。"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from qijia_video import run_service as task_service
from qijia_video.contracts import Actor, GenerationSettings
from qijia_video.infrastructure.media import FfmpegMediaPackager
from qijia_video.infrastructure.postgres_repository import (
    PostgresAggregateRepository,
)
from qijia_video.infrastructure.quality import FfprobeQualityChecker
from qijia_video.infrastructure.remotion_renderer import RemotionRenderer
from qijia_video.infrastructure.image_providers import SeedreamImageProvider
from qijia_video.infrastructure.script_providers import (
    OpenRouterScriptProvider,
    OpenRouterStoryboardProvider,
)
from qijia_video.infrastructure.storage import storage_from_settings
from qijia_video.infrastructure.tts_providers import VolcengineTtsProvider
from qijia_video.infrastructure.video_providers import SeedanceVideoProvider
from qijia_video.settings import settings
from qijia_video.service import QijiaVideoService


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUN_JOB_KIND = "qijia_video.run"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RunStart:
    task_id: str
    reused: bool


def actor_from_user(user: dict) -> Actor:
    raw_id = user.get("id")
    try:
        user_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        user_id = None
    role = str(user.get("role") or "member")
    return Actor(
        user_id=user_id,
        username=str(user.get("username") or ""),
        role=role if role in ("admin", "member") else "member",
    )


class QijiaVideoRuntime:
    def __init__(self):
        renderer = RemotionRenderer(
            PROJECT_ROOT / "video_renderer",
            timeout_seconds=settings.QIJIA_VIDEO_RENDER_TIMEOUT,
            node_binary=settings.QIJIA_VIDEO_NODE_BINARY,
            concurrency=settings.REMOTION_CONCURRENCY,
        )
        script_provider = OpenRouterScriptProvider(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
            model=settings.QIJIA_VIDEO_SCRIPT_MODEL,
        )
        storyboard_provider = OpenRouterStoryboardProvider(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
            model=settings.QIJIA_VIDEO_SCRIPT_MODEL,
        )
        tts_provider = VolcengineTtsProvider(
            endpoint=settings.QIJIA_VIDEO_TTS_ENDPOINT,
            resource_id=settings.QIJIA_VIDEO_TTS_RESOURCE_ID,
            voice_id=settings.QIJIA_VIDEO_TTS_VOICE_ID,
            api_key=(
                settings.VOLCENGINE_TTS_API_KEY
                or settings.VOLCENGINE_SPEECH_API_KEY
            ),
            app_id=(
                settings.VOLCENGINE_TTS_APP_ID
                or settings.VOLCENGINE_SPEECH_APP_ID
            ),
            access_token=(
                settings.VOLCENGINE_TTS_ACCESS_TOKEN
                or settings.VOLCENGINE_SPEECH_ACCESS_TOKEN
            ),
        )
        video_provider = SeedanceVideoProvider(
            api_key=settings.ARK_API_KEY,
            model=settings.QIJIA_VIDEO_SEEDANCE_MODEL,
            base_url=settings.QIJIA_VIDEO_SEEDANCE_BASE_URL,
            allowed_download_hosts=tuple(
                item.strip()
                for item in settings.QIJIA_VIDEO_SEEDANCE_DOWNLOAD_HOSTS.split(",")
                if item.strip()
            ),
        )
        image_provider = SeedreamImageProvider(
            api_key=settings.ARK_API_KEY,
            model=settings.QIJIA_VIDEO_SEEDREAM_MODEL,
            base_url=settings.QIJIA_VIDEO_SEEDREAM_BASE_URL,
            size=settings.QIJIA_VIDEO_SEEDREAM_SIZE,
            allowed_download_hosts=tuple(
                item.strip()
                for item in settings.QIJIA_VIDEO_SEEDREAM_DOWNLOAD_HOSTS.split(",")
                if item.strip()
            ),
        )
        self.renderer = renderer
        self.script_provider = script_provider
        self.storyboard_provider = storyboard_provider
        self.tts_provider = tts_provider
        self.image_provider = image_provider
        self.video_provider = video_provider
        self.storage = storage_from_settings(PROJECT_ROOT, settings)
        self.repository = PostgresAggregateRepository()
        self.service = QijiaVideoService(
            repository=self.repository,
            script_provider=script_provider,
            storyboard_provider=storyboard_provider,
            image_provider=image_provider,
            tts_provider=tts_provider,
            video_provider=video_provider,
            renderer=renderer,
            storage=self.storage,
            quality_checker=FfprobeQualityChecker(),
            media_packager=FfmpegMediaPackager(),
            work_root=settings.work_root_path(PROJECT_ROOT),
        )

    def capabilities(self) -> dict:
        renderer_ready, renderer_detail = self.renderer.available()
        missing: list[str] = list(settings.standalone_configuration_errors())
        if not (
            self.script_provider.configured
            and self.storyboard_provider.configured
        ):
            missing.append("OPENROUTER_API_KEY")
        if not self.tts_provider.configured:
            missing.append(
                "VOLCENGINE_SPEECH_API_KEY（或专用 VOLCENGINE_TTS_API_KEY）"
            )
        if not self.video_provider.configured or not self.image_provider.configured:
            missing.append("ARK_API_KEY")
        if not renderer_ready:
            missing.append("Remotion 运行依赖")
        if not getattr(self.storage, "configured", True):
            missing.append(
                "VOLCENGINE_TOS_ACCESS_KEY_ID / VOLCENGINE_TOS_SECRET_ACCESS_KEY / "
                "VOLCENGINE_TOS_BUCKET"
            )
        # Seedance must fetch the selected first frame through HTTPS. Local
        # storage is therefore valid only for the explicit mock CLI, never for
        # the real Web workflow (on Railway or a developer machine).
        if self.storage.name != "tos":
            missing.append("QIJIA_VIDEO_STORAGE=tos")
        generation_ready = not missing
        return {
            "module": "qijia_video",
            "mode": "minimal-real-workflow",
            "script_provider": self.script_provider.name,
            "storyboard_provider": self.storyboard_provider.name,
            "image_provider": self.image_provider.name,
            "tts_provider": self.tts_provider.name,
            "video_provider": self.video_provider.name,
            "renderer": {
                "name": self.renderer.name,
                "ready": renderer_ready,
                "detail": renderer_detail,
            },
            "storage": self.storage.name,
            "real_generation_ready": generation_ready,
            "production_ready": generation_ready and self.storage.name == "tos",
            "missing_configuration": missing,
            "generation_defaults": GenerationSettings().model_dump(mode="json"),
            "seedance_pricing": {
                "currency": "CNY",
                "yuan_per_million_tokens": max(
                    0.0,
                    float(settings.QIJIA_VIDEO_SEEDANCE_PRICE_PER_MILLION),
                ),
                "basis": "按量刊例价估算，实际账单以火山方舟为准",
            },
            "seedream_pricing": {
                "currency": "CNY",
                "yuan_per_image": max(
                    0.0,
                    float(settings.QIJIA_VIDEO_SEEDREAM_PRICE_PER_IMAGE),
                ),
                "candidates_per_shot": 1,
                "model": settings.QIJIA_VIDEO_SEEDREAM_MODEL,
                "basis": "按生成图片张数估算，实际账单以火山方舟为准",
            },
            "notes": [
                "生产链路不使用 Mock；收费生成 Provider 失败时不会伪造结果或自动换模型",
                "五个章节各生成一张 Seedream 首帧；其中三张驱动 480p、8-10 秒 Seedance 2.0 视频，另外两张由 Remotion 动态呈现",
                "存在全局参考图时由参考图主导画风；无参考图时使用全片画面导演设定",
                "最终由 Remotion 直接合成为 480x854 竖屏成片",
            ],
        }


runtime = QijiaVideoRuntime()


def _failure_stage(action: str) -> str:
    return {
        "generate_script": "script",
        "produce": "production",
        "package": "package",
    }.get(action, "production")


async def _execute(
    run_task_id: str,
    action: str,
    job_id: str,
    actor: Actor,
    parameters: dict | None = None,
):
    tokens = task_service.set_task_context({
        "id": actor.user_id,
        "username": actor.username,
        "role": actor.role,
    })
    try:
        last_progress_marker: tuple[str, int, str] | None = None

        def report(payload: dict) -> None:
            nonlocal last_progress_marker
            task_service.update_progress(run_task_id, payload)
            marker = (
                str(payload.get("stage") or ""),
                int(payload.get("percent") or 0),
                str(payload.get("message") or ""),
            )
            if marker != last_progress_marker:
                logger.info(
                    "Qijia video progress job=%s action=%s stage=%s percent=%s",
                    job_id,
                    action,
                    marker[0],
                    marker[1],
                )
                last_progress_marker = marker

        parameters = dict(parameters or {})
        if action == "generate_script":
            job = await runtime.service.generate_script(
                job_id, actor, progress=report
            )
        elif action == "produce":
            job = await runtime.service.produce(job_id, actor, progress=report)
        elif action == "package":
            job = await runtime.service.package(job_id, actor, progress=report)
        elif action == "regenerate_shot":
            job = await runtime.service.regenerate_shot(
                job_id,
                str(parameters.get("shot_id") or ""),
                str(parameters.get("prompt") or ""),
                str(parameters.get("expected_selected_fingerprint") or ""),
                actor,
                progress=report,
                first_frame_candidate_id=str(
                    parameters.get("first_frame_candidate_id") or ""
                ),
            )
        elif action == "select_shot_version":
            job = await runtime.service.select_shot_version(
                job_id,
                str(parameters.get("shot_id") or ""),
                str(parameters.get("version_id") or ""),
                str(parameters.get("expected_selected_fingerprint") or ""),
                actor,
                progress=report,
            )
        else:
            raise RuntimeError("未知的齐家短视频后台动作")
        terminal = {
            "generate_script": ("脚本已就绪，等待你确认", "confirm_script", 28),
            "produce": ("成片已就绪，等待你确认", "confirm_final", 90),
            "package": ("发布包已完成，可以下载", "package", 100),
            "regenerate_shot": (
                "镜头新版本已应用，等待你确认成片",
                "confirm_final",
                90,
            ),
            "select_shot_version": (
                "镜头版本已切换，等待你确认成片",
                "confirm_final",
                90,
            ),
        }[action]
        task_service.update_progress(run_task_id, {
            "message": terminal[0], "stage": terminal[1], "percent": terminal[2],
        })
        task_service.complete_task(run_task_id, {
            "job_id": job.id,
            "state": job.state.value,
            "revision": job.revision,
        })
        logger.info(
            "Qijia video action completed job=%s action=%s state=%s",
            job_id,
            action,
            job.state.value,
        )
    except Exception as exc:
        logger.warning(
            "Qijia video action failed job=%s action=%s error=%s",
            job_id,
            action,
            exc,
        )
        task_service.fail_task(run_task_id, str(exc), {"job_id": job_id, "action": action})
    finally:
        task_service.reset_task_context(tokens)


async def run_worker_task(task_id: str) -> int:
    """Execute one persisted host task from the process-isolated worker."""
    task = await task_service.get_task_async(task_id)
    if not task:
        raise RuntimeError("Worker 任务不存在")
    if task.get("job_kind") != RUN_JOB_KIND:
        raise RuntimeError("Worker 任务类型不匹配")
    payload = task.get("job_payload") or {}
    action = str(payload.get("action") or "")
    job_id = str(payload.get("job_id") or "")
    actor = Actor.model_validate(payload.get("actor") or {})
    parameters = payload.get("parameters")
    await _execute(
        task_id,
        action,
        job_id,
        actor,
        parameters if isinstance(parameters, dict) else {},
    )
    # complete_task/fail_task schedules a snapshot; explicitly flush so the
    # short-lived process cannot exit before the terminal state is durable.
    await asyncio.sleep(0)
    await task_service.flush_task_async(task_id)
    final = await task_service.get_task_async(task_id)
    return 0 if final and final.get("status") == "done" else 1


async def _monitor_worker(
    process: asyncio.subprocess.Process,
    task_id: str,
    action: str,
    job_id: str,
    actor: Actor,
):
    return_code = await process.wait()
    if return_code == 0:
        return
    task = await task_service.get_task_async(task_id)
    if task and task.get("status") in ("done", "failed"):
        return
    error = f"独立视频 Worker 异常退出（exit={return_code}）"
    task_service.fail_task(task_id, error, {"job_id": job_id, "action": action})
    await task_service.flush_task_async(task_id)
    try:
        if action in ("regenerate_shot", "select_shot_version"):
            await runtime.service.mark_shot_edit_failed(job_id, error, actor)
        else:
            await runtime.service.mark_execution_failed(
                job_id, _failure_stage(action), error, actor
            )
    except Exception:
        # 任务记录已明确失败；领域记录会在下一次恢复/重试时再次校正。
        pass


async def start_run(
    action: str,
    job_id: str,
    actor: Actor,
    parameters: dict | None = None,
) -> RunStart:
    if action not in (
        "generate_script",
        "produce",
        "package",
        "regenerate_shot",
        "select_shot_version",
    ):
        raise ValueError("不支持的后台动作")
    run_group = (
        "shot-edit"
        if action in ("regenerate_shot", "select_shot_version")
        else action
    )
    name = f"齐家短视频:{run_group}:{job_id}"
    payload = {
        "action": action,
        "job_id": job_id,
        "actor": actor.model_dump(mode="json"),
        "parameters": dict(parameters or {}),
    }
    task_id, reused = await task_service.create_or_get_running_task_async(
        name,
        job_kind=RUN_JOB_KIND,
        job_payload=payload,
        recoverable=True,
    )
    if not reused:
        await runtime.service.set_last_run_task(job_id, task_id, actor)
        mode = settings.QIJIA_VIDEO_EXECUTION_MODE.strip().lower()
        if mode not in ("auto", "inline", "process"):
            error = f"无效执行模式：{mode}"
            task_service.fail_task(task_id, error)
            await runtime.service.mark_execution_failed(
                job_id,
                _failure_stage(action),
                error,
                actor,
            )
            raise RuntimeError(f"无效的 QIJIA_VIDEO_EXECUTION_MODE：{mode}")
        use_process = mode == "process" or (
            mode == "auto" and task_service.async_session is not None
        )
        if use_process:
            creationflags = (
                subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            )
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "qijia_video.worker",
                    "--task-id",
                    task_id,
                    cwd=str(PROJECT_ROOT),
                    creationflags=creationflags,
                )
                asyncio.create_task(
                    _monitor_worker(process, task_id, action, job_id, actor)
                )
            except Exception as exc:
                error = f"无法启动独立视频 Worker：{exc}"
                task_service.fail_task(task_id, error)
                await task_service.flush_task_async(task_id)
                await runtime.service.mark_execution_failed(
                    job_id,
                    _failure_stage(action),
                    error,
                    actor,
                )
                raise
        else:
            asyncio.create_task(
                _execute(task_id, action, job_id, actor, dict(parameters or {}))
            )
    return RunStart(task_id=task_id, reused=reused)


async def _recover_run(payload: dict) -> str:
    action = str(payload.get("action") or "")
    job_id = str(payload.get("job_id") or "")
    actor = Actor.model_validate(payload.get("actor") or {})
    parameters = payload.get("parameters")
    tokens = task_service.set_task_context({
        "id": actor.user_id,
        "username": actor.username,
        "role": actor.role,
    })
    try:
        return (
            await start_run(
                action,
                job_id,
                actor,
                parameters if isinstance(parameters, dict) else {},
            )
        ).task_id
    finally:
        task_service.reset_task_context(tokens)


task_service.register_recovery_handler(RUN_JOB_KIND, _recover_run)
