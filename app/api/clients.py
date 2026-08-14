"""Clients endpoints — full CRUD.

    GET    /api/clients            → list
    GET    /api/clients/{id}       → single client
    POST   /api/clients            → create
    PATCH  /api/clients/{id}       → partial update
    DELETE /api/clients/{id}       → soft-delete (keeps historical invoices intact)

Soft-delete is important: even after a client is "deleted", any invoices
that referenced them still need to render correctly. Hard-delete would
break FK integrity or require cascade rules we don't want.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.core.exceptions import NotFoundError
from app.database import get_db
from app.repositories import client_repo
from app.schemas.client_crud import ClientCreate, ClientUpdate

router = APIRouter()


def _serialize(c) -> dict:
    """Client model → API JSON. `rate_config.hourly_rate` surfaces at top level."""
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
    """List clients wrapped in `items` for frontend consistency."""
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


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_client(
    payload: ClientCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    """Create a new client. Returns the persisted row (with server-assigned id)."""
    data = payload.model_dump()
    client = await client_repo.create(db, **data)
    await db.commit()
    return _serialize(client)


@router.patch("/{client_id}")
async def update_client(
    client_id: uuid.UUID,
    payload: ClientUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    """Partial update — only supplied fields are applied."""
    c = await client_repo.get_by_id(db, client_id)
    if c is None:
        raise NotFoundError(f"Client {client_id} not found.")
    # exclude_unset=True means keys the caller didn't send are ignored,
    # so PATCH stays truly partial.
    patch = payload.model_dump(exclude_unset=True)
    updated = await client_repo.update(db, c, **patch)
    await db.commit()
    return _serialize(updated)


@router.delete("/{client_id}", status_code=status.HTTP_204_NO_CONTENT,
               response_class=Response)
async def delete_client(
    client_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> Response:
    """Soft-delete. Historical invoices remain readable."""
    c = await client_repo.get_by_id(db, client_id)
    if c is None:
        raise NotFoundError(f"Client {client_id} not found.")
    await client_repo.soft_delete(db, c)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
