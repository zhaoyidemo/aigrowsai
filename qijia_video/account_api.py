"""Administrator-only colleague account management."""
from __future__ import annotations

from functools import wraps
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from qijia_video import accounts
from qijia_video.auth import require_admin
from qijia_video.errors import QijiaVideoError


WEB_DIR = Path(__file__).resolve().parent / "web"
account_api_router = APIRouter(
    prefix="/api/qijia-video/accounts",
    tags=["齐家工作台账号管理"],
    dependencies=[Depends(require_admin)],
)
account_page_router = APIRouter(tags=["齐家工作台账号管理页面"])


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
    # Passwords are opaque values; do not silently trim them.
    model_config = ConfigDict(extra="forbid")


class CreateMemberRequest(StrictRequest):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=12, max_length=128)
    is_active: bool = True
    can_use_workbench: bool = True


class UpdateMemberRequest(StrictRequest):
    expected_revision: int = Field(ge=1)
    is_active: bool
    can_use_workbench: bool


class ResetMemberPasswordRequest(StrictRequest):
    expected_revision: int = Field(ge=1)
    new_password: str = Field(min_length=12, max_length=128)


@account_page_router.get(
    "/qijia-video/accounts",
    include_in_schema=False,
    dependencies=[Depends(require_admin)],
)
async def account_management_page():
    return FileResponse(
        WEB_DIR / "accounts.html",
        headers={"Cache-Control": "no-store"},
    )


@account_api_router.get("")
@boundary
async def list_accounts(admin: dict = Depends(require_admin)):
    return ok({
        "administrator": {
            "username": str(admin.get("username") or "admin"),
            "role": "admin",
        },
        "members": await accounts.list_members(),
        "available_permissions": [{
            "id": accounts.WORKBENCH_PERMISSION,
            "label": "使用齐家内容工作台",
        }],
    })


@account_api_router.post("")
@boundary
async def create_account(
    body: CreateMemberRequest,
    admin: dict = Depends(require_admin),
):
    member = await accounts.create_member(
        username=body.username,
        password=body.password,
        is_active=body.is_active,
        can_use_workbench=body.can_use_workbench,
        actor_username=str(admin.get("username") or ""),
    )
    return ok(member, "同事账号已创建")


@account_api_router.patch("/{user_id}")
@boundary
async def update_account(
    user_id: int,
    body: UpdateMemberRequest,
    admin: dict = Depends(require_admin),
):
    member = await accounts.update_member(
        user_id,
        expected_revision=body.expected_revision,
        is_active=body.is_active,
        can_use_workbench=body.can_use_workbench,
        actor_username=str(admin.get("username") or ""),
    )
    return ok(member, "账号权限已更新")


@account_api_router.post("/{user_id}/actions/reset-password")
@boundary
async def reset_account_password(
    user_id: int,
    body: ResetMemberPasswordRequest,
    admin: dict = Depends(require_admin),
):
    member = await accounts.reset_member_password(
        user_id,
        expected_revision=body.expected_revision,
        new_password=body.new_password,
        actor_username=str(admin.get("username") or ""),
    )
    return ok(member, "密码已重置，原登录会话已失效")
