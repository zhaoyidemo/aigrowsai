"""Administrator and persistent colleague authentication."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Depends, HTTPException, Request

from qijia_video import accounts
from qijia_video.errors import ProviderUnavailable
from qijia_video.settings import settings


AUTH_COOKIE = "qijia_video_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
SESSION_VERSION = 2


def configured() -> bool:
    return not settings.standalone_configuration_errors()


def admin_user() -> dict:
    return {
        # Keep the legacy standalone-admin ID stable. Persistent member IDs are
        # generated above 1000 so they cannot collide with existing resources.
        "id": 1,
        "username": settings.ADMIN_USERNAME.strip() or "admin",
        "role": "admin",
        "permissions": ["qijia_video", "manage_accounts"],
        "is_active": True,
        "session_version": 1,
    }


async def authenticate(username: str, password: str) -> dict | None:
    supplied_username = str(username or "").strip()
    supplied_password = str(password or "")
    if len(settings.SESSION_SECRET) < 32:
        return None
    admin_name = settings.ADMIN_USERNAME.strip()
    admin_ready = (
        bool(admin_name)
        and len(settings.ADMIN_PASSWORD) >= 12
        and len(settings.SESSION_SECRET) >= 32
    )
    if admin_ready and secrets.compare_digest(supplied_username, admin_name):
        return (
            admin_user()
            if secrets.compare_digest(supplied_password, settings.ADMIN_PASSWORD)
            else None
        )
    try:
        return await accounts.authenticate_member(
            supplied_username, supplied_password
        )
    except ProviderUnavailable:
        return None


async def verify_credentials(username: str, password: str) -> bool:
    """Compatibility helper for callers that only need a yes/no result."""

    return bool(await authenticate(username, password))


def _sign(payload: bytes) -> str:
    return hmac.new(
        settings.SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()


def create_session_token(user: dict) -> str:
    role = "admin" if user.get("role") == "admin" else "member"
    payload = json.dumps(
        {
            "v": SESSION_VERSION,
            "uid": int(user.get("id") or -1),
            "u": str(user.get("username") or ""),
            "role": role,
            "sv": int(user.get("session_version") or 1),
            "exp": int(time.time()) + SESSION_MAX_AGE_SECONDS,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def _session_payload(token: str) -> dict | None:
    try:
        encoded, signature = str(token or "").split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _sign(payload)):
            return None
        value = json.loads(payload)
        if int(value.get("exp") or 0) < int(time.time()):
            return None
        return value
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


async def verify_session_token(token: str) -> dict | None:
    value = _session_payload(token)
    if not value:
        return None
    # Preserve valid administrator sessions created before colleague accounts
    # were introduced so a deployment does not unexpectedly log the owner out.
    if value.get("v") == 1:
        return (
            admin_user()
            if value.get("u") == settings.ADMIN_USERNAME.strip()
            else None
        )
    if value.get("v") != SESSION_VERSION:
        return None
    if value.get("role") == "admin":
        return (
            admin_user()
            if value.get("u") == settings.ADMIN_USERNAME.strip()
            and int(value.get("uid") or 0) == 1
            else None
        )
    if value.get("role") != "member":
        return None
    try:
        return await accounts.resolve_member_session(
            int(value.get("uid") or 0),
            str(value.get("u") or ""),
            int(value.get("sv") or 0),
        )
    except (ProviderUnavailable, TypeError, ValueError):
        return None


async def user_from_request(request: Request) -> dict | None:
    token = request.cookies.get(AUTH_COOKIE, "")
    return await verify_session_token(token)


async def get_current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if not user:
        user = await user_from_request(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或登录已过期")
    return user


def require_permission(permission: str):
    async def dependency(user: dict = Depends(get_current_user)) -> dict:
        if user.get("role") == "admin" or permission in (
            user.get("permissions") or []
        ):
            return user
        raise HTTPException(status_code=403, detail="无权访问该功能")

    return dependency


async def require_admin(user: dict = Depends(get_current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可以管理账号")
    return user
