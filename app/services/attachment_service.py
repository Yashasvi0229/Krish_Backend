"""
Attachment service — download → dedupe → extract.

Called once per attachment from the Gmail-search worker. Ensures:
    1. File bytes saved to disk exactly once per file_hash.
    2. Extracted text saved to disk exactly once per file_hash.
    3. Every referencing email gets its own `attachments` row (so we can
       still count "how many attachments are on this email").

Handles size limits, unsupported types, and extraction errors gracefully
— a bad PDF should never crash the whole job.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.constants import ExtractionStatus
from app.core.logging import get_logger
from app.integrations.gmail_client import GmailClient
from app.repositories import attachment_repo
from app.services.extraction_service import ExtractionResult, extract
from app.utils.file_storage import storage
from app.utils.hashing import file_hash

log = get_logger(__name__)

SNIPPET_MAX_CHARS = 500


async def download_and_process(
    session: AsyncSession,
    *,
    gmail: GmailClient,
    email_id: uuid.UUID,
    gmail_message_id: str,
    attachment_meta: dict[str, Any],
) -> uuid.UUID | None:
    """
    Full lifecycle for one attachment.

    `attachment_meta` is the parsed message part:
        { attachmentId, filename, mimeType, size }

    Returns the new attachments.id, or None if this attachment was
    skipped (too large, unsupported inline, etc.).
    """
    gmail_att_id = attachment_meta.get("attachmentId")
    filename = attachment_meta.get("filename") or "unnamed"
    mime = attachment_meta.get("mimeType") or "application/octet-stream"
    size = int(attachment_meta.get("size") or 0)

    if not gmail_att_id:
        return None   # inline part, no downloadable body

    # ---- Size guard ----------------------------------------------------
    max_bytes = settings.max_attachment_size_mb * 1024 * 1024
    if size and size > max_bytes:
        log.info("attachment_skipped_too_large", filename=filename, size=size)
        return None

    # ---- Download from Gmail ------------------------------------------
    try:
        data = await gmail.get_attachment(gmail_message_id, gmail_att_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("attachment_download_failed",
                    filename=filename, error=str(exc))
        return None

    if not data:
        return None

    # ---- Hash + dedupe check ------------------------------------------
    hash_hex = file_hash(data)
    extension = Path(filename).suffix.lstrip(".").lower() or "bin"

    existing = await attachment_repo.find_by_file_hash(session, hash_hex)

    if existing:
        # Reuse the storage_path and extracted-text from the existing row.
        storage_path = existing.storage_path
        extracted_text_path = existing.extracted_text_path
        extracted_text_snippet = existing.extracted_text_snippet
        page_count = existing.page_count
        ocr_applied = existing.ocr_applied
        extraction_status = existing.extraction_status
        document_type = existing.document_type
        log.info("attachment_content_dedup", filename=filename, hash=hash_hex[:12])
    else:
        # Fresh file — save + extract.
        storage_path = storage.attachment_path(hash_hex, extension)
        storage.write_bytes(storage_path, data)
        result: ExtractionResult = extract(data, extension)
        extracted_text_snippet = (result.text or "")[:SNIPPET_MAX_CHARS]
        page_count = result.page_count
        ocr_applied = result.ocr_applied
        if result.text:
            extracted_text_path = storage.extracted_text_path(hash_hex)
            storage.write_text(extracted_text_path, result.text)
            extraction_status = ExtractionStatus.DONE.value
        else:
            extracted_text_path = None
            extraction_status = (
                ExtractionStatus.FAILED.value if result.error else ExtractionStatus.DONE.value
            )
        document_type = _infer_document_type(filename, mime)

    # ---- Insert one attachment row for this email ---------------------
    row = {
        "email_id": email_id,
        "file_hash": hash_hex,
        "filename": filename[:500],
        "file_size": len(data),
        "mime_type": mime[:100],
        "file_extension": extension[:20],
        "storage_path": storage_path,
        "extracted_text_path": extracted_text_path,
        "extracted_text_snippet": extracted_text_snippet,
        "page_count": page_count,
        "ocr_applied": ocr_applied,
        "extraction_status": extraction_status,
        "document_type": document_type,
        "processed_at": datetime.now(UTC),
    }

    ids = await attachment_repo.bulk_insert(session, [row])
    return ids[0] if ids else None


def _infer_document_type(filename: str, mime: str) -> str | None:
    """Coarse classification purely from filename/mime — real classification
    (e.g. "Xactimate Sketch" vs "RCV Estimate") happens in the AI layer."""
    lower = filename.lower()
    if "xactimate" in lower or "sketch" in lower:
        return "Xactimate"
    if "estimate" in lower or "rcv" in lower or "acv" in lower:
        return "Estimate"
    if "invoice" in lower or "bill" in lower:
        return "Invoice"
    if "report" in lower or "assessment" in lower:
        return "Report"
    if "photo" in lower or lower.endswith((".jpg", ".jpeg", ".png")):
        return "Photo"
    return None
