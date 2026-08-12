"""家庭教育选题研究的轻量任务执行骨架。"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from qijia_video import run_service as task_service
from qijia_video.contracts import Actor
from qijia_video.cost_analysis import USD_TO_CNY_RATE
from qijia_video.errors import ProviderUnavailable
from qijia_video.infrastructure.postgres_repository import (
    PostgresAggregateRepository,
)
from qijia_video.infrastructure.tikhub import (
    PLANNED_MAX_TIKHUB_CALLS,
    TikHubDouyinResearchProvider,
    evidence_quality_policy,
)
from qijia_video.infrastructure.topic_providers import OpenRouterTopicEditor
from qijia_video.model_registry import PRODUCTION_MODELS
from qijia_video.settings import settings
from qijia_video.topic_contracts import TopicResearchRun, TopicResearchStatus
from qijia_video.topic_service import TopicResearchService


TOPIC_JOB_KIND = "qijia_topic.research"
TOPIC_TASK_NAME = "齐家选题研究:family_education"
logger = logging.getLogger(__name__)
_start_lock = asyncio.Lock()
_background_tasks: set[asyncio.Task] = set()


@dataclass(frozen=True)
class TopicRunStart:
    run: TopicResearchRun
    task_id: str
    reused: bool


class TopicResearchRuntime:
    def __init__(self):
        self.data_provider = TikHubDouyinResearchProvider(
            api_key=settings.TIKHUB_API_KEY,
            base_url=settings.TIKHUB_BASE_URL,
            request_budget=settings.QIJIA_TOPIC_TIKHUB_REQUEST_BUDGET,
        )
        self.editor = OpenRouterTopicEditor(
            api_key=settings.OPENROUTER_API_KEY,
            base_url=settings.OPENROUTER_BASE_URL,
            model=PRODUCTION_MODELS.topic_editor,
        )
        self.repository = PostgresAggregateRepository()
        self.service = TopicResearchService(
            repository=self.repository,
            data_provider=self.data_provider,
            editor=self.editor,
            estimated_tikhub_cost_per_success_usd=(
                settings.QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS
            ),
        )

    def capabilities(self) -> dict:
        missing: list[str] = []
        if not self.data_provider.configured:
            missing.extend(self.data_provider.configuration_errors)
        if not self.editor.configured:
            missing.extend(self.editor.configuration_errors)
        return {
            "ready": not missing,
            "theme": "family_education",
            "theme_label": "家庭教育",
            "platform": "douyin",
            "provider": self.data_provider.name,
            "editor": self.editor.name,
            "model": self.editor.model,
            "request_budget": self.data_provider.request_budget,
            "planned_max_calls": PLANNED_MAX_TIKHUB_CALLS,
            "evidence_policy": evidence_quality_policy(),
            "estimated_usd_per_success": max(
                0.0,
                float(settings.QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS),
            ),
            "usd_to_cny_rate": USD_TO_CNY_RATE,
            "missing_configuration": missing,
            "cost_confirmation_required": True,
            "notes": [
                "TikHub 与字节跳动无官方隶属关系，数据仅用于选题研究",
                "TikHub 榜单决定样本入池；粉丝、播放、互动和发布时间只用于复核标签与排序",
                "榜单作品通过一次批量详情调用补齐指标，字段缺失不会被按 0 淘汰",
                "抖音趋势不会自动转成已核验来源卡",
                "TikHub 金额是规划估算，实际端点价格与阶梯折扣以供应商账单为准",
                "为避免服务重启后重复计费，中断的研究不会自动重跑",
            ],
        }

    async def reconcile_run(
        self, run: TopicResearchRun, actor: Actor
    ) -> TopicResearchRun:
        if run.status != TopicResearchStatus.RUNNING:
            return run
        if not actor.is_admin and run.created_by != actor.username:
            return run
        if not run.last_run_task_id:
            return await self.service.fail_if_interrupted(run.id, actor)
        task = await task_service.get_task_async(
            run.last_run_task_id, refresh=True
        )
        if not task or task.get("status") in ("done", "failed"):
            return await self.service.fail_if_interrupted(run.id, actor)
        return run

    async def list_runs(
        self, actor: Actor, *, limit: int = 30
    ) -> list[TopicResearchRun]:
        rows = await self.service.list_runs(actor, limit=limit)
        return [await self.reconcile_run(item, actor) for item in rows]

    async def get_run(
        self, run_id: str, actor: Actor
    ) -> TopicResearchRun:
        return await self.reconcile_run(
            await self.service.view_run(run_id, actor), actor
        )


topic_runtime = TopicResearchRuntime()


async def _execute(run_task_id: str, run_id: str, actor: Actor) -> None:
    tokens = task_service.set_task_context({
        "id": actor.user_id,
        "username": actor.username,
        "role": actor.role,
    })
    try:
        def report(payload: dict) -> None:
            task_service.update_progress(run_task_id, payload)

        run = await topic_runtime.service.execute(
            run_id, actor, progress=report
        )
        task_service.update_progress(run_task_id, {
            "message": "五个候选已形成，等待人工选择",
            "stage": "topic_review",
            "percent": 100,
        })
        task_service.complete_task(run_task_id, {
            "run_id": run.id,
            "status": run.status.value,
            "revision": run.revision,
        })
        logger.info("Topic research completed run=%s", run_id)
    except Exception as exc:
        logger.warning("Topic research failed run=%s error=%s", run_id, exc)
        try:
            await topic_runtime.service.mark_failed(run_id, str(exc), actor)
        except Exception:
            logger.exception("Failed to persist topic research failure run=%s", run_id)
        task_service.fail_task(
            run_task_id, str(exc), {"run_id": run_id}
        )
    finally:
        await asyncio.sleep(0)
        await task_service.flush_task_async(run_task_id)
        task_service.reset_task_context(tokens)


async def start_topic_research(actor: Actor) -> TopicRunStart:
    async with _start_lock:
        initial_payload = {"actor": actor.model_dump(mode="json")}
        task_id, reused = await task_service.create_or_get_running_task_async(
            TOPIC_TASK_NAME,
            job_kind=TOPIC_JOB_KIND,
            job_payload=initial_payload,
            # Research calls may be billable. Never replay them automatically
            # after a restart without a fresh human confirmation.
            recoverable=False,
        )
        if reused:
            task = await task_service.get_task_async(task_id)
            run_id = str((task or {}).get("job_payload", {}).get("run_id") or "")
            if not run_id:
                raise ProviderUnavailable("已有选题任务尚未完成初始化，请稍后重试")
            return TopicRunStart(
                run=await topic_runtime.get_run(run_id, actor),
                task_id=task_id,
                reused=True,
            )
        try:
            run = await topic_runtime.service.create_run(actor)
            payload = {
                **initial_payload,
                "run_id": run.id,
            }
            await task_service.update_task_payload_async(task_id, payload)
            run = await topic_runtime.service.set_last_run_task(
                run.id, task_id, actor
            )
        except Exception as exc:
            task_service.fail_task(task_id, str(exc))
            await task_service.flush_task_async(task_id)
            raise
        background_task = asyncio.create_task(_execute(task_id, run.id, actor))
        _background_tasks.add(background_task)
        background_task.add_done_callback(_background_tasks.discard)
        return TopicRunStart(run=run, task_id=task_id, reused=False)
