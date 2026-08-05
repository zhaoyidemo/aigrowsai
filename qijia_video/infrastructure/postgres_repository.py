"""Dedicated PostgreSQL aggregate repository."""
from __future__ import annotations

import json
import uuid

from sqlalchemy import select

from qijia_video.contracts import Actor
from qijia_video.database import async_session
from qijia_video.db_models import VideoResource
from qijia_video.errors import AccessDenied, ResourceNotFound, RevisionConflict


class PostgresAggregateRepository:
    @property
    def configured(self) -> bool:
        return async_session is not None

    @staticmethod
    def _copy(value: dict) -> dict:
        return json.loads(json.dumps(value, ensure_ascii=False))

    @staticmethod
    def _owner_id(actor: Actor) -> int:
        return int(actor.user_id or 1)

    @staticmethod
    def _authorize(row: VideoResource, actor: Actor) -> None:
        if not actor.is_admin and row.owner_user_id != int(actor.user_id or 0):
            raise AccessDenied("无权访问该资源")

    @staticmethod
    def _require_session():
        if async_session is None:
            raise RuntimeError("DATABASE_URL 未配置，无法持久化短视频任务")
        return async_session

    async def create(
        self, kind: str, name: str, actor: Actor, document: dict
    ) -> dict:
        session_factory = self._require_session()
        resource_id = f"qv_{uuid.uuid4().hex[:16]}"
        prepared = self._copy(document)
        prepared["id"] = resource_id
        prepared["revision"] = max(1, int(prepared.get("revision") or 1))
        row = VideoResource(
            resource_id=resource_id,
            kind=str(kind or "")[:32],
            name=str(name or "")[:300],
            owner_user_id=self._owner_id(actor),
            owner_username=str(actor.username or "")[:128],
            revision=int(prepared["revision"]),
            document=prepared,
        )
        async with session_factory() as session:
            session.add(row)
            await session.commit()
        return self._copy(prepared)

    async def get(self, kind: str, resource_id: str, actor: Actor) -> dict:
        session_factory = self._require_session()
        async with session_factory() as session:
            row = await session.get(VideoResource, resource_id)
            if not row or row.kind != kind:
                raise ResourceNotFound("资源不存在")
            self._authorize(row, actor)
            return self._copy(row.document)

    async def list(
        self, kind: str, actor: Actor, *, limit: int = 100
    ) -> list[dict]:
        session_factory = self._require_session()
        safe_limit = max(1, min(500, int(limit or 100)))
        statement = select(VideoResource).where(VideoResource.kind == kind)
        if not actor.is_admin:
            statement = statement.where(
                VideoResource.owner_user_id == self._owner_id(actor)
            )
        statement = statement.order_by(VideoResource.created_at.desc()).limit(
            safe_limit
        )
        async with session_factory() as session:
            rows = (await session.execute(statement)).scalars().all()
            return [self._copy(row.document) for row in rows]

    async def replace(
        self,
        kind: str,
        resource_id: str,
        actor: Actor,
        document: dict,
        *,
        expected_revision: int,
    ) -> dict:
        session_factory = self._require_session()
        async with session_factory() as session:
            statement = (
                select(VideoResource)
                .where(VideoResource.resource_id == resource_id)
                .with_for_update()
            )
            row = (await session.execute(statement)).scalars().first()
            if not row or row.kind != kind:
                raise ResourceNotFound("资源不存在")
            self._authorize(row, actor)
            if row.revision != int(expected_revision):
                raise RevisionConflict("内容已更新，请刷新后重试")
            prepared = self._copy(document)
            prepared["id"] = resource_id
            row.revision = int(prepared.get("revision") or row.revision + 1)
            row.document = prepared
            await session.commit()
            return self._copy(prepared)
