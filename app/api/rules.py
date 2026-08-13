"""Billing-rules listing.

    GET /api/rules  →  { items: [{id, code, category, description, base_hours, uom, ...}] }

Read-only for Phase 1 — the frontend Review UI needs a category dropdown.
Full admin CRUD (create/edit/delete/versions) can come later; for now the
25 rules seeded via migration 0002 are the canonical list.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.database import get_db
from app.repositories import billing_rule_repo

router = APIRouter()


@router.get("")
async def list_rules(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    """All active global billing rules. Wrapped in `items` for frontend
    consistency with list-style endpoints."""
    rows = await billing_rule_repo.list_active_global(db)
    return {
        "items": [
            {
                "id": str(r.id),
                "code": r.code,
                "category": r.category,
                "description": r.description,
                "charge_type": r.charge_type,
                "base_hours": float(r.base_hours) if r.base_hours is not None else None,
                "uom": r.uom,
                "conditions": r.conditions or {},
                "is_active": r.is_active,
                "version": r.version,
            }
            for r in rows
        ]
    }
