"""
Analysis endpoints.

    POST /api/claims/{claim_id}/analyze  → queue AI analysis + draft creation
    GET  /api/claims/{claim_id}/analyses → view AI results (for review UI)
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.config import settings
from app.core.constants import JobType
from app.core.exceptions import NotFoundError
from app.core.logging import get_logger
from app.database import get_db
from app.models.ai_analysis import AIAnalysis
from app.repositories import claim_repo, job_repo
from app.schemas.invoice import AnalyzeClaimRequest
from app.schemas.job import JobCreatedResponse
from app.services import job_service

log = get_logger(__name__)
router = APIRouter()


ANALYZE_STEPS = [
    {"index": 0, "name": "Loading claim data"},
    {"index": 1, "name": "Analyzing emails"},
    {"index": 2, "name": "Analyzing attachments"},
    {"index": 3, "name": "Applying billing rules"},
    {"index": 4, "name": "Creating invoice draft"},
]


@router.post(
    "/{claim_id}/analyze",
    response_model=JobCreatedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def analyze_claim(
    claim_id: uuid.UUID,
    payload: AnalyzeClaimRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> JobCreatedResponse:
    """Queue an AI analysis of every email + attachment on a claim.
    Ends with an InvoiceDraft the reviewer can approve."""
    claim = await claim_repo.get_by_id(db, claim_id)
    if claim is None:
        raise NotFoundError(f"Claim {claim_id} not found.")

    job = await job_repo.create(
        db,
        job_type=JobType.CLAIM_ANALYSIS,
        input_params={
            "claim_id": str(claim_id),
            "force_refresh": payload.force_refresh,
        },
        steps=ANALYZE_STEPS,
        user_id=None,
    )
    await db.commit()
    job_id = job.id

    # Same eager/queue split as gmail-search (see app/api/jobs.py comments)
    if settings.celery_task_always_eager:
        from app.workers.ai_tasks import _analyze_claim_async
        asyncio.create_task(_analyze_claim_async(str(job_id), None))
        log.info("analyze_claim_job_started_eager", job_id=str(job_id))
    else:
        from app.workers.ai_tasks import analyze_claim as celery_task
        r = celery_task.delay(str(job_id))
        log.info("analyze_claim_job_queued", job_id=str(job_id), celery_task=r.id)

    return JobCreatedResponse(
        job_id=job_id,
        status=job.status,
        steps=job.steps,
        websocket_url=f"/api/jobs/{job_id}/ws",
    )


@router.get("/{claim_id}/analyses")
async def list_claim_analyses(
    claim_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> list[dict]:
    """Every AI analysis (email + attachment) associated with a claim.
    Used by the frontend review UI."""
    claim = await claim_repo.get_by_id(db, claim_id)
    if claim is None:
        raise NotFoundError(f"Claim {claim_id} not found.")

    from app.models.attachment import Attachment
    from app.models.email import Email

    # Emails for this claim, then their analyses
    email_ids = [
        r[0] for r in (await db.execute(
            select(Email.id).where(Email.claim_id == claim_id)
        )).fetchall()
    ]
    attachment_ids = [
        r[0] for r in (await db.execute(
            select(Attachment.id).where(Attachment.email_id.in_(email_ids))
        )).fetchall()
    ] if email_ids else []

    result = await db.execute(
        select(AIAnalysis).where(
            (AIAnalysis.email_id.in_(email_ids))
            | (AIAnalysis.attachment_id.in_(attachment_ids))
        ).order_by(AIAnalysis.created_at.desc())
    )
    analyses = result.scalars().all()

    return [{
        "id": str(a.id),
        "target_type": "email" if a.email_id else "attachment",
        "target_id": str(a.email_id or a.attachment_id),
        "is_billable": a.is_billable,
        "category": a.category,
        "rule_code": a.rule_code,
        "confidence": a.confidence,
        "summary": a.summary,
        "invoice_description": a.invoice_description,
        "reasoning": a.reasoning,
        "should_flag": a.should_flag,
        "flag_reason": a.flag_reason,
        "cost_usd": float(a.cost_usd or 0),
        "created_at": a.created_at.isoformat() if a.created_at else None,
    } for a in analyses]
