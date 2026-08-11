"""Read-only cost ledger normalization and team analysis."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable

from qijia_video.contracts import (
    BEIJING_TZ,
    ProviderUsageRecord,
    SEEDANCE_EFFICIENT_MODEL,
    SEEDANCE_FLAGSHIP_MODEL,
    SEEDANCE_RETIRED_MODEL,
    VideoJob,
)
from qijia_video.topic_contracts import TopicResearchRun


STAGE_LABELS = {
    "topic_data": "抖音数据研究",
    "topic_editor": "选题编辑模型",
    "script_generation": "脚本生成",
    "script_draft_generation": "脚本初稿",
    "script_critique": "独立审稿",
    "script_revision": "主编重写",
    "director_treatment": "导演视觉开发",
    "storyboard_generation": "分镜生成",
    "tts_synthesis": "旁白合成",
    "tts_preview": "配音试听",
    "seedream_image": "首帧与图片",
    "seedream_style_frame": "视觉开发样片",
    "seedance_video": "视频生成",
    "douyin_performance": "抖音效果回流",
}
PROVIDER_LABELS = {
    "tikhub": "TikHub",
    "openrouter": "OpenRouter",
    "volcengine-seed-tts-2.0": "豆包语音",
    "volcengine-seedream": "Seedream",
    "volcengine-seedance": "Seedance",
}
EVENT_DETAIL_LIMIT = 500
USD_TO_CNY_RATE = 6.7
PLAYBACK_VALUE_CNY_PER_1000 = 10.0
TARGET_ROI_MULTIPLE = 10.0


def _number(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) and result >= 0 else default


def _parse_time(value: str, fallback: datetime | None = None) -> datetime:
    raw = str(value or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=BEIJING_TZ)
            return parsed.astimezone(BEIJING_TZ)
        except ValueError:
            pass
    return fallback or datetime.now(BEIJING_TZ)


def _round_money(value: float) -> float:
    return round(max(0.0, float(value or 0)), 8)


def _is_mock_provider(value: str) -> bool:
    return "mock" in str(value or "").lower()


@dataclass(frozen=True)
class CostEvent:
    event_id: str
    scope_type: str
    scope_id: str
    title: str
    creator: str
    scope_status: str
    occurred_at: datetime
    stage: str
    provider: str
    model_id: str = ""
    request_id: str = ""
    request_count: int = 1
    total_tokens: int = 0
    quantity: float = 1
    unit: str = "request"
    reported_usd: float = 0
    reported_cny: float = 0
    estimated_usd: float = 0
    estimated_cny: float = 0
    priced: bool = False
    valuation: str = "unpriced"
    note: str = ""

    def public(self) -> dict:
        reported_cny = _round_money(
            self.reported_cny + self.reported_usd * USD_TO_CNY_RATE
        )
        estimated_cny = _round_money(
            self.estimated_cny + self.estimated_usd * USD_TO_CNY_RATE
        )
        return {
            "event_id": self.event_id,
            "scope_type": self.scope_type,
            "scope_id": self.scope_id,
            "title": self.title,
            "creator": self.creator,
            "scope_status": self.scope_status,
            "occurred_at": self.occurred_at.isoformat(timespec="seconds"),
            "stage": self.stage,
            "stage_label": STAGE_LABELS.get(self.stage, self.stage),
            "provider": self.provider,
            "provider_label": PROVIDER_LABELS.get(self.provider, self.provider),
            "model_id": self.model_id,
            "request_id": self.request_id,
            "request_count": self.request_count,
            "total_tokens": self.total_tokens,
            "quantity": self.quantity,
            "unit": self.unit,
            "reported_cny": reported_cny,
            "estimated_cny": estimated_cny,
            "accounted_cny": _round_money(reported_cny + estimated_cny),
            "priced": self.priced,
            "valuation": self.valuation,
            "note": self.note,
        }


def _job_title(job: VideoJob) -> str:
    return str(
        (job.input_snapshot.display_title if job.input_snapshot else "")
        or (job.source_card_snapshot or {}).get("title")
        or (job.script.video_title if job.script else "")
        or job.id
    )


def _record_event(job: VideoJob, record: ProviderUsageRecord) -> CostEvent:
    production_provider = not _is_mock_provider(record.provider)
    reported_usd = (
        _number(record.reported_cost)
        if production_provider
        and record.reported_currency == "USD"
        and record.reported_cost is not None
        else 0
    )
    reported_cny = (
        _number(record.reported_cost)
        if production_provider
        and record.reported_currency == "CNY"
        and record.reported_cost is not None
        else 0
    )
    estimated_usd = (
        _number(record.estimated_cost)
        if production_provider
        and record.estimated_currency == "USD"
        and record.estimated_cost is not None
        else 0
    )
    estimated_cny = (
        _number(record.estimated_cost)
        if production_provider
        and record.estimated_currency == "CNY"
        and record.estimated_cost is not None
        else 0
    )
    priced = production_provider and (
        record.reported_cost is not None or record.estimated_cost is not None
    )
    if not production_provider:
        valuation = "unpriced"
    elif record.reported_cost is not None:
        valuation = "reported"
    elif record.estimated_cost is not None:
        valuation = "estimated_snapshot"
    else:
        valuation = "unpriced"
    return CostEvent(
        event_id=record.usage_id,
        scope_type="video_job",
        scope_id=job.id,
        title=_job_title(job),
        creator=job.created_by or "未知",
        scope_status=job.state.value,
        occurred_at=_parse_time(
            record.occurred_at, _parse_time(job.updated_at or job.created_at)
        ),
        stage=record.operation,
        provider=record.provider,
        model_id=record.model_id,
        request_id=record.request_id,
        request_count=max(1, int(record.request_count or 1)),
        total_tokens=max(0, int(record.total_tokens or 0)),
        quantity=_number(record.quantity),
        unit=record.unit,
        reported_usd=reported_usd,
        reported_cny=reported_cny,
        estimated_usd=estimated_usd,
        estimated_cny=estimated_cny,
        priced=priced,
        valuation=valuation,
        note="；".join(
            item
            for item in (
                "测试 Provider 不计入生产费用"
                if not production_provider
                else "",
                record.pricing_basis,
                record.note,
            )
            if item
        ),
    )


def _job_events(
    job: VideoJob,
    *,
    seedream_price_per_image: float,
    seedance_price_per_million_tokens: float,
    seedance_model_prices_per_million_tokens: dict[str, float],
    tts_price_per_10000_characters: float,
) -> list[CostEvent]:
    events = [_record_event(job, item) for item in job.usage_records]
    recorded = {
        (item.operation, item.request_id)
        for item in job.usage_records
        if item.request_id
    }
    fallback_time = _parse_time(job.updated_at or job.created_at)

    for candidate in job.first_frame_candidates:
        key = ("seedream_image", candidate.candidate_id)
        if key in recorded:
            continue
        snapshot = candidate.estimated_cost_cny
        production_provider = not _is_mock_provider(candidate.model_id)
        amount = (
            _number(snapshot)
            if snapshot is not None
            else _number(seedream_price_per_image)
        )
        priced = production_provider and (
            snapshot is not None or seedream_price_per_image > 0
        )
        events.append(CostEvent(
            event_id=f"legacy_seedream_{job.id}_{candidate.candidate_id}",
            scope_type="video_job",
            scope_id=job.id,
            title=_job_title(job),
            creator=job.created_by or "未知",
            scope_status=job.state.value,
            occurred_at=_parse_time(candidate.created_at, fallback_time),
            stage="seedream_image",
            provider="volcengine-seedream",
            model_id=candidate.model_id,
            request_id=candidate.candidate_id,
            total_tokens=candidate.usage_total_tokens,
            quantity=1,
            unit="image",
            estimated_cny=amount if priced else 0,
            priced=priced,
            valuation=(
                "unpriced"
                if not priced
                else "estimated_snapshot"
                if snapshot is not None
                else "estimated_current_price"
            ),
            note=(
                "测试 Provider 不计入生产费用"
                if not production_provider
                else candidate.pricing_basis
                or "历史图片按当前配置刊例价补算；火山方舟账单优先"
            ),
        ))

    versions_by_task = {
        item.task.provider_task_id: item.created_at
        for item in job.visual_versions
        if item.task.provider_task_id
    }
    tasks = {}
    for task in [
        *job.video_tasks,
        *(item.task for item in job.visual_versions),
    ]:
        key = task.provider_task_id or task.request_fingerprint
        tasks[key] = task
    for task in tasks.values():
        record_key = ("seedance_video", task.provider_task_id)
        if task.provider_task_id and record_key in recorded:
            continue
        snapshot = task.estimated_cost_cny
        production_provider = not _is_mock_provider(task.provider)
        current_model_rate = _number(
            seedance_model_prices_per_million_tokens.get(
                task.model_id,
                seedance_price_per_million_tokens,
            )
        )
        can_estimate = (
            production_provider
            and task.usage_total_tokens > 0
            and (
                task.pricing_rate_cny_per_million is not None
                or current_model_rate > 0
            )
        )
        rate = (
            _number(task.pricing_rate_cny_per_million)
            if task.pricing_rate_cny_per_million is not None
            else current_model_rate
        )
        amount = (
            _number(snapshot)
            if snapshot is not None
            else (
                task.usage_total_tokens
                * rate
                / 1_000_000
                if can_estimate
                else 0
            )
        )
        priced = production_provider and (
            snapshot is not None or can_estimate
        )
        events.append(CostEvent(
            event_id=f"legacy_seedance_{job.id}_{task.request_fingerprint[:16]}",
            scope_type="video_job",
            scope_id=job.id,
            title=_job_title(job),
            creator=job.created_by or "未知",
            scope_status=job.state.value,
            occurred_at=_parse_time(
                task.created_at
                or versions_by_task.get(task.provider_task_id, ""),
                fallback_time,
            ),
            stage="seedance_video",
            provider=task.provider or "volcengine-seedance",
            model_id=task.model_id,
            request_id=task.provider_task_id,
            total_tokens=task.usage_total_tokens,
            quantity=1,
            unit="video",
            estimated_cny=_round_money(amount) if priced else 0,
            priced=priced,
            valuation=(
                "unpriced"
                if not priced
                else "estimated_snapshot"
                if snapshot is not None
                else "estimated_current_price"
            ),
            note=(
                "测试 Provider 不计入生产费用"
                if not production_provider
                else task.pricing_basis
                or (
                    "历史视频按当前配置刊例价补算；火山方舟账单优先"
                    if priced
                    else "供应商未保存 usage.total_tokens，金额待账单核对"
                )
            ),
        ))

    has_tts_records = any(
        item.operation == "tts_synthesis" for item in job.usage_records
    )
    if job.narration_manifest and not has_tts_records:
        characters = sum(
            len(item.text) for item in job.narration_manifest.segments
        )
        payload_bytes = len(
            "\n".join(
                item.text for item in job.narration_manifest.segments
            ).encode("utf-8")
        )
        inferred_requests = max(1, math.ceil(payload_bytes / 1000))
        production_provider = not _is_mock_provider(
            job.narration_manifest.provider
        )
        priced = production_provider and tts_price_per_10000_characters > 0
        events.append(CostEvent(
            event_id=f"legacy_tts_{job.id}",
            scope_type="video_job",
            scope_id=job.id,
            title=_job_title(job),
            creator=job.created_by or "未知",
            scope_status=job.state.value,
            occurred_at=fallback_time,
            stage="tts_synthesis",
            provider=job.narration_manifest.provider,
            model_id=job.narration_manifest.provider,
            request_count=inferred_requests,
            quantity=characters,
            unit="character",
            estimated_cny=(
                _round_money(
                    characters * tts_price_per_10000_characters / 10000
                )
                if priced
                else 0
            ),
            priced=priced,
            valuation="estimated_current_price" if priced else "unpriced",
            note=(
                "测试 Provider 不计入生产费用"
                if not production_provider
                else (
                    "历史任务根据已保存旁白字符数和当前刊例价补算；"
                    "分块次数为保守推断，供应商账单优先"
                )
            ),
        ))
    return events


def _topic_events(
    run: TopicResearchRun,
    *,
    tikhub_price_per_success_usd: float,
) -> list[CostEvent]:
    events: list[CostEvent] = []
    occurred_at = _parse_time(run.updated_at or run.created_at)
    title = f"家庭教育选题研究 · {run.valid_through or run.created_at[:10]}"
    success_count = max(0, int(run.cost.tikhub_success_count or 0))
    snapshot_total = run.cost.estimated_tikhub_cost_usd
    snapshot_unit = (
        _number(snapshot_total) / success_count
        if snapshot_total is not None and success_count > 0
        else None
    )
    for index, call in enumerate(run.cost.tikhub_calls, 1):
        unit_price = (
            snapshot_unit
            if snapshot_unit is not None
            else _number(tikhub_price_per_success_usd)
        )
        priced = not call.succeeded or unit_price > 0
        amount = unit_price if call.succeeded and priced else 0
        cost_note = (
            f"TikHub 规划价 ¥{unit_price * USD_TO_CNY_RATE:g}/成功请求；"
            f"美元按固定汇率 1 USD = ¥{USD_TO_CNY_RATE:g} 换算，供应商账单优先"
            if unit_price > 0
            else "当前未配置 TikHub 成功请求规划价，金额待供应商账单"
        )
        events.append(CostEvent(
            event_id=f"tikhub_{run.id}_{index}",
            scope_type="topic_research",
            scope_id=run.id,
            title=title,
            creator=run.created_by or "未知",
            scope_status=run.status.value,
            occurred_at=occurred_at,
            stage="topic_data",
            provider="tikhub",
            model_id=call.endpoint,
            request_id=call.request_id,
            request_count=1,
            quantity=1,
            unit="request",
            estimated_usd=_round_money(amount),
            priced=priced,
            valuation=(
                "estimated_snapshot"
                if snapshot_unit is not None
                else "estimated_current_price" if priced else "unpriced"
            ),
            note=cost_note,
        ))
    model = run.cost.model_usage
    if model and model.request_count:
        priced = model.reported_cost_usd is not None
        events.append(CostEvent(
            event_id=f"topic_editor_{run.id}",
            scope_type="topic_research",
            scope_id=run.id,
            title=title,
            creator=run.created_by or "未知",
            scope_status=run.status.value,
            occurred_at=occurred_at,
            stage="topic_editor",
            provider=model.provider or "openrouter",
            model_id=model.model,
            request_id=model.request_id,
            request_count=model.request_count,
            total_tokens=model.total_tokens,
            quantity=model.request_count,
            unit="request",
            reported_usd=(
                _round_money(model.reported_cost_usd)
                if model.reported_cost_usd is not None
                else 0
            ),
            priced=priced,
            valuation="reported" if priced else "unpriced",
            note=(
                "OpenRouter usage.cost 供应商回传金额"
                if priced
                else "供应商未回传金额，需与 OpenRouter Activity 对账"
            ),
        ))
    return events


def _totals(events: list[CostEvent]) -> dict:
    priced = sum(1 for item in events if item.priced)
    reported_cny = _round_money(sum(
        item.reported_cny + item.reported_usd * USD_TO_CNY_RATE
        for item in events
    ))
    estimated_cny = _round_money(sum(
        item.estimated_cny + item.estimated_usd * USD_TO_CNY_RATE
        for item in events
    ))
    return {
        "event_count": len(events),
        "request_count": sum(item.request_count for item in events),
        "total_tokens": sum(item.total_tokens for item in events),
        "reported_cny": reported_cny,
        "estimated_cny": estimated_cny,
        "accounted_cny": _round_money(reported_cny + estimated_cny),
        "priced_event_count": priced,
        "unpriced_event_count": len(events) - priced,
        "coverage_ratio": round(priced / len(events), 4) if events else 1.0,
    }


def build_video_job_cost_summary(
    job: VideoJob,
    *,
    seedream_price_per_image: float = 0.22,
    seedance_price_per_million_tokens: float = 4.2,
    seedance_model_prices_per_million_tokens: dict[str, float] | None = None,
    tts_price_per_10000_characters: float = 5.0,
) -> dict:
    """Calculate one job with exactly the same rules as the team cost ledger."""

    model_prices = {
        SEEDANCE_EFFICIENT_MODEL: 4.2,
        SEEDANCE_RETIRED_MODEL: 8.0,
        SEEDANCE_FLAGSHIP_MODEL: 46.0,
    }
    model_prices.update({
        str(model_id): _number(rate)
        for model_id, rate in (
            seedance_model_prices_per_million_tokens or {}
        ).items()
        if str(model_id).strip()
    })
    return _totals(_job_events(
        job,
        seedream_price_per_image=seedream_price_per_image,
        seedance_price_per_million_tokens=seedance_price_per_million_tokens,
        seedance_model_prices_per_million_tokens=model_prices,
        tts_price_per_10000_characters=tts_price_per_10000_characters,
    ))


def build_douyin_performance_analysis(
    job: VideoJob,
    *,
    seedream_price_per_image: float = 0.22,
    seedance_price_per_million_tokens: float = 4.2,
    seedance_model_prices_per_million_tokens: dict[str, float] | None = None,
    tts_price_per_10000_characters: float = 5.0,
) -> dict:
    totals = build_video_job_cost_summary(
        job,
        seedream_price_per_image=seedream_price_per_image,
        seedance_price_per_million_tokens=seedance_price_per_million_tokens,
        seedance_model_prices_per_million_tokens=(
            seedance_model_prices_per_million_tokens
        ),
        tts_price_per_10000_characters=tts_price_per_10000_characters,
    )
    snapshots = (
        job.douyin_performance.snapshots
        if job.douyin_performance
        else []
    )
    latest = snapshots[-1] if snapshots else None
    play_count = latest.play_count if latest else None
    accounted_cost = _number(totals["accounted_cny"])
    playback_value = (
        _round_money(play_count * PLAYBACK_VALUE_CNY_PER_1000 / 1000)
        if play_count is not None
        else None
    )
    target_views = math.ceil(
        accounted_cost
        * TARGET_ROI_MULTIPLE
        * 1000
        / PLAYBACK_VALUE_CNY_PER_1000
    )
    roi_multiple = (
        round(playback_value / accounted_cost, 4)
        if playback_value is not None and accounted_cost > 0
        else None
    )
    cost_complete = totals["unpriced_event_count"] == 0
    meets_accounted_target = bool(
        play_count is not None
        and accounted_cost > 0
        and play_count >= target_views
    )
    return {
        "platform": "douyin",
        "play_count": play_count,
        "like_count": latest.like_count if latest else None,
        "comment_count": latest.comment_count if latest else None,
        "share_count": latest.share_count if latest else None,
        "collect_count": latest.collect_count if latest else None,
        "observed_at": latest.observed_at if latest else "",
        "snapshot_count": len(snapshots),
        "accounted_cost_cny": _round_money(accounted_cost),
        "cost_coverage_ratio": totals["coverage_ratio"],
        "cost_event_count": totals["event_count"],
        "priced_event_count": totals["priced_event_count"],
        "unpriced_event_count": totals["unpriced_event_count"],
        "cost_complete": cost_complete,
        "playback_value_cny": playback_value,
        "roi_multiple": roi_multiple,
        "target_roi_multiple": TARGET_ROI_MULTIPLE,
        "target_views": target_views,
        "remaining_views": (
            max(0, target_views - play_count)
            if play_count is not None
            else target_views
        ),
        "target_achieved": meets_accounted_target and cost_complete,
        "target_achieved_provisional": (
            meets_accounted_target and not cost_complete
        ),
        "basis": "每千次播放按 ¥10 估值；目标 ROI 为 10 倍",
    }


def build_team_content_performance(
    jobs: list[VideoJob],
    *,
    days: int = 30,
    seedream_price_per_image: float = 0.22,
    seedance_price_per_million_tokens: float = 4.2,
    seedance_model_prices_per_million_tokens: dict[str, float] | None = None,
    tts_price_per_10000_characters: float = 5.0,
    now: datetime | None = None,
) -> dict:
    """Build a comparable cohort from manually bound Douyin publications.

    The selected period decides which publications belong to the cohort. Once a
    publication is included, both its latest cumulative play count and its
    whole-life video cost are used so the ROI numerator and denominator remain
    comparable. Refreshing a snapshot never moves an old publication into a
    newer cohort.
    """

    current = now or datetime.now(BEIJING_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    current = current.astimezone(BEIJING_TZ)
    safe_days = max(0, min(3650, int(days or 0)))
    cutoff = current - timedelta(days=safe_days) if safe_days else None

    packaged_jobs: list[VideoJob] = []
    for job in jobs:
        if job.state.value != "packaged":
            continue
        cohort_time = _parse_time(
            (
                job.douyin_performance.bound_at
                if job.douyin_performance
                else job.updated_at or job.created_at
            )
        )
        if cutoff is None or cohort_time >= cutoff:
            packaged_jobs.append(job)

    rows: list[dict] = []
    for job in packaged_jobs:
        performance = job.douyin_performance
        if performance is None:
            continue
        analysis = build_douyin_performance_analysis(
            job,
            seedream_price_per_image=seedream_price_per_image,
            seedance_price_per_million_tokens=(
                seedance_price_per_million_tokens
            ),
            seedance_model_prices_per_million_tokens=(
                seedance_model_prices_per_million_tokens
            ),
            tts_price_per_10000_characters=(
                tts_price_per_10000_characters
            ),
        )
        rows.append({
            "job_id": job.id,
            "title": _job_title(job),
            "creator": job.created_by or "未知",
            "video_id": performance.video_id,
            "video_url": performance.video_url,
            "video_title": performance.video_title,
            "author_name": performance.author_name,
            "bound_at": performance.bound_at,
            "updated_at": performance.updated_at,
            **analysis,
        })

    rows_by_video_id: dict[str, list[dict]] = {}
    for row in rows:
        rows_by_video_id.setdefault(row["video_id"], []).append(row)
    for bindings in rows_by_video_id.values():
        bindings.sort(
            key=lambda item: (
                _parse_time(item["bound_at"]),
                item["job_id"],
            )
        )
        canonical_job_id = bindings[0]["job_id"]
        for index, row in enumerate(bindings):
            row["duplicate_binding"] = index > 0
            row["duplicate_of_job_id"] = (
                canonical_job_id if index > 0 else ""
            )

    rows.sort(
        key=lambda item: (
            not bool(item["duplicate_binding"]),
            item["roi_multiple"] is not None,
            _number(item["roi_multiple"]),
            int(item["play_count"] or 0),
            item["observed_at"],
        ),
        reverse=True,
    )
    unique_rows = [
        item for item in rows if not item["duplicate_binding"]
    ]
    bound_job_count = len(rows)
    tracked_count = len(unique_rows)
    duplicate_binding_count = bound_job_count - tracked_count
    packaged_count = len(packaged_jobs)
    total_play_count = sum(
        int(item["play_count"] or 0) for item in unique_rows
    )
    total_cost = _round_money(sum(
        _number(item["accounted_cost_cny"]) for item in unique_rows
    ))
    total_value = _round_money(sum(
        _number(item["playback_value_cny"]) for item in unique_rows
    ))
    total_cost_events = sum(
        int(item["cost_event_count"] or 0) for item in unique_rows
    )
    total_priced_events = sum(
        int(item["priced_event_count"] or 0) for item in unique_rows
    )
    total_unpriced_events = sum(
        int(item["unpriced_event_count"] or 0) for item in unique_rows
    )
    portfolio_target_views = math.ceil(
        total_cost
        * TARGET_ROI_MULTIPLE
        * 1000
        / PLAYBACK_VALUE_CNY_PER_1000
    )
    portfolio_roi = (
        round(total_value / total_cost, 4)
        if total_cost > 0
        else None
    )
    target_achieved_count = sum(
        bool(item["target_achieved"]) for item in unique_rows
    )
    provisional_target_achieved_count = sum(
        bool(item["target_achieved_provisional"]) for item in unique_rows
    )
    portfolio_meets_accounted_target = bool(
        tracked_count
        and total_cost > 0
        and total_play_count >= portfolio_target_views
    )
    latest_observed = max(
        (
            _parse_time(item["observed_at"])
            for item in unique_rows
            if item["observed_at"]
        ),
        default=None,
    )
    return {
        "platform": "douyin",
        "period": {
            "days": safe_days,
            "label": "全部记录" if safe_days == 0 else f"最近 {safe_days} 天",
            "cohort_basis": (
                "已绑定视频按首次绑定时间纳入；未绑定的已打包视频按任务更新时间纳入。"
                "手动刷新不会改变视频所属时间范围。"
            ),
        },
        "summary": {
            "packaged_video_count": packaged_count,
            "bound_job_count": bound_job_count,
            "tracked_video_count": tracked_count,
            "duplicate_binding_count": duplicate_binding_count,
            "untracked_packaged_count": max(
                0, packaged_count - bound_job_count
            ),
            "tracking_coverage_ratio": (
                round(tracked_count / packaged_count, 4)
                if packaged_count
                else 0
            ),
            "snapshot_count": sum(
                int(item["snapshot_count"] or 0) for item in unique_rows
            ),
            "total_play_count": total_play_count,
            "accounted_cost_cny": total_cost,
            "playback_value_cny": total_value,
            "roi_multiple": portfolio_roi,
            "target_roi_multiple": TARGET_ROI_MULTIPLE,
            "target_views": portfolio_target_views,
            "remaining_views": max(
                0, portfolio_target_views - total_play_count
            ),
            "target_achieved": bool(
                portfolio_meets_accounted_target
                and total_unpriced_events == 0
            ),
            "target_achieved_provisional": bool(
                portfolio_meets_accounted_target
                and total_unpriced_events > 0
            ),
            "target_achieved_count": target_achieved_count,
            "provisional_target_achieved_count": (
                provisional_target_achieved_count
            ),
            "target_achievement_rate": (
                round(target_achieved_count / tracked_count, 4)
                if tracked_count
                else 0
            ),
            "cost_event_count": total_cost_events,
            "priced_event_count": total_priced_events,
            "unpriced_event_count": total_unpriced_events,
            "cost_coverage_ratio": (
                round(total_priced_events / total_cost_events, 4)
                if total_cost_events
                else 0
            ),
            "cost_complete": total_unpriced_events == 0,
            "provisional_video_count": sum(
                not bool(item["cost_complete"]) for item in unique_rows
            ),
            "latest_observed_at": (
                latest_observed.isoformat(timespec="seconds")
                if latest_observed
                else ""
            ),
        },
        "rows": rows,
        "basis": {
            "playback_value": "播放量 ÷ 1000 × ¥10",
            "portfolio_roi": "纳入视频播放价值合计 ÷ 纳入视频全生命周期已计成本",
            "target": "10 倍 ROI；团队目标播放量按合计成本计算",
            "cost_scope": (
                "只包含每条视频自身的模型、数据 API 与播放回流成本；"
                "不分摊选题研究成本"
            ),
            "data_scope": (
                "只读取已手填绑定的抖音作品最新快照；"
                "播放量采用 TikHub 星图总播放口径（包含可能的投流播放）；"
                "不采集小红书、视频号或 APP 下载注册；"
                "同一抖音作品重复绑定时只按最早绑定任务计入团队合计"
            ),
        },
    }


def _group_rows(
    events: list[CostEvent],
    key: Callable[[CostEvent], str],
    label: Callable[[str], str] | None = None,
) -> list[dict]:
    groups: dict[str, list[CostEvent]] = {}
    for event in events:
        groups.setdefault(key(event) or "unknown", []).append(event)
    rows = [
        {
            "key": group_key,
            "label": label(group_key) if label else group_key,
            **_totals(items),
        }
        for group_key, items in groups.items()
    ]
    return sorted(rows, key=lambda item: (-item["request_count"], item["label"]))


def _bucket_key(value: datetime, days: int) -> str:
    if days == 0 or days > 180:
        return value.strftime("%Y-%m")
    if days > 31:
        monday = value.date() - timedelta(days=value.weekday())
        return monday.isoformat()
    return value.date().isoformat()


def build_cost_analysis(
    jobs: list[VideoJob],
    topic_runs: list[TopicResearchRun],
    *,
    days: int = 30,
    seedream_price_per_image: float = 0.22,
    seedance_price_per_million_tokens: float = 4.2,
    seedance_model_prices_per_million_tokens: dict[str, float] | None = None,
    tts_price_per_10000_characters: float = 5.0,
    tikhub_price_per_success_usd: float = 0.001,
    tikhub_performance_price_per_success_usd: float = 0.002,
    now: datetime | None = None,
    source_limit: int = 500,
) -> dict:
    current = now or datetime.now(BEIJING_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING_TZ)
    current = current.astimezone(BEIJING_TZ)
    safe_days = max(0, min(3650, int(days or 0)))
    cutoff = current - timedelta(days=safe_days) if safe_days else None
    model_prices = {
        SEEDANCE_EFFICIENT_MODEL: 4.2,
        SEEDANCE_RETIRED_MODEL: 8.0,
        SEEDANCE_FLAGSHIP_MODEL: 46.0,
    }
    model_prices.update({
        str(model_id): _number(rate)
        for model_id, rate in (
            seedance_model_prices_per_million_tokens or {}
        ).items()
        if str(model_id).strip()
    })
    all_events = [
        event
        for job in jobs
        for event in _job_events(
            job,
            seedream_price_per_image=seedream_price_per_image,
            seedance_price_per_million_tokens=seedance_price_per_million_tokens,
            seedance_model_prices_per_million_tokens=model_prices,
            tts_price_per_10000_characters=tts_price_per_10000_characters,
        )
    ] + [
        event
        for run in topic_runs
        for event in _topic_events(
            run,
            tikhub_price_per_success_usd=tikhub_price_per_success_usd,
        )
    ]
    events = [
        item for item in all_events
        if cutoff is None or item.occurred_at >= cutoff
    ]
    event_scope_ids = {(item.scope_type, item.scope_id) for item in events}

    content: list[dict] = []
    for job in jobs:
        relevant = [
            item for item in events
            if item.scope_type == "video_job" and item.scope_id == job.id
        ]
        source_time = _parse_time(job.updated_at or job.created_at)
        if not relevant and cutoff is not None and source_time < cutoff:
            continue
        content.append({
            "scope_type": "video_job",
            "scope_id": job.id,
            "title": _job_title(job),
            "creator": job.created_by or "未知",
            "status": job.state.value,
            "latest_at": max(
                [item.occurred_at for item in relevant] or [source_time]
            ).isoformat(timespec="seconds"),
            **_totals(relevant),
        })
    for run in topic_runs:
        relevant = [
            item for item in events
            if item.scope_type == "topic_research" and item.scope_id == run.id
        ]
        source_time = _parse_time(run.updated_at or run.created_at)
        if not relevant and cutoff is not None and source_time < cutoff:
            continue
        content.append({
            "scope_type": "topic_research",
            "scope_id": run.id,
            "title": f"家庭教育选题研究 · {run.valid_through or run.created_at[:10]}",
            "creator": run.created_by or "未知",
            "status": run.status.value,
            "latest_at": max(
                [item.occurred_at for item in relevant] or [source_time]
            ).isoformat(timespec="seconds"),
            **_totals(relevant),
        })
    content.sort(key=lambda item: item["latest_at"], reverse=True)

    daily_groups: dict[str, list[CostEvent]] = {}
    for event in events:
        daily_groups.setdefault(
            _bucket_key(event.occurred_at, safe_days), []
        ).append(event)
    timeline = [
        {"period": key, **_totals(items)}
        for key, items in sorted(daily_groups.items())
    ]
    summary = _totals(events)
    video_totals = _totals([
        item for item in events if item.scope_type == "video_job"
    ])
    topic_totals = _totals([
        item for item in events if item.scope_type == "topic_research"
    ])
    completed_video_count = sum(
        item["scope_type"] == "video_job" and item["status"] == "packaged"
        for item in content
    )
    summary.update({
        "video_job_count": sum(
            item["scope_type"] == "video_job" for item in content
        ),
        "completed_video_count": completed_video_count,
        "topic_run_count": sum(
            item["scope_type"] == "topic_research" for item in content
        ),
        "video": video_totals,
        "topic_research": topic_totals,
        "video_cost_per_packaged": {
            "denominator": completed_video_count,
            "accounted_cny": (
                _round_money(video_totals["accounted_cny"] / completed_video_count)
                if completed_video_count else None
            ),
            "includes_in_progress_spend": True,
        },
    })
    ordered_events = sorted(
        events, key=lambda value: value.occurred_at, reverse=True
    )
    return {
        "generated_at": current.isoformat(timespec="seconds"),
        "currency": {
            "display": "CNY",
            "usd_to_cny_rate": USD_TO_CNY_RATE,
            "basis": "固定经营分析汇率：1 USD = ¥6.7",
        },
        "period": {
            "days": safe_days,
            "label": "全部记录" if safe_days == 0 else f"最近 {safe_days} 天",
            "bucket": (
                "month" if safe_days == 0 or safe_days > 180
                else "week" if safe_days > 31 else "day"
            ),
        },
        "summary": summary,
        "performance": build_team_content_performance(
            jobs,
            days=safe_days,
            seedream_price_per_image=seedream_price_per_image,
            seedance_price_per_million_tokens=(
                seedance_price_per_million_tokens
            ),
            seedance_model_prices_per_million_tokens=model_prices,
            tts_price_per_10000_characters=(
                tts_price_per_10000_characters
            ),
            now=current,
        ),
        "timeline": timeline,
        "by_provider": _group_rows(
            events,
            lambda item: item.provider,
            lambda value: PROVIDER_LABELS.get(value, value),
        ),
        "by_stage": _group_rows(
            events,
            lambda item: item.stage,
            lambda value: STAGE_LABELS.get(value, value),
        ),
        "by_creator": _group_rows(events, lambda item: item.creator),
        "content": content,
        "events": [item.public() for item in ordered_events[:EVENT_DETAIL_LIMIT]],
        "coverage": {
            "source_limit": source_limit,
            "job_limit_reached": len(jobs) >= source_limit,
            "topic_limit_reached": len(topic_runs) >= source_limit,
            "event_detail_limit": EVENT_DETAIL_LIMIT,
            "event_detail_limit_reached": len(ordered_events) > EVENT_DETAIL_LIMIT,
            "has_unpriced_events": summary["unpriced_event_count"] > 0,
            "notes": [
                "页面只显示人民币；所有美元成本按固定经营分析汇率 1 USD = ¥6.7 换算。",
                "OpenRouter 使用响应内 usage.cost 换算人民币；不包含充值手续费、税费或账外调整。",
                "TikHub 按成功请求规划价换算人民币；端点价格、阶梯折扣与账单优先。",
                "Seedream、Seedance 和豆包语音按调用发生时保存的刊例价估算；历史任务缺少价格快照时才使用当前配置补算。",
                "成本账本上线前的脚本与分镜没有保存 OpenRouter usage，历史金额无法可靠反推，需到 OpenRouter Activity 对账。",
                "未回传 tokens、金额或发生网络中断的请求保留为待对账，不按 0 元冒充完整成本。",
                "范围仅包含内容生产相关模型与数据 API，不包含 Railway、TOS、带宽、人工和购买积分手续费。",
            ],
        },
        "pricing": [
            {
                "provider": "TikHub（选题研究 / 短链解析）",
                "rate": (
                    f"¥{_number(tikhub_price_per_success_usd) * USD_TO_CNY_RATE:g}"
                    "/成功请求"
                ),
                "currency": "CNY",
                "valuation": "estimated",
                "source": "https://docs.tikhub.io/4579905m0",
            },
            {
                "provider": "TikHub（抖音效果回流）",
                "rate": (
                    f"¥{_number(tikhub_performance_price_per_success_usd) * USD_TO_CNY_RATE:g}"
                    "/成功请求"
                ),
                "currency": "CNY",
                "valuation": "estimated",
                "source": "https://docs.tikhub.io/493289600e0",
            },
            {
                "provider": "OpenRouter",
                "rate": f"响应 usage.cost × ¥{USD_TO_CNY_RATE:g}/USD",
                "currency": "CNY",
                "valuation": "reported",
                "source": "https://openrouter.ai/docs/cookbook/administration/usage-accounting",
            },
            {
                "provider": "Seedream",
                "rate": f"¥{_number(seedream_price_per_image):g}/张",
                "currency": "CNY",
                "valuation": "estimated",
                "source": "https://www.volcengine.com/product/yunque",
            },
            {
                "provider": "Seedance 1.0 Pro Fast",
                "rate": (
                    f"¥{model_prices[SEEDANCE_EFFICIENT_MODEL]:g}/百万 tokens"
                    "（无声视频）"
                ),
                "currency": "CNY",
                "valuation": "estimated",
                "source": "https://www.volcengine.com/product/doubao/",
            },
            {
                "provider": "Seedance 1.5 Pro（历史）",
                "rate": (
                    f"¥{model_prices[SEEDANCE_RETIRED_MODEL]:g}/百万 tokens"
                    "（无声视频）"
                ),
                "currency": "CNY",
                "valuation": "estimated",
                "source": "https://www.volcengine.com/product/doubao/",
            },
            {
                "provider": "Seedance 2.0",
                "rate": (
                    f"¥{model_prices[SEEDANCE_FLAGSHIP_MODEL]:g}/百万 tokens"
                    "（无视频输入）"
                ),
                "currency": "CNY",
                "valuation": "estimated",
                "source": "https://www.volcengine.com/product/doubao/",
            },
            {
                "provider": "豆包语音",
                "rate": f"¥{_number(tts_price_per_10000_characters):g}/万字符",
                "currency": "CNY",
                "valuation": "estimated",
                "source": "https://www.volcengine.com/product/yunque",
            },
        ],
        "source_scope_count": len(event_scope_ids),
    }
