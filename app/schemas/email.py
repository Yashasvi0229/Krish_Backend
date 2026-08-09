"""
Pydantic schemas for email + attachment endpoints.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class AttachmentSummary(BaseModel):
    id: uuid.UUID
    filename: str
    mime_type: str
    file_extension: str
    file_size: int
    page_count: int | None = None
    ocr_applied: bool = False
    document_type: str | None = None
    extraction_status: str
    extracted_text_snippet: str | None = None

    model_config = {"from_attributes": True}


class EmailSummary(BaseModel):
    """Row in the email list view."""
    id: uuid.UUID
    gmail_message_id: str
    gmail_link: str
    subject: str
    from_email: str
    from_name: str
    date: datetime
    body_snippet: str
    is_internal: bool
    attachment_count: int = 0

    model_config = {"from_attributes": True}


class EmailDetail(EmailSummary):
    """Full email with attachments — GET /api/emails/{id}."""
    to_emails: list[str]
    cc_emails: list[str]
    body_text: str    # full body (loaded from disk)
    attachments: list[AttachmentSummary]
