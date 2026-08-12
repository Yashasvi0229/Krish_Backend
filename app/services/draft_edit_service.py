"""
Draft-edit service.

Owns the mutation semantics for InvoiceDraft.line_items — every edit
funnels here so that:
    1. Totals stay consistent (subtotal + grand_total recomputed).
    2. `manual_override=true` gets set on the affected line.
    3. An entry is appended to `approval_history` for audit trail.
    4. The status remains DRAFT (edits are only allowed in DRAFT state —
       once submitted for PM review, edits require the reviewer to
       explicitly reopen).

WHY line_items is a JSONB list rather than a proper table:
    * A draft is a working copy — churn is high, no cross-draft queries
      required, and joining the whole invoice hits every table.
    * Historical drafts stay immutable once approved (snapshot goes into
      the Invoice row's `snapshot_data`), so no version-history concerns.
    * The frontend can round-trip the whole list without translation.

Soft delete: removing a line sets `removed: true` rather than dropping it.
That keeps line_numbers stable for the reviewer AND preserves the AI's
original suggestion in the audit trail.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import DraftStatus
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.invoice_draft import InvoiceDraft
from app.repositories import draft_repo
from app.schemas.draft_edit import LineItemAdd, LineItemDelete, LineItemEdit
from app.services import billing_service

log = get_logger(__name__)


# Statuses in which line-item edits are permitted. Once the draft has
# moved into review, edits require an explicit reopen (workflow_service).
_EDITABLE_STATUSES: frozenset[str] = frozenset({
    DraftStatus.DRAFT.value,
    DraftStatus.REJECTED.value,
})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
async def edit_line_item(
    session: AsyncSession,
    draft_id: uuid.UUID,
    line_number: int,
    patch: LineItemEdit,
    *,
    user_id: str | None,
) -> InvoiceDraft:
    """Apply a partial edit to one line. Returns the refreshed draft.

    Raises NotFoundError if the draft/line doesn't exist,
    ConflictError if the draft is not in an editable state.
    """
    draft = await _load_editable(session, draft_id)
    lines, line, idx = _get_line(draft, line_number)

    old_snapshot = _snapshot(line)
    changed_fields: dict[str, Any] = {}

    # Apply supplied fields
    for field_name in (
        "description", "category", "rule_code",
        "quantity", "quantity_unit", "quantity_hours", "rate",
    ):
        new_val = getattr(patch, field_name)
        if new_val is None:
            continue
        if line.get(field_name) != new_val:
            changed_fields[field_name] = {
                "old": line.get(field_name), "new": new_val,
            }
            line[field_name] = new_val

    if not changed_fields:
        # No-op edit — return without touching audit trail.
        return draft

    # Recompute the line's total if hours or rate touched.
    if "quantity_hours" in changed_fields or "rate" in changed_fields:
        hours = float(line.get("quantity_hours") or 0)
        rate = float(line.get("rate") or 0)
        line["total"] = round(hours * rate, 2)

    line["manual_override"] = True
    lines[idx] = line
    draft.line_items = list(lines)   # trigger JSONB write

    _recalculate_totals(draft)
    _append_history(draft, action="edited",
                    from_status=draft.status, to_status=draft.status,
                    user_id=user_id, note=patch.reason,
                    change={"line_number": line_number, "fields": changed_fields})

    await session.flush()
    log.info("draft_line_edited",
             draft_id=str(draft_id), line_number=line_number,
             fields=list(changed_fields.keys()))
    return draft


async def add_line_item(
    session: AsyncSession,
    draft_id: uuid.UUID,
    payload: LineItemAdd,
    *,
    user_id: str | None,
    default_rate: Decimal,
) -> InvoiceDraft:
    """Add a brand-new line at the end of the list.

    Line numbers stay dense (1..n_active); we number the new line at
    max(line_number)+1 so display order stays predictable.
    """
    draft = await _load_editable(session, draft_id)
    lines = list(draft.line_items or [])

    # Validate rule_code exists in the engine (so we get consistent
    # `category` for free — but do NOT run compute_hours; the reviewer
    # supplies quantity_hours directly).
    spec = billing_service.get_rule(payload.rule_code)
    category = payload.category or (spec.category if spec else "Manual")

    rate = float(payload.rate if payload.rate is not None else default_rate)
    hours = float(payload.quantity_hours)
    total = round(hours * rate, 2)

    next_line_no = 1 + max(
        (int(li.get("line_number") or 0) for li in lines), default=0
    )

    new_line = {
        "line_number": next_line_no,
        "description": payload.description,
        "category": category,
        "rule_code": payload.rule_code,
        "quantity": float(payload.quantity),
        "quantity_unit": payload.quantity_unit,
        "quantity_hours": hours,
        "rate": rate,
        "total": total,
        "source_email_id": None,
        "source_attachment_id": None,
        "ai_confidence": "N/A",
        "ai_reasoning": None,
        "hours_reasoning": f"Manually added by reviewer: {hours} hrs × ${rate}",
        "is_flagged": False,
        "flag_reason": None,
        "hit_cap": False,
        "manual_override": True,
        "removed": False,
    }
    lines.append(new_line)
    draft.line_items = lines

    _recalculate_totals(draft)
    _append_history(draft, action="line_added",
                    from_status=draft.status, to_status=draft.status,
                    user_id=user_id, note=payload.reason,
                    change={"line_number": next_line_no,
                            "new_line": new_line})

    await session.flush()
    log.info("draft_line_added",
             draft_id=str(draft_id), line_number=next_line_no,
             total=total)
    return draft


async def delete_line_item(
    session: AsyncSession,
    draft_id: uuid.UUID,
    line_number: int,
    payload: LineItemDelete,
    *,
    user_id: str | None,
) -> InvoiceDraft:
    """Soft-delete a line by setting `removed: true`. Line stays in the
    JSON (for audit); totals ignore it."""
    draft = await _load_editable(session, draft_id)
    lines, line, idx = _get_line(draft, line_number)

    if line.get("removed"):
        raise ConflictError(f"Line {line_number} is already removed.")

    line["removed"] = True
    line["manual_override"] = True
    lines[idx] = line
    draft.line_items = list(lines)

    _recalculate_totals(draft)
    _append_history(draft, action="line_removed",
                    from_status=draft.status, to_status=draft.status,
                    user_id=user_id, note=payload.reason,
                    change={"line_number": line_number,
                            "removed_total": line.get("total")})

    await session.flush()
    log.info("draft_line_removed",
             draft_id=str(draft_id), line_number=line_number)
    return draft


async def restore_line_item(
    session: AsyncSession,
    draft_id: uuid.UUID,
    line_number: int,
    *,
    user_id: str | None,
) -> InvoiceDraft:
    """Undo a soft-delete. Handy for an 'oops' button in the review UI."""
    draft = await _load_editable(session, draft_id)
    lines, line, idx = _get_line(draft, line_number)

    if not line.get("removed"):
        raise ConflictError(f"Line {line_number} isn't removed.")

    line["removed"] = False
    lines[idx] = line
    draft.line_items = list(lines)

    _recalculate_totals(draft)
    _append_history(draft, action="line_restored",
                    from_status=draft.status, to_status=draft.status,
                    user_id=user_id, note=None,
                    change={"line_number": line_number})

    await session.flush()
    log.info("draft_line_restored",
             draft_id=str(draft_id), line_number=line_number)
    return draft


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------
async def _load_editable(session: AsyncSession, draft_id: uuid.UUID) -> InvoiceDraft:
    draft = await draft_repo.get_by_id(session, draft_id)
    if draft is None:
        raise NotFoundError(f"Draft {draft_id} not found.")
    if draft.status not in _EDITABLE_STATUSES:
        raise ConflictError(
            f"Draft is in status '{draft.status}' — line-item edits are only "
            f"allowed in DRAFT or REJECTED. Reopen the draft first."
        )
    return draft


def _get_line(
    draft: InvoiceDraft, line_number: int
) -> tuple[list[dict[str, Any]], dict[str, Any], int]:
    lines = list(draft.line_items or [])
    for i, li in enumerate(lines):
        if int(li.get("line_number") or 0) == line_number:
            return lines, dict(li), i
    raise NotFoundError(f"Line {line_number} not found in draft {draft.id}.")


def _snapshot(line: dict[str, Any]) -> dict[str, Any]:
    """A lightweight snapshot for audit — only the fields likely to change."""
    return {
        k: line.get(k) for k in (
            "description", "category", "rule_code",
            "quantity", "quantity_unit", "quantity_hours", "rate", "total",
        )
    }


def _recalculate_totals(draft: InvoiceDraft) -> None:
    """Sum non-removed line totals into subtotal + grand_total.
    GST/discount preserved (they are set by an admin flow, not per line)."""
    subtotal = Decimal("0")
    for li in (draft.line_items or []):
        if li.get("removed"):
            continue
        subtotal += Decimal(str(li.get("total") or 0))
    draft.subtotal = subtotal
    # GST + discount are Decimal fields with defaults; recompute grand.
    gst_val = Decimal(str(draft.gst_value or 0))
    disc = Decimal(str(draft.discount_amount or 0))
    draft.grand_total = subtotal + gst_val - disc


def _append_history(
    draft: InvoiceDraft, *,
    action: str, from_status: str, to_status: str,
    user_id: str | None, note: str | None,
    change: dict[str, Any] | None = None,
) -> None:
    """Append one entry to the draft's approval_history JSONB list.

    Every mutation on a draft — status change or line-item change —
    lands here. This is our audit trail; a proper table would be nicer
    for cross-draft queries but for MVP-scale review flow, JSONB is fine.
    """
    hist = list(draft.approval_history or [])
    hist.append({
        "at": datetime.now(UTC).isoformat(),
        "action": action,
        "from_status": from_status,
        "to_status": to_status,
        "user_id": str(user_id) if user_id else None,
        "note": note,
        "change": change,
    })
    draft.approval_history = hist
