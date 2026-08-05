"""Database lifecycle for the standalone service."""
from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qijia_video.db_models import Base
from qijia_video.settings import settings


logger = logging.getLogger(__name__)


def _async_database_url(raw: str) -> str:
    value = str(raw or "").strip()
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+asyncpg://", 1)
    if value.startswith("postgresql://") and "+asyncpg" not in value:
        return value.replace("postgresql://", "postgresql+asyncpg://", 1)
    return value


DATABASE_URL = _async_database_url(settings.DATABASE_URL)
async_engine = (
    create_async_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=5,
        max_overflow=5,
    )
    if DATABASE_URL
    else None
)
async_session = (
    async_sessionmaker(
        async_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    if async_engine
    else None
)


def database_configured() -> bool:
    return async_session is not None


async def init_database() -> None:
    if not async_engine:
        logger.warning("DATABASE_URL 未配置，独立工作台数据库不可用")
        return
    async with async_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("Standalone video database initialized")


async def close_database() -> None:
    if async_engine:
        await async_engine.dispose()
