"""Clients endpoints — read-only in Phase 1.

    GET /api/clients            → list (filter dropdowns + admin page)
    GET /api/clients/{id}       → single client detail

Full CRUD (create/update/delete/template-upload) will be added in a later
phase once the admin UI is wired.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.core.exceptions import NotFoundError
from app.database import get_db
from app.repositories import client_repo

router = APIRouter()


def _serialize(c) -> dict:
    """Convert Client model to API JSON. `rate_config` is a JSONB — we
    surface its `hourly_rate` at top level for frontend convenience."""
    rc = c.rate_config or {}
    return {
        "id": str(c.id),
        "name": c.name,
        "company_legal_name": c.company_legal_name,
        "client_type": c.client_type,
        "primary_contact_name": c.primary_contact_name,
        "email": c.email,
        "phone": c.phone,
        "address_line1": c.address_line1,
        "address_line2": c.address_line2,
        "currency": c.currency,
        "hourly_rate": float(rc.get("hourly_rate") or 0),
        "gst_percent": float(c.gst_percent) if c.gst_percent is not None else 0.0,
        "invoice_prefix": c.invoice_prefix,
        "is_active": c.is_active,
    }


@router.get("")
async def list_clients(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
    status: str | None = None,   # 'active' filters by is_active + not deleted
) -> dict:
    """List clients. Wrapped in `items` for frontend list consistency."""
    rows = await (client_repo.list_active(db) if status == "active"
                  else client_repo.list_all(db))
    return {"items": [_serialize(c) for c in rows]}


@router.get("/{client_id}")
async def get_client(
    client_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    c = await client_repo.get_by_id(db, client_id)
    if c is None:
        raise NotFoundError(f"Client {client_id} not found.")
    return _serialize(c)
