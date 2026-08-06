"""无平台依赖的内存仓储，用于契约测试、CLI 演示与迁移验证。"""
from __future__ import annotations

import asyncio
import uuid

from qijia_video.contracts import Actor
from qijia_video.errors import AccessDenied, ResourceNotFound, RevisionConflict


class InMemoryAggregateRepository:
    def __init__(self):
        self._documents: dict[tuple[str, str], dict] = {}
        self._owners: dict[tuple[str, str], int | None] = {}
        self._owner_usernames: dict[tuple[str, str], str] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _copy(value: dict) -> dict:
        import json

        return json.loads(json.dumps(value, ensure_ascii=False))

    def _authorize(self, key: tuple[str, str], actor: Actor) -> None:
        if actor.is_admin:
            return
        if (
            self._owners.get(key) != actor.user_id
            or self._owner_usernames.get(key) != actor.username
        ):
            raise AccessDenied("无权访问该资源")

    async def create(
        self, kind: str, name: str, actor: Actor, document: dict
    ) -> dict:
        del name
        async with self._lock:
            resource_id = f"qv_{uuid.uuid4().hex[:16]}"
            key = (kind, resource_id)
            prepared = self._copy(document)
            prepared["id"] = resource_id
            prepared["revision"] = max(1, int(prepared.get("revision") or 1))
            self._documents[key] = prepared
            self._owners[key] = actor.user_id
            self._owner_usernames[key] = actor.username
            return self._copy(prepared)

    async def get(self, kind: str, resource_id: str, actor: Actor) -> dict:
        key = (kind, resource_id)
        async with self._lock:
            if key not in self._documents:
                raise ResourceNotFound("资源不存在")
            self._authorize(key, actor)
            return self._copy(self._documents[key])

    async def get_visible(
        self, kind: str, resource_id: str, actor: Actor
    ) -> dict:
        del actor
        key = (kind, resource_id)
        async with self._lock:
            if key not in self._documents:
                raise ResourceNotFound("资源不存在")
            return self._copy(self._documents[key])

    async def list(
        self, kind: str, actor: Actor, *, limit: int = 100
    ) -> list[dict]:
        async with self._lock:
            rows = []
            for key, document in reversed(list(self._documents.items())):
                if key[0] != kind:
                    continue
                if not actor.is_admin and (
                    self._owners.get(key) != actor.user_id
                    or self._owner_usernames.get(key) != actor.username
                ):
                    continue
                rows.append(self._copy(document))
                if len(rows) >= limit:
                    break
            return rows

    async def list_visible(
        self, kind: str, actor: Actor, *, limit: int = 100
    ) -> list[dict]:
        del actor
        async with self._lock:
            rows = []
            for key, document in reversed(list(self._documents.items())):
                if key[0] != kind:
                    continue
                rows.append(self._copy(document))
                if len(rows) >= limit:
                    break
            return rows

    async def replace(
        self,
        kind: str,
        resource_id: str,
        actor: Actor,
        document: dict,
        *,
        expected_revision: int,
    ) -> dict:
        key = (kind, resource_id)
        async with self._lock:
            if key not in self._documents:
                raise ResourceNotFound("资源不存在")
            self._authorize(key, actor)
            current = self._documents[key]
            if int(current.get("revision") or 0) != int(expected_revision):
                raise RevisionConflict("内容已更新，请刷新后重试")
            prepared = self._copy(document)
            prepared["id"] = resource_id
            self._documents[key] = prepared
            return self._copy(prepared)
