"""家庭教育选题研究应用服务。"""
from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Callable

from qijia_video.contracts import Actor, timestamp
from qijia_video.errors import (
    InvalidTransition,
    QualityGateFailed,
    RevisionConflict,
)
from qijia_video.ports import AggregateRepository
from qijia_video.topic_contracts import (
    TopicCandidate,
    TopicCostSummary,
    TopicEvidence,
    TopicEvidenceTier,
    TopicEvidenceType,
    TopicResearchRun,
    TopicResearchStatus,
)
from qijia_video.topic_ports import (
    TopicCollectionFailed,
    TopicDataProvider,
    TopicEditorialFailed,
    TopicEditor,
)


ProgressReporter = Callable[[dict], None]
TOPIC_RESOURCE_KIND = "topic_research"


class TopicResearchService:
    def __init__(
        self,
        *,
        repository: AggregateRepository,
        data_provider: TopicDataProvider,
        editor: TopicEditor,
        estimated_tikhub_cost_per_success_usd: float = 0.001,
    ):
        self.repository = repository
        self.data_provider = data_provider
        self.editor = editor
        self.estimated_tikhub_cost_per_success_usd = max(
            0.0, float(estimated_tikhub_cost_per_success_usd)
        )

    @staticmethod
    def _report(
        progress: ProgressReporter | None,
        *,
        message: str,
        stage: str,
        percent: int,
    ) -> None:
        if progress:
            progress({
                "message": message,
                "stage": stage,
                "percent": max(0, min(100, int(percent))),
            })

    @staticmethod
    def _assert_revision(actual: int, expected: int) -> None:
        if int(actual) != int(expected):
            raise RevisionConflict("选题研究已在其他页面更新，请刷新后重试")

    async def create_run(self, actor: Actor) -> TopicResearchRun:
        now = timestamp()
        draft = TopicResearchRun(
            id="pending",
            status=TopicResearchStatus.RUNNING,
            cost=TopicCostSummary(
                tikhub_request_budget=self.data_provider.request_budget,
                tikhub_cost_basis=(
                    "运行后按成功请求次数估算；端点单价与阶梯折扣以 TikHub 账单为准"
                    if self.estimated_tikhub_cost_per_success_usd > 0
                    else "记录调用次数；当前未配置 TikHub 单次成功请求规划价"
                ),
            ),
            created_by=actor.username,
            created_at=now,
            updated_at=now,
        )
        saved = await self.repository.create(
            TOPIC_RESOURCE_KIND,
            "齐家家庭教育选题研究",
            actor,
            draft.model_dump(mode="json"),
        )
        return TopicResearchRun.model_validate(saved)

    async def list_runs(
        self, actor: Actor, *, limit: int = 30
    ) -> list[TopicResearchRun]:
        return [
            TopicResearchRun.model_validate(item)
            for item in await self.repository.list_visible(
                TOPIC_RESOURCE_KIND, actor, limit=limit
            )
        ]

    async def get_run(self, run_id: str, actor: Actor) -> TopicResearchRun:
        return TopicResearchRun.model_validate(
            await self.repository.get(TOPIC_RESOURCE_KIND, run_id, actor)
        )

    async def view_run(self, run_id: str, actor: Actor) -> TopicResearchRun:
        return TopicResearchRun.model_validate(
            await self.repository.get_visible(
                TOPIC_RESOURCE_KIND, run_id, actor
            )
        )

    async def _save(
        self, run: TopicResearchRun, actor: Actor
    ) -> TopicResearchRun:
        current = run.revision
        run.revision += 1
        run.updated_at = timestamp()
        saved = await self.repository.replace(
            TOPIC_RESOURCE_KIND,
            run.id,
            actor,
            run.model_dump(mode="json"),
            expected_revision=current,
        )
        return TopicResearchRun.model_validate(saved)

    async def set_last_run_task(
        self, run_id: str, task_id: str, actor: Actor
    ) -> TopicResearchRun:
        run = await self.get_run(run_id, actor)
        run.last_run_task_id = task_id
        return await self._save(run, actor)

    def _cost_summary(
        self,
        calls,
        *,
        model_usage=None,
        candidate_count: int = 0,
    ) -> TopicCostSummary:
        succeeded = sum(item.succeeded for item in calls)
        tikhub_estimate = (
            round(succeeded * self.estimated_tikhub_cost_per_success_usd, 8)
            if self.estimated_tikhub_cost_per_success_usd > 0
            else None
        )
        if tikhub_estimate is None:
            basis = (
                "已记录 TikHub 请求量；未配置单次成功请求规划价，金额待账单核对"
            )
        else:
            basis = (
                f"按规划价 ${self.estimated_tikhub_cost_per_success_usd:g}/次"
                f" × {succeeded} 次成功请求估算；失败响应按 $0 估算，"
                "实际端点单价和阶梯折扣以 TikHub 账单为准"
            )
        total_estimate = None
        if (
            tikhub_estimate is not None
            and model_usage is not None
            and model_usage.reported_cost_usd is not None
        ):
            total_estimate = round(
                tikhub_estimate + model_usage.reported_cost_usd,
                8,
            )
        per_candidate = (
            round(total_estimate / candidate_count, 8)
            if total_estimate is not None and candidate_count > 0
            else None
        )
        return TopicCostSummary(
            tikhub_request_budget=self.data_provider.request_budget,
            tikhub_request_count=len(calls),
            tikhub_success_count=succeeded,
            tikhub_calls=list(calls),
            estimated_tikhub_cost_usd=tikhub_estimate,
            estimated_total_cost_usd=total_estimate,
            estimated_cost_per_candidate_usd=per_candidate,
            tikhub_cost_basis=basis,
            model_usage=model_usage,
        )

    @staticmethod
    def _editor_evidence(evidence: list[TopicEvidence]) -> list[TopicEvidence]:
        terms = [
            item for item in evidence
            if item.evidence_type == TopicEvidenceType.TREND_TERM
        ][:20]
        quality_tiers = {
            TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT,
            TopicEvidenceTier.HIGH_HEAT_BREAKOUT,
        }
        videos = [
            item for item in evidence
            if item.evidence_type == TopicEvidenceType.VIDEO
            and item.quality_tier in quality_tiers
        ]
        tier_order = {
            TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT: 0,
            TopicEvidenceTier.HIGH_HEAT_BREAKOUT: 1,
        }

        def evidence_rank(item: TopicEvidence) -> tuple:
            metrics = item.metrics
            age_hours = (
                metrics.published_age_hours
                if metrics and metrics.published_age_hours is not None
                else float("inf")
            )
            if age_hours <= 24:
                freshness_bucket = 0
            elif age_hours <= 72:
                freshness_bucket = 1
            else:
                freshness_bucket = 2
            return (
                tier_order[item.quality_tier],
                freshness_bucket,
                -(
                    metrics.average_daily_plays
                    if metrics and metrics.average_daily_plays
                    else 0
                ),
                age_hours,
                -(metrics.play_follower_ratio if metrics and metrics.play_follower_ratio else 0),
                -(metrics.play_count if metrics else 0),
                item.source_rank or 999,
            )

        videos.sort(key=evidence_rank)
        return [*terms, *videos[:30]]

    @staticmethod
    def _candidate_id(title: str, angle: str) -> str:
        digest = hashlib.sha256(
            f"{title}|{angle}".encode("utf-8")
        ).hexdigest()
        return f"topic_{digest[:12]}"

    @staticmethod
    def _build_candidates(proposals, evidence: list[TopicEvidence]) -> list[TopicCandidate]:
        evidence_by_id = {item.id: item for item in evidence}
        candidates: list[TopicCandidate] = []
        seen_titles: set[str] = set()
        for rank, proposal in enumerate(proposals, start=1):
            refs = list(dict.fromkeys(proposal.evidence_refs))
            unknown = set(refs) - set(evidence_by_id)
            if unknown:
                raise QualityGateFailed(
                    f"选题模型引用了不存在的抖音证据：{sorted(unknown)}"
                )
            if len(refs) < 2:
                raise QualityGateFailed("候选选题至少需要两条抖音研究证据")
            qualified_refs = [
                ref
                for ref in refs
                if evidence_by_id[ref].evidence_type == TopicEvidenceType.VIDEO
                and evidence_by_id[ref].quality_tier in {
                    TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT,
                    TopicEvidenceTier.HIGH_HEAT_BREAKOUT,
                }
            ]
            if len(qualified_refs) < 2:
                raise QualityGateFailed("每个候选至少需要两条独立的爆款视频共同验证")
            if not any(
                evidence_by_id[ref].quality_tier
                == TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT
                for ref in qualified_refs
            ):
                raise QualityGateFailed("每个候选至少需要一条低粉爆款视频")
            normalized_title = re.sub(
                r"[^\w\u4e00-\u9fff]+", "", proposal.title.casefold()
            )
            if not normalized_title:
                raise QualityGateFailed("候选标题必须包含可读的文字")
            if normalized_title in seen_titles:
                raise QualityGateFailed("选题模型返回了重复候选")
            seen_titles.add(normalized_title)
            candidates.append(TopicCandidate(
                id=TopicResearchService._candidate_id(
                    proposal.title, proposal.editorial_angle
                ),
                rank=rank,
                content_pillar=proposal.content_pillar,
                title=proposal.title,
                parent_question=proposal.parent_question,
                editorial_angle=proposal.editorial_angle,
                opening_hook=proposal.opening_hook,
                why_now=proposal.why_now,
                evidence_refs=refs,
                risk_note=proposal.risk_note,
            ))
        if len(candidates) != 5:
            raise QualityGateFailed("本轮没有形成完整的 5 个高质量候选")
        if len({item.content_pillar for item in candidates}) < 4:
            raise QualityGateFailed("五个候选至少需要覆盖四个不同的家庭教育内容支柱")
        referenced_videos = {
            ref
            for candidate in candidates
            for ref in candidate.evidence_refs
            if evidence_by_id[ref].evidence_type == TopicEvidenceType.VIDEO
            and evidence_by_id[ref].quality_tier in {
                TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT,
                TopicEvidenceTier.HIGH_HEAT_BREAKOUT,
            }
        }
        if len(referenced_videos) < 8:
            raise QualityGateFailed("五个候选不能反复依赖同一批证据，至少需引用八条不同爆款视频")
        low_follower_videos = {
            ref
            for ref in referenced_videos
            if evidence_by_id[ref].quality_tier
            == TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT
        }
        if len(low_follower_videos) < 5:
            raise QualityGateFailed("五个候选合计至少需要引用五条不同的低粉爆款视频")
        top_low_follower_groups = [
            {
                ref
                for ref in candidate.evidence_refs
                if evidence_by_id[ref].quality_tier
                == TopicEvidenceTier.LOW_FOLLOWER_BREAKOUT
            }
            for candidate in candidates[:3]
        ]
        has_distinct_top_evidence = any(
            len({first, second, third}) == 3
            for first in top_low_follower_groups[0]
            for second in top_low_follower_groups[1]
            for third in top_low_follower_groups[2]
        )
        if not has_distinct_top_evidence:
            raise QualityGateFailed("排名前三位的候选必须分别引用不同的低粉爆款视频")
        return candidates

    async def execute(
        self,
        run_id: str,
        actor: Actor,
        progress: ProgressReporter | None = None,
    ) -> TopicResearchRun:
        run = await self.get_run(run_id, actor)
        if run.status != TopicResearchStatus.RUNNING:
            raise InvalidTransition("本轮选题研究已结束")
        ledger_lock = asyncio.Lock()

        async def persist_calls(calls) -> None:
            async with ledger_lock:
                current = await self.get_run(run_id, actor)
                if current.status != TopicResearchStatus.RUNNING:
                    raise InvalidTransition("本轮选题研究已结束，不能继续写入调用账本")
                current.cost = self._cost_summary(
                    calls,
                    model_usage=current.cost.model_usage,
                )
                await self._save(current, actor)

        async def persist_model_usage(model_usage) -> None:
            async with ledger_lock:
                current = await self.get_run(run_id, actor)
                if current.status != TopicResearchStatus.RUNNING:
                    raise InvalidTransition("本轮选题研究已结束，不能继续写入模型账本")
                current.cost = self._cost_summary(
                    current.cost.tikhub_calls,
                    model_usage=model_usage,
                )
                await self._save(current, actor)

        try:
            collection = await self.data_provider.collect_family_education(
                progress,
                on_calls=persist_calls,
            )
        except TopicCollectionFailed as exc:
            run = await self.get_run(run_id, actor)
            calls = (
                run.cost.tikhub_calls
                if len(run.cost.tikhub_calls) > len(exc.calls)
                else exc.calls
            )
            run.cost = self._cost_summary(calls)
            await self._save(run, actor)
            raise
        run = await self.get_run(run_id, actor)
        run.valid_through = collection.valid_through
        run.evidence = collection.evidence
        run.warnings = collection.warnings[:20]
        run.cost = self._cost_summary(collection.calls)
        run = await self._save(run, actor)

        self._report(
            progress,
            message="编辑模型正在聚类并提出五个内容角度…",
            stage="topic_editorial",
            percent=78,
        )
        try:
            editorial = await self.editor.propose(
                self._editor_evidence(run.evidence),
                valid_through=run.valid_through,
                on_usage=persist_model_usage,
            )
        except TopicEditorialFailed as exc:
            run = await self.get_run(run.id, actor)
            run.cost = self._cost_summary(
                run.cost.tikhub_calls,
                model_usage=exc.usage,
            )
            await self._save(run, actor)
            raise
        run = await self.get_run(run.id, actor)
        # 先持久化已经发生的模型费用；即使后续质量门禁拒绝候选，成本也不会丢失。
        run.cost = self._cost_summary(
            run.cost.tikhub_calls,
            model_usage=editorial.usage,
        )
        run = await self._save(run, actor)
        run.candidates = self._build_candidates(editorial.proposals, run.evidence)
        run.cost = self._cost_summary(
            run.cost.tikhub_calls,
            model_usage=editorial.usage,
            candidate_count=len(run.candidates),
        )
        run.status = TopicResearchStatus.READY
        run.error = ""
        self._report(
            progress,
            message="五个候选已形成，等待人工选择…",
            stage="topic_review",
            percent=96,
        )
        return await self._save(run, actor)

    async def mark_failed(
        self, run_id: str, error: str, actor: Actor
    ) -> TopicResearchRun:
        run = await self.get_run(run_id, actor)
        if run.status == TopicResearchStatus.READY:
            return run
        run.status = TopicResearchStatus.FAILED
        run.error = str(error or "选题研究失败")[:2000]
        if (
            run.cost.model_usage is None
            and run.cost.estimated_tikhub_cost_usd is not None
        ):
            # 数据阶段失败且模型尚未调用时，TikHub 规划成本就是本轮已知总成本。
            run.cost.estimated_total_cost_usd = (
                run.cost.estimated_tikhub_cost_usd
            )
        return await self._save(run, actor)

    async def select_candidate(
        self,
        run_id: str,
        candidate_id: str,
        expected_revision: int,
        actor: Actor,
    ) -> TopicResearchRun:
        run = await self.get_run(run_id, actor)
        self._assert_revision(run.revision, expected_revision)
        if run.status != TopicResearchStatus.READY:
            raise InvalidTransition("只有研究完成后才能采用选题")
        if not any(item.id == candidate_id for item in run.candidates):
            raise QualityGateFailed("候选选题不存在")
        run.selected_candidate_id = candidate_id
        run.selected_by = actor.username
        run.selected_at = timestamp()
        return await self._save(run, actor)

    async def fail_if_interrupted(
        self, run_id: str, actor: Actor
    ) -> TopicResearchRun:
        run = await self.get_run(run_id, actor)
        if run.status != TopicResearchStatus.RUNNING:
            return run
        run.status = TopicResearchStatus.FAILED
        if (
            run.cost.model_usage is None
            and run.cost.estimated_tikhub_cost_usd is not None
        ):
            run.cost.estimated_total_cost_usd = (
                run.cost.estimated_tikhub_cost_usd
            )
        run.error = (
            "服务重启导致研究中断。为避免重复产生付费请求，系统没有自动重跑；"
            "中断前可能已产生但尚未回写的供应商费用，请先核对账单，再手动开始新一轮。"
        )
        run.warnings = list(dict.fromkeys([
            *run.warnings,
            "中断轮次的本地成本记录可能不完整，最终费用以 TikHub 与 OpenRouter 账单为准。",
        ]))[:20]
        return await self._save(run, actor)
