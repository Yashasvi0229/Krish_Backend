"""
Job endpoints.

Routes:
    POST /api/jobs/gmail-search   — kick off a search+fetch job
    GET  /api/jobs/{job_id}       — get current state (polling fallback)
    WS   /api/jobs/{job_id}/ws    — live progress (see websockets.py)
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.config import settings
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.database import get_db
from app.repositories import job_repo
from app.schemas.job import (
    GmailSearchRequest,
    JobCreatedResponse,
    JobDetailResponse,
)
from app.services import job_service

log = get_logger(__name__)
router = APIRouter()


@router.post(
    "/gmail-search",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_gmail_search(
    payload: GmailSearchRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> JobCreatedResponse:
    """Queue a background job to search Gmail + fetch matching emails."""
    if not any((payload.claim_no, payload.file_name, payload.gnc_file_no)):
        from app.core.exceptions import BadRequestError
        raise BadRequestError(
            "At least one of claim_no / file_name / gnc_file_no must be provided."
        )

    job_id = await job_service.create_gmail_search_job(
        db,
        claim_no=payload.claim_no,
        file_name=payload.file_name,
        gnc_file_no=payload.gnc_file_no,
        client_id=payload.client_id,
    )

    # Dispatch — three modes, mutually exclusive:
    # 1. Real Celery worker running → `.delay()` queues to Redis, worker picks up
    # 2. `CELERY_TASK_ALWAYS_EAGER=true` → `.delay()` tries to run inline via
    #    `asyncio.run()` inside the task, which BREAKS because FastAPI already
    #    has an event loop running. So we detect eager mode and spawn the
    #    async implementation as a background task instead. Behaviourally
    #    identical from the client's POV (they still get 202 immediately).
    if settings.celery_task_always_eager:
        # Import here to avoid pulling worker code at module load if unused.
        import asyncio
        from app.workers.email_tasks import _process_gmail_search_async
        asyncio.create_task(_process_gmail_search_async(str(job_id), None))
        log.info("gmail_search_job_started_eager", job_id=str(job_id))
    else:
        from app.workers.email_tasks import process_gmail_search
        async_result = process_gmail_search.delay(str(job_id))
        log.info("gmail_search_job_queued",
                 job_id=str(job_id), celery_task=async_result.id)

    job = await job_repo.get_by_id(db, job_id)
    assert job is not None
    return JobCreatedResponse(
        job_id=job_id,
        status=job.status,
        steps=job.steps,
        websocket_url=f"/api/jobs/{job_id}/ws",
    )


@router.get("/{job_id}", response_model=JobDetailResponse)
async def get_job(
    job_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> JobDetailResponse:
    """Poll the current state of a job. Frontend also gets live updates via WS."""
    job = await job_repo.get_by_id(db, job_id)
    if job is None:
        raise NotFoundError(f"Job {job_id} not found.")
    return JobDetailResponse(
        id=job.id,
        job_type=job.job_type,
        status=job.status,
        progress=float(job.progress),
        current_step_index=job.current_step_index,
        current_step_name=job.current_step_name,
        steps=job.steps,
        stats=job.stats,
        result_data=job.result_data,
        error_message=job.error_message,
        started_at=job.started_at,
        completed_at=job.completed_at,
        created_at=job.created_at,
    )
