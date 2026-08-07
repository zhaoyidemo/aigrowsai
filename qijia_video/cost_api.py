"""Team-visible, read-only cost analysis HTTP boundary."""
from __future__ import annotations

import asyncio
from functools import wraps
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from qijia_video.auth import get_current_user, require_permission
from qijia_video.cost_analysis import build_cost_analysis
from qijia_video.contracts import (
    SEEDANCE_EFFICIENT_MODEL,
    SEEDANCE_FLAGSHIP_MODEL,
    SEEDANCE_RETIRED_MODEL,
)
from qijia_video.errors import QijiaVideoError
from qijia_video.runtime import actor_from_user, runtime
from qijia_video.settings import settings
from qijia_video.topic_runtime import topic_runtime


WEB_DIR = Path(__file__).resolve().parent / "web"
SOURCE_LIMIT = 500

cost_api_router = APIRouter(
    prefix="/api/qijia-video/costs",
    tags=["齐家内容成本分析"],
    dependencies=[Depends(require_permission("qijia_video"))],
)
cost_page_router = APIRouter(tags=["齐家内容成本分析页面"])


def ok(data=None, message: str = "") -> dict:
    return {"code": 0, "data": data, "message": message}


def boundary(func):
    @wraps(func)
    async def wrapped(*args, **kwargs):
        try:
            return await func(*args, **kwargs)
        except QijiaVideoError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc

    return wrapped


@cost_page_router.get(
    "/qijia-video/costs",
    include_in_schema=False,
    dependencies=[Depends(require_permission("qijia_video"))],
)
async def cost_analysis_page():
    return HTMLResponse(
        (WEB_DIR / "costs.html").read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@cost_api_router.get("")
@boundary
async def cost_analysis(
    response: Response,
    days: int = Query(default=30, ge=0, le=3650),
    user: dict = Depends(get_current_user),
):
    actor = actor_from_user(user)
    # Read the aggregates directly. TopicRuntime.list_runs performs interrupted
    # task reconciliation and may write, which a reporting endpoint must never do.
    jobs, topic_runs = await asyncio.gather(
        runtime.service.list_jobs(actor, limit=SOURCE_LIMIT),
        topic_runtime.service.list_runs(actor, limit=SOURCE_LIMIT),
    )
    response.headers["Cache-Control"] = "no-store"
    analysis = build_cost_analysis(
        jobs,
        topic_runs,
        days=days,
        seedream_price_per_image=(
            settings.QIJIA_VIDEO_SEEDREAM_PRICE_PER_IMAGE
        ),
        seedance_price_per_million_tokens=(
            settings.QIJIA_VIDEO_SEEDANCE_PRICE_PER_MILLION
        ),
        seedance_model_prices_per_million_tokens={
            SEEDANCE_EFFICIENT_MODEL: (
                settings.QIJIA_VIDEO_SEEDANCE_10_FAST_PRICE_PER_MILLION
            ),
            SEEDANCE_RETIRED_MODEL: (
                settings.QIJIA_VIDEO_SEEDANCE_15_PRICE_PER_MILLION
            ),
            SEEDANCE_FLAGSHIP_MODEL: (
                settings.QIJIA_VIDEO_SEEDANCE_20_PRICE_PER_MILLION
            ),
        },
        tts_price_per_10000_characters=(
            settings.QIJIA_VIDEO_TTS_PRICE_PER_10000_CHARACTERS
        ),
        tikhub_price_per_success_usd=(
            settings.QIJIA_TOPIC_TIKHUB_ESTIMATED_USD_PER_SUCCESS
        ),
        source_limit=SOURCE_LIMIT,
    )
    jobs_by_id = {job.id: job for job in jobs}
    username = str(user.get("username") or "")
    is_admin = user.get("role") == "admin"
    performance = analysis.get("performance") or {}
    for row in performance.get("rows") or []:
        job = jobs_by_id.get(str(row.get("job_id") or ""))
        row["revision"] = int(job.revision) if job else 0
        row["can_refresh"] = bool(
            job
            and (is_admin or str(job.created_by or "") == username)
        )
    performance["refresh"] = {
        **runtime.capabilities().get("douyin_performance", {}),
        "confirmation_required": True,
    }
    analysis["performance"] = performance
    return ok(analysis)
