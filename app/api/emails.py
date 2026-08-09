"""
Email + attachment read endpoints.

    GET /api/emails/{id}                 — full detail (with body + attachments)
    GET /api/claims/{claim_id}/emails    — list emails for a claim
    GET /api/attachments/{id}/download   — download raw file
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.core.exceptions import NotFoundError
from app.database import get_db
from app.repositories import attachment_repo, email_repo
from app.schemas.email import AttachmentSummary, EmailDetail, EmailSummary
from app.utils.file_storage import storage

router = APIRouter()


@router.get("/emails/{email_id}", response_model=EmailDetail)
async def get_email(
    email_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> EmailDetail:
    email = await email_repo.get_by_id(db, email_id)
    if email is None:
        raise NotFoundError(f"Email {email_id} not found.")

    # Load body from disk (empty string if not persisted, e.g. metadata-only sync)
    try:
        body_text = storage.read_text(email.body_path) if email.body_path else ""
    except FileNotFoundError:
        body_text = ""

    atts = await attachment_repo.list_by_email(db, email.id)

    return EmailDetail(
        id=email.id,
        gmail_message_id=email.gmail_message_id,
        gmail_link=email.gmail_link,
        subject=email.subject,
        from_email=email.from_email,
        from_name=email.from_name,
        date=email.date,
        body_snippet=email.body_snippet,
        is_internal=email.is_internal,
        attachment_count=len(atts),
        to_emails=list(email.to_emails or []),
        cc_emails=list(email.cc_emails or []),
        body_text=body_text,
        attachments=[AttachmentSummary.model_validate(a) for a in atts],
    )


@router.get("/claims/{claim_id}/emails", response_model=list[EmailSummary])
async def list_claim_emails(
    claim_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
    limit: int = 200,
) -> list[EmailSummary]:
    emails = await email_repo.list_by_claim(db, claim_id, limit=limit)
    out: list[EmailSummary] = []
    for e in emails:
        atts = await attachment_repo.list_by_email(db, e.id)
        out.append(EmailSummary(
            id=e.id,
            gmail_message_id=e.gmail_message_id,
            gmail_link=e.gmail_link,
            subject=e.subject,
            from_email=e.from_email,
            from_name=e.from_name,
            date=e.date,
            body_snippet=e.body_snippet,
            is_internal=e.is_internal,
            attachment_count=len(atts),
        ))
    return out


@router.get("/attachments/{attachment_id}/download")
async def download_attachment(
    attachment_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> FileResponse:
    att = await attachment_repo.get_by_id(db, attachment_id)
    if att is None:
        raise NotFoundError(f"Attachment {attachment_id} not found.")
    abs_path = storage.absolute_path(att.storage_path)
    if not abs_path.exists():
        raise NotFoundError("Attachment file missing on disk (may have been GCed).")
    return FileResponse(
        path=str(abs_path),
        filename=att.filename,
        media_type=att.mime_type,
    )
