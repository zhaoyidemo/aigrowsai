"""Single-administrator authentication for the standalone MVP."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time

from fastapi import Depends, HTTPException, Request

from qijia_video.settings import settings


AUTH_COOKIE = "qijia_video_session"
SESSION_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def configured() -> bool:
    return not settings.standalone_configuration_errors()


def admin_user() -> dict:
    return {
        "id": 1,
        "username": settings.ADMIN_USERNAME.strip() or "admin",
        "role": "admin",
        "permissions": ["qijia_video"],
        "is_active": True,
    }


def verify_credentials(username: str, password: str) -> bool:
    if len(settings.ADMIN_PASSWORD) < 12 or len(settings.SESSION_SECRET) < 32:
        return False
    return secrets.compare_digest(
        str(username or "").strip(), settings.ADMIN_USERNAME.strip()
    ) and secrets.compare_digest(str(password or ""), settings.ADMIN_PASSWORD)


def _sign(payload: bytes) -> str:
    return hmac.new(
        settings.SESSION_SECRET.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()


def create_session_token() -> str:
    payload = json.dumps(
        {
            "v": 1,
            "u": settings.ADMIN_USERNAME.strip(),
            "exp": int(time.time()) + SESSION_MAX_AGE_SECONDS,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(payload)}"


def verify_session_token(token: str) -> bool:
    try:
        encoded, signature = str(token or "").split(".", 1)
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        if not hmac.compare_digest(signature, _sign(payload)):
            return False
        value = json.loads(payload)
        return (
            value.get("v") == 1
            and value.get("u") == settings.ADMIN_USERNAME.strip()
            and int(value.get("exp") or 0) >= int(time.time())
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def user_from_request(request: Request) -> dict | None:
    token = request.cookies.get(AUTH_COOKIE, "")
    return admin_user() if verify_session_token(token) else None


async def get_current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None) or user_from_request(request)
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
