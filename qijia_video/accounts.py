"""Persistent colleague accounts for the standalone workbench."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import unicodedata
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from qijia_video import database
from qijia_video.db_models import WorkbenchUser, utc_now
from qijia_video.errors import (
    ProviderUnavailable,
    QualityGateFailed,
    ResourceNotFound,
    RevisionConflict,
)
from qijia_video.settings import settings


WORKBENCH_PERMISSION = "qijia_video"
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128
USERNAME_MIN_LENGTH = 2
USERNAME_MAX_LENGTH = 64
_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024


def normalize_username(value: str) -> str:
    username = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not USERNAME_MIN_LENGTH <= len(username) <= USERNAME_MAX_LENGTH:
        raise QualityGateFailed("账号名需为 2-64 个字符")
    if any(
        character.isspace()
        or unicodedata.category(character).startswith("C")
        or not (character.isalnum() or character in "._-")
        for character in username
    ):
        raise QualityGateFailed("账号名只能包含中文、字母、数字、点、短横线或下划线")
    if not any(character.isalnum() for character in username):
        raise QualityGateFailed("账号名至少需要包含一个中文、字母或数字")
    return username


def username_key(value: str) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()


def validate_password(password: str) -> str:
    value = str(password or "")
    if not PASSWORD_MIN_LENGTH <= len(value) <= PASSWORD_MAX_LENGTH:
        raise QualityGateFailed("密码需为 12-128 个字符")
    return value


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _derive_password(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
        maxmem=_SCRYPT_MAXMEM,
    )


def hash_password(password: str) -> str:
    value = validate_password(password)
    salt = secrets.token_bytes(16)
    digest = _derive_password(value, salt)
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}"
        f"${_encode(salt)}${_encode(digest)}"
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, raw_n, raw_r, raw_p, raw_salt, raw_digest = str(
            encoded or ""
        ).split("$", 5)
        if algorithm != "scrypt":
            return False
        n_value, r_value, p_value = int(raw_n), int(raw_r), int(raw_p)
        if (n_value, r_value, p_value) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        expected = _decode(raw_digest)
        actual = hashlib.scrypt(
            str(password or "").encode("utf-8"),
            salt=_decode(raw_salt),
            n=n_value,
            r=r_value,
            p=p_value,
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
        return secrets.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


_DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA"
    "$jRnqZEqfEZVf6GBMi21yHITvOoYTOHhXM72X1JxHkVY"
)


def _session_factory():
    if database.async_session is None:
        raise ProviderUnavailable("DATABASE_URL 未配置，无法管理同事账号")
    return database.async_session


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def member_user(row: WorkbenchUser) -> dict:
    return {
        "id": row.user_id,
        "username": row.username,
        "role": "member",
        "permissions": list(row.permissions or []),
        "is_active": bool(row.is_active),
        "session_version": int(row.session_version or 1),
    }


def public_account(row: WorkbenchUser) -> dict:
    permissions = list(row.permissions or [])
    return {
        "id": row.user_id,
        "username": row.username,
        "role": "member",
        "is_active": bool(row.is_active),
        "can_use_workbench": WORKBENCH_PERMISSION in permissions,
        "revision": int(row.revision or 1),
        "created_by": row.created_by or "",
        "updated_by": row.updated_by or "",
        "last_login_at": _timestamp(row.last_login_at),
        "created_at": _timestamp(row.created_at),
        "updated_at": _timestamp(row.updated_at),
    }


async def authenticate_member(username: str, password: str) -> dict | None:
    key = username_key(username)
    if not key:
        return None
    session_factory = _session_factory()
    async with session_factory() as session:
        row = (
            await session.execute(
                select(WorkbenchUser).where(WorkbenchUser.username_key == key)
            )
        ).scalars().first()
        encoded = row.password_hash if row else _DUMMY_PASSWORD_HASH
        verified = await asyncio.to_thread(verify_password, password, encoded)
        if not (
            row
            and verified
            and row.is_active
            and WORKBENCH_PERMISSION in (row.permissions or [])
        ):
            return None
        row.last_login_at = utc_now()
        await session.commit()
        return member_user(row)


async def resolve_member_session(
    user_id: int,
    username: str,
    session_version: int,
) -> dict | None:
    session_factory = _session_factory()
    async with session_factory() as session:
        row = await session.get(WorkbenchUser, int(user_id))
        if not (
            row
            and row.is_active
            and row.username_key == username_key(username)
            and int(row.session_version or 0) == int(session_version)
            and WORKBENCH_PERMISSION in (row.permissions or [])
        ):
            return None
        return member_user(row)


async def list_members() -> list[dict]:
    session_factory = _session_factory()
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(WorkbenchUser).order_by(WorkbenchUser.created_at.asc())
            )
        ).scalars().all()
        return [public_account(row) for row in rows]


async def create_member(
    *,
    username: str,
    password: str,
    is_active: bool,
    can_use_workbench: bool,
    actor_username: str,
) -> dict:
    normalized = normalize_username(username)
    key = username_key(normalized)
    if key == username_key(settings.ADMIN_USERNAME):
        raise RevisionConflict("该账号名已由管理员使用")
    password_hash = await asyncio.to_thread(hash_password, password)
    session_factory = _session_factory()
    async with session_factory() as session:
        existing = (
            await session.execute(
                select(WorkbenchUser.user_id).where(
                    WorkbenchUser.username_key == key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise RevisionConflict("同事账号已存在")
        for _ in range(3):
            row = WorkbenchUser(
                user_id=1000 + secrets.randbelow(2_000_000_000),
                username=normalized,
                username_key=key,
                password_hash=password_hash,
                permissions=(
                    [WORKBENCH_PERMISSION] if can_use_workbench else []
                ),
                is_active=bool(is_active),
                session_version=1,
                revision=1,
                created_by=str(actor_username or "")[:128],
                updated_by=str(actor_username or "")[:128],
            )
            session.add(row)
            try:
                await session.commit()
                return public_account(row)
            except IntegrityError as exc:
                await session.rollback()
                duplicate = (
                    await session.execute(
                        select(WorkbenchUser.user_id).where(
                            WorkbenchUser.username_key == key
                        )
                    )
                ).scalar_one_or_none()
                if duplicate is not None:
                    raise RevisionConflict("同事账号已存在") from exc
        raise RevisionConflict("无法分配安全的账号标识，请重试")


async def update_member(
    user_id: int,
    *,
    expected_revision: int,
    is_active: bool,
    can_use_workbench: bool,
    actor_username: str,
) -> dict:
    session_factory = _session_factory()
    async with session_factory() as session:
        row = (
            await session.execute(
                select(WorkbenchUser)
                .where(WorkbenchUser.user_id == int(user_id))
                .with_for_update()
            )
        ).scalars().first()
        if not row:
            raise ResourceNotFound("同事账号不存在")
        if int(row.revision or 0) != int(expected_revision):
            raise RevisionConflict("账号已在其他页面更新，请刷新后重试")
        permissions = [WORKBENCH_PERMISSION] if can_use_workbench else []
        access_changed = (
            bool(row.is_active) != bool(is_active)
            or list(row.permissions or []) != permissions
        )
        row.is_active = bool(is_active)
        row.permissions = permissions
        if access_changed:
            row.session_version = int(row.session_version or 0) + 1
        row.revision = int(row.revision or 0) + 1
        row.updated_by = str(actor_username or "")[:128]
        row.updated_at = utc_now()
        await session.commit()
        return public_account(row)


async def reset_member_password(
    user_id: int,
    *,
    expected_revision: int,
    new_password: str,
    actor_username: str,
) -> dict:
    password_hash = await asyncio.to_thread(hash_password, new_password)
    session_factory = _session_factory()
    async with session_factory() as session:
        row = (
            await session.execute(
                select(WorkbenchUser)
                .where(WorkbenchUser.user_id == int(user_id))
                .with_for_update()
            )
        ).scalars().first()
        if not row:
            raise ResourceNotFound("同事账号不存在")
        if int(row.revision or 0) != int(expected_revision):
            raise RevisionConflict("账号已在其他页面更新，请刷新后重试")
        row.password_hash = password_hash
        row.session_version = int(row.session_version or 0) + 1
        row.revision = int(row.revision or 0) + 1
        row.updated_by = str(actor_username or "")[:128]
        row.updated_at = utc_now()
        await session.commit()
        return public_account(row)
