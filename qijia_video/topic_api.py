"""家庭教育选题研究 HTTP 边界。"""
from __future__ import annotations

from functools import wraps
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from qijia_video.auth import get_current_user, require_permission
from qijia_video.errors import ProviderUnavailable, QijiaVideoError
from qijia_video.runtime import actor_from_user
from qijia_video.topic_runtime import start_topic_research, topic_runtime


topic_api_router = APIRouter(
    prefix="/api/qijia-video/topic-research",
    tags=["齐家家庭教育选题研究"],
    dependencies=[Depends(require_permission("qijia_video"))],
)


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


class StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class StartTopicResearchRequest(StrictRequest):
    confirm_cost: Literal[True]


class SelectTopicCandidateRequest(StrictRequest):
    candidate_id: str = Field(pattern=r"^topic_[a-f0-9]{12}$")
    expected_revision: int = Field(ge=1)


@topic_api_router.get("/capabilities")
@boundary
async def topic_capabilities(user: dict = Depends(get_current_user)):
    return ok({
        **topic_runtime.capabilities(),
        "actor": actor_from_user(user).model_dump(mode="json"),
    })


@topic_api_router.post("/runs")
@boundary
async def create_topic_research_run(
    body: StartTopicResearchRequest,
    user: dict = Depends(get_current_user),
):
    del body
    capability = topic_runtime.capabilities()
    if not capability["ready"]:
        missing = "、".join(capability.get("missing_configuration") or [])
        raise ProviderUnavailable(
            "家庭教育选题研究尚未配置完成"
            + (f"：{missing}" if missing else "")
        )
    started = await start_topic_research(actor_from_user(user))
    return ok({
        "run": started.run.model_dump(mode="json"),
        "task_id": started.task_id,
        "reused": started.reused,
    }, "选题研究已开始")


@topic_api_router.get("/runs")
@boundary
async def list_topic_research_runs(
    limit: int = Query(30, ge=1, le=100),
    user: dict = Depends(get_current_user),
):
    rows = await topic_runtime.list_runs(
        actor_from_user(user), limit=limit
    )
    return ok([item.model_dump(mode="json") for item in rows])


@topic_api_router.get("/runs/{run_id}")
@boundary
async def get_topic_research_run(
    run_id: str,
    user: dict = Depends(get_current_user),
):
    run = await topic_runtime.get_run(run_id, actor_from_user(user))
    return ok(run.model_dump(mode="json"))


@topic_api_router.post("/runs/{run_id}/actions/select")
@boundary
async def select_topic_candidate(
    run_id: str,
    body: SelectTopicCandidateRequest,
    user: dict = Depends(get_current_user),
):
    run = await topic_runtime.service.select_candidate(
        run_id,
        body.candidate_id,
        body.expected_revision,
        actor_from_user(user),
    )
    return ok(run.model_dump(mode="json"), "选题已采用，请补充可靠来源")
