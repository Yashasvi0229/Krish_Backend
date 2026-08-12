"""
Gmail-connection management routes (admin-only).

Routes:
    POST   /api/gmail/connect     — returns the Google consent URL
    GET    /api/gmail/status      — current connection state
    POST   /api/gmail/disconnect  — clears the singleton row

The actual OAuth callback (`/api/auth/google/callback`) lives in
`api/auth.py` because Google's registered redirect URI points there.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.database import get_db
from app.schemas.gmail import GmailAuthUrlResponse, GmailStatusResponse
from app.services import gmail_service

router = APIRouter()


@router.post("/connect", response_model=GmailAuthUrlResponse)
async def connect(
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> GmailAuthUrlResponse:
    """Return the Google consent URL. Frontend redirects the browser to it."""
    url = gmail_service.build_authorization_url(admin_email=admin.email)
    return GmailAuthUrlResponse(auth_url=url)


@router.get("/status", response_model=GmailStatusResponse)
async def status_(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001 — auth only
) -> GmailStatusResponse:
    """Snapshot of the current Gmail connection."""
    return await gmail_service.get_status(db)


@router.post("/disconnect", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def disconnect(
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],  # noqa: ARG001
) -> Response:
    """Remove the singleton connection row. Idempotent — 204 either way."""
    await gmail_service.disconnect(db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
