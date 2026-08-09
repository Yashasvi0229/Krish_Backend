"""
User routes.

Phase 1 only exposes `/me` — the frontend's Axios interceptor calls this
right after login to populate the auth store's `user` field.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.deps import CurrentAdmin, get_current_admin
from app.schemas.auth import MeResponse

router = APIRouter()


@router.get("/me", response_model=MeResponse)
async def me(
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> MeResponse:
    """Return the currently-authenticated admin's identity."""
    return MeResponse(email=admin.email, name=admin.name, role=admin.role)
