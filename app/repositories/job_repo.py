"""
Repository for `processing_jobs`.

Progress updates go through here; the actual publishing to Redis pub/sub
happens in `job_service.py` (this file stays pure data access).
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import JobStatus, JobType
from app.models.processing_job import ProcessingJob


async def get_by_id(session: AsyncSession, job_id: uuid.UUID) -> ProcessingJob | None:
    return await session.get(ProcessingJob, job_id)


async def create(
    session: AsyncSession,
    *,
    job_type: JobType,
    input_params: dict[str, Any],
    user_id: uuid.UUID | None = None,
    steps: list[dict[str, Any]] | None = None,
) -> ProcessingJob:
    job = ProcessingJob(
        user_id=user_id,
        job_type=job_type.value,
        status=JobStatus.PENDING.value,
        input_params=input_params,
        steps=steps or [],
        stats={},
        progress=Decimal("0"),
    )
    session.add(job)
    await session.flush()
    return job


async def mark_started(
    session: AsyncSession, job_id: uuid.UUID, celery_task_id: str | None = None
) -> None:
    job = await get_by_id(session, job_id)
    if not job:
        return
    job.status = JobStatus.PROCESSING.value
    job.started_at = datetime.now(UTC)
    if celery_task_id:
        job.celery_task_id = celery_task_id


async def update_progress(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    progress: float,
    current_step_index: int,
    current_step_name: str,
    stats_delta: dict[str, Any] | None = None,
) -> None:
    """Idempotent progress bump. Any provided fields overwrite; `stats_delta`
    is merged into the existing `stats` JSONB blob."""
    job = await get_by_id(session, job_id)
    if not job:
        return
    job.progress = Decimal(str(round(min(max(progress, 0.0), 100.0), 2)))
    job.current_step_index = current_step_index
    job.current_step_name = current_step_name
    if stats_delta:
        merged = dict(job.stats or {})
        merged.update(stats_delta)
        job.stats = merged


async def mark_completed(
    session: AsyncSession,
    job_id: uuid.UUID,
    result_data: dict[str, Any] | None = None,
) -> None:
    job = await get_by_id(session, job_id)
    if not job:
        return
    job.status = JobStatus.COMPLETED.value
    job.progress = Decimal("100")
    job.completed_at = datetime.now(UTC)
    if result_data:
        job.result_data = result_data


async def mark_failed(
    session: AsyncSession, job_id: uuid.UUID, error_message: str
) -> None:
    job = await get_by_id(session, job_id)
    if not job:
        return
    job.status = JobStatus.FAILED.value
    job.error_message = error_message[:5000]   # cap message size
    job.completed_at = datetime.now(UTC)


async def list_recent_by_user(
    session: AsyncSession, user_id: uuid.UUID | None, limit: int = 20
) -> list[ProcessingJob]:
    stmt = select(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(limit)
    if user_id is not None:
        stmt = stmt.where(ProcessingJob.user_id == user_id)
    result = await session.execute(stmt)
    return list(result.scalars().all())
