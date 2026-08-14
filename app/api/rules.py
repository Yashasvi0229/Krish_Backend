"""Billing Rules — full CRUD.

    GET    /api/rules            → list (for review dropdowns + admin page)
    POST   /api/rules            → create
    PATCH  /api/rules/{id}       → update (bumps version)
    DELETE /api/rules/{id}       → delete

Rules define how billing hours are calculated. Any change here directly
affects new drafts — old drafts have already crystallized the value in
their line_items JSONB, so past invoices are safe from rule edits.
"""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.core.exceptions import ConflictError, NotFoundError
from app.database import get_db
from app.repositories import billing_rule_repo
from app.schemas.rule_crud import RuleCreate, RuleUpdate

router = APIRouter()


def _serialize(r) -> dict:
    return {
        "id": str(r.id),
        "code": r.code,
        "category": r.category,
        "description": r.description,
        "charge_type": r.charge_type,
        "base_hours": float(r.base_hours) if r.base_hours is not None else None,
        "flat_fee": float(r.flat_fee) if r.flat_fee is not None else None,
        "uom": r.uom,
        "conditions": r.conditions or {},
        "is_active": r.is_active,
        "version": r.version,
    }


@router.get("")
async def list_rules(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    rows = await billing_rule_repo.list_active_global(db)
    return {"items": [_serialize(r) for r in rows]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rule(
    payload: RuleCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    # Rule codes are globally unique; reject early with a clear message.
    existing = await billing_rule_repo.get_by_code(db, payload.code)
    if existing is not None:
        raise ConflictError(f"Rule code '{payload.code}' already exists.")
    rule = await billing_rule_repo.create(db, **payload.model_dump())
    await db.commit()
    return _serialize(rule)


@router.patch("/{rule_id}")
async def update_rule(
    rule_id: uuid.UUID,
    payload: RuleUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> dict:
    rule = await billing_rule_repo.get_by_id(db, rule_id)
    if rule is None:
        raise NotFoundError(f"Rule {rule_id} not found.")
    patch = payload.model_dump(exclude_unset=True)
    if "code" in patch and patch["code"] != rule.code:
        # Prevent silent code collisions on rename.
        conflict = await billing_rule_repo.get_by_code(db, patch["code"])
        if conflict is not None:
            raise ConflictError(f"Rule code '{patch['code']}' already exists.")
    updated = await billing_rule_repo.update(db, rule, **patch)
    await db.commit()
    return _serialize(updated)


@router.delete("/{rule_id}", status_code=status.HTTP_204_NO_CONTENT,
               response_class=Response)
async def delete_rule(
    rule_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> Response:
    rule = await billing_rule_repo.get_by_id(db, rule_id)
    if rule is None:
        raise NotFoundError(f"Rule {rule_id} not found.")
    await billing_rule_repo.delete(db, rule)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
