"""
ProcessingJob model — tracks background job state for the Gmail-fetch-and-
analyze pipeline.

Lifecycle:
    PENDING → PROCESSING → (COMPLETED | FAILED | CANCELLED)

Progress reporting: `progress` (0-100), `current_step_name`, `steps` (JSONB
array of step definitions), and `stats` (JSONB counters like emails_fetched,
attachments_processed) power the WebSocket updates the frontend renders on
the "Processing" loader screen (see React `ProcessingLoader.jsx`).
"""
from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.constants import JobStatus, JobType, enum_values
from app.models.base import Base, TimestampMixin, UUIDPKMixin

if TYPE_CHECKING:
    from app.models.invoice_draft import InvoiceDraft
    from app.models.user import User


class ProcessingJob(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "processing_jobs"

    # ---- Who / what ----
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    job_type: Mapped[str] = mapped_column(
        String(50), nullable=False, default=JobType.INVOICE_SEARCH.value
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=JobStatus.PENDING.value
    )

    # ---- Input ----
    input_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict
    )

    # ---- Celery link ----
    celery_task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # ---- Progress ----
    progress: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), nullable=False, default=Decimal("0")
    )
    current_step_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_step_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default=""
    )
    steps: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list
    )
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    # ---- Result / error ----
    result_data: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    warning_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    estimated_remaining_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # ---- Timing ----
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ---- Relationships ----
    user: Mapped["User | None"] = relationship(back_populates="processing_jobs")
    drafts: Mapped[list["InvoiceDraft"]] = relationship(back_populates="job")

    __table_args__ = (
        CheckConstraint(
            f"status IN ({', '.join(repr(v) for v in enum_values(JobStatus))})",
            name="status_valid",
        ),
        CheckConstraint(
            f"job_type IN ({', '.join(repr(v) for v in enum_values(JobType))})",
            name="job_type_valid",
        ),
        CheckConstraint("progress >= 0 AND progress <= 100", name="progress_range"),
        Index("ix_processing_jobs_user_id", "user_id"),
        Index("ix_processing_jobs_status", "status"),
        Index("ix_processing_jobs_celery_task_id", "celery_task_id"),
    )

    def __repr__(self) -> str:
        return f"<ProcessingJob {self.id} {self.status} {self.progress}%>"
