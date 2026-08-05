"""Independent PostgreSQL records for the video workbench."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Index, Integer, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def json_document_type():
    """Use JSONB on PostgreSQL while keeping metadata locally testable."""

    return JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


class VideoResource(Base):
    """One source card or video-job aggregate with optimistic revisioning."""

    __tablename__ = "video_resources"

    resource_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    name: Mapped[str] = mapped_column(String(300), default="")
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_username: Mapped[str] = mapped_column(String(128), default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    document: Mapped[dict] = mapped_column(json_document_type(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        Index(
            "ix_video_resources_owner_kind_created",
            "owner_user_id",
            "kind",
            "created_at",
        ),
    )


class VideoRun(Base):
    """Durable progress and recovery state for one background action."""

    __tablename__ = "video_runs"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="running")
    progress: Mapped[str] = mapped_column(Text, default="")
    progress_meta: Mapped[dict] = mapped_column(json_document_type(), default=dict)
    owner_user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_username: Mapped[str] = mapped_column(String(128), default="")
    job_kind: Mapped[str] = mapped_column(String(64), default="")
    job_payload: Mapped[dict] = mapped_column(json_document_type(), default=dict)
    recoverable: Mapped[bool] = mapped_column(Boolean, default=False)
    result: Mapped[dict | None] = mapped_column(
        json_document_type(), nullable=True
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )

    __table_args__ = (
        Index("ix_video_runs_owner_created", "owner_user_id", "created_at"),
        Index("ix_video_runs_status", "status"),
        # A recovering predecessor is intentionally excluded so it can spawn
        # one replacement run after a deployment restart.
        Index(
            "ux_video_runs_active_name_owner",
            "name",
            "owner_user_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
    )
