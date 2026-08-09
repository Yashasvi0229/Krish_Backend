"""
Debug/stats endpoints.

Read-only endpoints for development-time DB introspection when Render's
Shell isn't available (free tier). Behind admin auth so only the operator
can see counts + samples. Should be removed or gated in true production.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.database import get_db
from app.models.attachment import Attachment
from app.models.claim import Claim
from app.models.client import Client
from app.models.email import Email
from app.models.processing_job import ProcessingJob

router = APIRouter()


@router.get("/stats")
async def db_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict[str, Any]:
    """Return row counts for all major tables — quick health/data check."""
    async def _count(model) -> int:
        return (await db.execute(select(func.count()).select_from(model))).scalar() or 0

    return {
        "clients":         await _count(Client),
        "claims":          await _count(Claim),
        "emails":          await _count(Email),
        "emails_internal": (await db.execute(
            select(func.count()).select_from(Email).where(Email.is_internal.is_(True))
        )).scalar() or 0,
        "attachments":     await _count(Attachment),
        "processing_jobs": await _count(ProcessingJob),
    }


@router.get("/emails/latest")
async def latest_emails(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Latest N emails — subject / from / date / is_internal / attachment count."""
    rows = (await db.execute(
        select(Email).order_by(Email.date.desc()).limit(limit)
    )).scalars().all()

    out: list[dict[str, Any]] = []
    for e in rows:
        n_atts = (await db.execute(
            select(func.count()).select_from(Attachment).where(Attachment.email_id == e.id)
        )).scalar() or 0
        out.append({
            "id": str(e.id),
            "subject": e.subject,
            "from_email": e.from_email,
            "date": e.date.isoformat() if e.date else None,
            "is_internal": e.is_internal,
            "attachments": n_atts,
        })
    return out


@router.get("/attachments/latest")
async def latest_attachments(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Latest N attachments — filename / pages / size / extraction status."""
    rows = (await db.execute(
        select(Attachment).order_by(Attachment.created_at.desc()).limit(limit)
    )).scalars().all()
    return [{
        "id": str(a.id),
        "filename": a.filename,
        "file_extension": a.file_extension,
        "file_size": a.file_size,
        "page_count": a.page_count,
        "extraction_status": a.extraction_status,
        "ocr_applied": a.ocr_applied,
        "document_type": a.document_type,
        "snippet": (a.extracted_text_snippet or "")[:200],
    } for a in rows]


@router.get("/jobs/latest")
async def latest_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Latest N processing jobs — status / progress / stats."""
    rows = (await db.execute(
        select(ProcessingJob).order_by(ProcessingJob.created_at.desc()).limit(limit)
    )).scalars().all()
    return [{
        "id": str(j.id),
        "job_type": j.job_type,
        "status": j.status,
        "progress": float(j.progress),
        "current_step": j.current_step_name,
        "stats": j.stats,
        "input_params": j.input_params,
        "error_message": j.error_message,
        "created_at": j.created_at.isoformat() if j.created_at else None,
        "completed_at": j.completed_at.isoformat() if j.completed_at else None,
    } for j in rows]



@router.get("/claims/latest")
async def latest_claims(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> list[dict]:
    """Return all claims with their IDs (debug helper)."""
    from app.models.claim import Claim
    from sqlalchemy import select
    result = await db.execute(select(Claim).order_by(Claim.created_at.desc()))
    return [
        {
            "id": str(c.id),
            "claim_no": c.claim_no,
            "gnc_file_no": c.gnc_file_no,
            "file_name": c.file_name,
        }
        for c in result.scalars().all()
    ]
