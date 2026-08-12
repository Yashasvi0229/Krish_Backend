"""
Pydantic schemas for Gmail-connection endpoints.

The connect flow is 3 hops:
    1. Frontend POSTs to /api/gmail/connect            → `GmailAuthUrlResponse`
    2. Browser visits that URL, user grants consent
    3. Google redirects to /api/auth/google/callback   → HTTP 302 back to frontend

The frontend then polls /api/gmail/status → `GmailStatusResponse` to
confirm the connection landed.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, HttpUrl


class GmailAuthUrlResponse(BaseModel):
    """Google consent-screen URL the frontend should send the browser to."""

    auth_url: HttpUrl


class GmailStatusResponse(BaseModel):
    """Snapshot of the current Gmail connection state."""

    connected: bool
    email: str | None = None
    display_name: str | None = None
    scopes: str | None = None
    last_sync_at: datetime | None = None
    connected_by_admin_email: str | None = None
    connected_at: datetime | None = None
