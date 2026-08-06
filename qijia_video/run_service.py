"""Durable background-run coordination owned by the standalone workbench."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from qijia_video.database import async_session
from qijia_video.db_models import VideoRun, utc_now


logger = logging.getLogger(__name__)
MAX_HOT_TASKS = 200
_tasks: dict[str, dict] = {}
_creation_lock = asyncio.Lock()
_persist_lock = asyncio.Lock()
_recovery_handlers: dict[str, object] = {}
_owner_user_id: ContextVar[int] = ContextVar("video_owner_user_id", default=1)
_owner_username: ContextVar[str] = ContextVar(
    "video_owner_username", default="admin"
)
_PRIVATE_KEYS = {
    "owner_user_id",
    "owner_username",
    "job_kind",
    "job_payload",
    "recoverable",
}


class TaskConflict(RuntimeError):
    pass


class TaskReservationError(RuntimeError):
    pass


def _jsonable(value):
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _record_to_dict(row: VideoRun) -> dict:
    return {
        "task_id": row.task_id,
        "name": row.name or "",
        "status": row.status or "running",
        "progress": row.progress or "",
        "progress_meta": dict(row.progress_meta or {}),
        "owner_user_id": row.owner_user_id,
        "owner_username": row.owner_username or "",
        "job_kind": row.job_kind or "",
        "job_payload": dict(row.job_payload or {}),
        "recoverable": bool(row.recoverable),
        "result": row.result,
        "error": row.error,
        "created_at": _timestamp(row.created_at) or "",
        "finished_at": _timestamp(row.finished_at),
    }


def _trim_hot_tasks() -> None:
    if len(_tasks) <= MAX_HOT_TASKS:
        return
    ordered = sorted(
        _tasks.values(), key=lambda item: item.get("created_at") or ""
    )
    for item in ordered[: len(_tasks) - MAX_HOT_TASKS]:
        _tasks.pop(str(item.get("task_id") or ""), None)


def set_task_context(user: dict | None) -> tuple:
    value = user or {}
    try:
        user_id = int(value.get("id") or 1)
    except (TypeError, ValueError):
        user_id = 1
    return (
        _owner_user_id.set(user_id),
        _owner_username.set(str(value.get("username") or "admin")),
    )


def reset_task_context(tokens: tuple | None) -> None:
    if not tokens:
        return
    _owner_user_id.reset(tokens[0])
    _owner_username.reset(tokens[1])


def public_task(task: dict, *, viewer: dict | None = None) -> dict:
    del viewer
    return {
        key: value
        for key, value in (task or {}).items()
        if key not in _PRIVATE_KEYS
    }


def can_read_task(user: dict, task: dict) -> bool:
    del task
    if (user or {}).get("role") == "admin":
        return True
    return "qijia_video" in ((user or {}).get("permissions") or [])


def _new_snapshot(
    name: str,
    *,
    job_kind: str,
    job_payload: dict | None,
    recoverable: bool,
) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "task_id": f"t_{uuid.uuid4().hex[:12]}",
        "name": str(name or "")[:300],
        "status": "running",
        "progress": "",
        "progress_meta": {},
        "owner_user_id": _owner_user_id.get(),
        "owner_username": _owner_username.get()[:128],
        "job_kind": str(job_kind or "")[:64],
        "job_payload": _jsonable(job_payload or {}),
        "recoverable": bool(recoverable and job_kind),
        "result": None,
        "error": None,
        "created_at": now,
        "finished_at": None,
    }


def _apply_snapshot(row: VideoRun, snapshot: dict) -> None:
    row.name = str(snapshot.get("name") or "")[:300]
    row.status = str(snapshot.get("status") or "running")[:32]
    row.progress = str(snapshot.get("progress") or "")
    row.progress_meta = _jsonable(snapshot.get("progress_meta") or {})
    row.owner_user_id = int(snapshot.get("owner_user_id") or 1)
    row.owner_username = str(snapshot.get("owner_username") or "")[:128]
    row.job_kind = str(snapshot.get("job_kind") or "")[:64]
    row.job_payload = _jsonable(snapshot.get("job_payload") or {})
    row.recoverable = bool(snapshot.get("recoverable"))
    row.result = _jsonable(snapshot.get("result"))
    row.error = str(snapshot.get("error")) if snapshot.get("error") else None
    finished_at = snapshot.get("finished_at")
    row.finished_at = (
        datetime.fromisoformat(str(finished_at)) if finished_at else None
    )
    row.updated_at = utc_now()


async def _persist_snapshot(snapshot: dict) -> None:
    if async_session is None or not snapshot:
        return
    async with _persist_lock:
        async with async_session() as session:
            row = await session.get(VideoRun, snapshot["task_id"])
            incoming_status = str(snapshot.get("status") or "running")
            if (
                row
                and row.status in ("done", "failed")
                and incoming_status in ("running", "recovering")
            ):
                return
            if not row:
                row = VideoRun(
                    task_id=snapshot["task_id"],
                    name=str(snapshot.get("name") or "")[:300],
                    owner_user_id=int(snapshot.get("owner_user_id") or 1),
                )
                session.add(row)
            _apply_snapshot(row, snapshot)
            await session.commit()


def _schedule_snapshot(task_id: str) -> None:
    snapshot = _jsonable(_tasks.get(task_id) or {})
    if not snapshot or async_session is None:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    task = loop.create_task(_persist_snapshot(snapshot))

    def report_failure(done: asyncio.Task) -> None:
        try:
            done.result()
        except Exception:
            logger.exception("Failed to persist video run %s", task_id)

    task.add_done_callback(report_failure)


async def flush_task_async(task_id: str) -> None:
    snapshot = _jsonable(_tasks.get(task_id) or {})
    if snapshot:
        await _persist_snapshot(snapshot)


async def update_task_payload_async(task_id: str, payload: dict) -> None:
    """Bind durable recovery metadata before a newly reserved task starts."""

    task = _tasks.get(task_id)
    if not task:
        task = await get_task_async(task_id, refresh=True)
    if not task or task.get("status") != "running":
        raise RuntimeError("后台任务已不存在或不再运行")
    task["job_payload"] = _jsonable(payload or {})
    _tasks[task_id] = task
    await _persist_snapshot(task)


def update_progress(task_id: str, progress: str | dict) -> None:
    task = _tasks.get(task_id)
    if not task:
        return
    if isinstance(progress, dict):
        payload = dict(progress)
        task["progress"] = str(payload.pop("message", "") or "")
        task["progress_meta"] = _jsonable(payload)
    else:
        task["progress"] = str(progress or "")
        task["progress_meta"] = {}
    _schedule_snapshot(task_id)


def complete_task(task_id: str, result: dict | None = None) -> None:
    task = _tasks.get(task_id)
    if not task:
        return
    task["status"] = "done"
    task["result"] = _jsonable(result) if result is not None else None
    task["error"] = None
    task["finished_at"] = datetime.now(timezone.utc).isoformat()
    _schedule_snapshot(task_id)


def fail_task(
    task_id: str, error: str, result: dict | None = None
) -> None:
    task = _tasks.get(task_id)
    if not task:
        return
    task["status"] = "failed"
    task["error"] = str(error or "后台任务失败")[:4000]
    if result is not None:
        task["result"] = _jsonable(result)
    task["finished_at"] = datetime.now(timezone.utc).isoformat()
    _schedule_snapshot(task_id)


async def get_task_async(task_id: str, *, refresh: bool = False) -> dict | None:
    task = _tasks.get(task_id)
    should_refresh = bool(
        async_session
        and (
            refresh
            or not task
            or task.get("status") in ("running", "recovering")
        )
    )
    if task and not should_refresh:
        return dict(task)
    if async_session is None:
        return dict(task) if task else None
    async with async_session() as session:
        row = await session.get(VideoRun, task_id)
        if not row:
            return dict(task) if task else None
        snapshot = _record_to_dict(row)
        _tasks[task_id] = snapshot
        _trim_hot_tasks()
        return dict(snapshot)


async def create_or_get_running_task_async(
    name: str,
    conflict_names: list[str] | tuple[str, ...] | None = None,
    *,
    shared: bool = False,
    job_kind: str = "",
    job_payload: dict | None = None,
    recoverable: bool = False,
    reject_conflicts: bool = False,
) -> tuple[str, bool]:
    del shared
    names = list(dict.fromkeys([name, *(conflict_names or [])]))
    owner_id = _owner_user_id.get()
    async with _creation_lock:
        for task in _tasks.values():
            if (
                task.get("name") in names
                and task.get("owner_user_id") == owner_id
                and task.get("status") == "running"
            ):
                if reject_conflicts and task.get("name") != name:
                    raise TaskConflict(
                        f"相关任务正在运行：{task.get('name') or '未命名任务'}"
                    )
                return str(task["task_id"]), True

        snapshot = _new_snapshot(
            name,
            job_kind=job_kind,
            job_payload=job_payload,
            recoverable=recoverable,
        )
        if async_session is None:
            _tasks[snapshot["task_id"]] = snapshot
            _trim_hot_tasks()
            return str(snapshot["task_id"]), False

        async with async_session() as session:
            statement = select(VideoRun).where(
                VideoRun.name.in_(names),
                VideoRun.owner_user_id == owner_id,
                VideoRun.status == "running",
            )
            existing = (
                await session.execute(
                    statement.order_by(VideoRun.created_at.desc()).limit(1)
                )
            ).scalars().first()
            if existing:
                task = _record_to_dict(existing)
                _tasks[existing.task_id] = task
                if reject_conflicts and existing.name != name:
                    raise TaskConflict(
                        f"相关任务正在运行：{existing.name or '未命名任务'}"
                    )
                return existing.task_id, True

            row = VideoRun(
                task_id=snapshot["task_id"],
                name=snapshot["name"],
                owner_user_id=owner_id,
            )
            _apply_snapshot(row, snapshot)
            session.add(row)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                existing = (
                    await session.execute(
                        statement.order_by(VideoRun.created_at.desc()).limit(1)
                    )
                ).scalars().first()
                if not existing:
                    raise TaskReservationError(
                        "无法安全创建后台任务，请稍后重试"
                    ) from exc
                task = _record_to_dict(existing)
                _tasks[existing.task_id] = task
                return existing.task_id, True

        _tasks[snapshot["task_id"]] = snapshot
        _trim_hot_tasks()
        return str(snapshot["task_id"]), False


def register_recovery_handler(job_kind: str, handler) -> None:
    if job_kind and handler:
        _recovery_handlers[str(job_kind)] = handler


async def _resume_interrupted_task(
    task_id: str, handler, payload: dict
) -> None:
    try:
        replacement_id = str(await handler(dict(payload or {})) or "")
        if not replacement_id or replacement_id == task_id:
            raise RuntimeError("恢复处理器未创建有效的替代任务")
        update_progress(task_id, "服务已恢复，正在继续原任务…")
        for _ in range(8 * 60 * 60):
            replacement = await get_task_async(replacement_id, refresh=True)
            if replacement and replacement.get("status") == "done":
                result = dict(replacement.get("result") or {})
                result.update({
                    "recovered_after_restart": True,
                    "replacement_task_id": replacement_id,
                })
                complete_task(task_id, result)
                await flush_task_async(task_id)
                return
            if replacement and replacement.get("status") == "failed":
                raise RuntimeError(
                    replacement.get("error") or "恢复后的替代任务失败"
                )
            await asyncio.sleep(1)
        raise RuntimeError("恢复后的任务等待超时")
    except Exception as exc:
        logger.exception("Failed to recover interrupted run %s", task_id)
        fail_task(task_id, f"服务重启后的自动恢复失败：{exc}")
        await flush_task_async(task_id)


async def recover_interrupted_tasks() -> int:
    if async_session is None:
        return 0
    resumable: list[tuple[str, object, dict]] = []
    async with async_session() as session:
        rows = (
            await session.execute(
                select(VideoRun).where(
                    VideoRun.status.in_(("running", "recovering"))
                )
            )
        ).scalars().all()
        for row in rows:
            snapshot = _record_to_dict(row)
            handler = _recovery_handlers.get(snapshot.get("job_kind") or "")
            if snapshot.get("recoverable") and handler:
                row.status = "recovering"
                row.progress = "服务已重启，正在恢复任务…"
                row.progress_meta = {}
                row.error = None
                row.finished_at = None
                row.updated_at = utc_now()
                snapshot = _record_to_dict(row)
                _tasks[row.task_id] = snapshot
                resumable.append((
                    row.task_id,
                    handler,
                    dict(row.job_payload or {}),
                ))
            else:
                row.status = "failed"
                row.error = "服务曾重启，后台任务已中断，请重新运行"
                row.finished_at = utc_now()
                row.updated_at = utc_now()
        await session.commit()

    for task_id, handler, payload in resumable:
        asyncio.create_task(_resume_interrupted_task(task_id, handler, payload))
    if rows:
        logger.warning(
            "Recovered %s interrupted runs; retired %s",
            len(resumable),
            len(rows) - len(resumable),
        )
    return len(rows)
