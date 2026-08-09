"""
Auth routes.

Routes:
    POST /api/auth/login            — issue a JWT
    POST /api/auth/logout           — no-op for stateless JWTs; kept for symmetry
    GET  /api/auth/google/callback  — OAuth callback from Google (Gmail connect)

The Google callback lives HERE (not in gmail.py) because Google's
redirect URI is registered as `.../api/auth/google/callback`. Keeping it
in `auth.py` matches the URL structure and lets us later share the same
callback with any future "Sign in with Google" user login flow.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import AppException
from app.core.logging import get_logger
from app.database import get_db
from app.schemas.auth import LoginRequest, LoginResponse
from app.services import auth_service, gmail_service

log = get_logger(__name__)
router = APIRouter()


@router.post("/login", response_model=LoginResponse)
async def login(payload: LoginRequest) -> LoginResponse:
    """Exchange admin email + password for a JWT session token."""
    return auth_service.login(email=str(payload.email), password=payload.password)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout() -> Response:
    """Stateless JWTs — the frontend just deletes the token.

    This endpoint exists so the frontend's Axios interceptor has something
    to POST to on logout; when we add token revocation (Redis blocklist),
    we'll deny the token here.

    FastAPI's 204 status enforces no response body, so we return the
    `Response` class explicitly rather than `None` (which FastAPI would
    otherwise try to serialize).
    """
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Google OAuth callback --------------------------------------------------
@router.get("/google/callback", response_model=None, include_in_schema=True)
async def google_callback(
    db: Annotated[AsyncSession, Depends(get_db)],
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
    error_description: Annotated[str | None, Query()] = None,
) -> RedirectResponse | HTMLResponse:
    """OAuth 2.0 authorization-code callback.

    Google redirects here with either:
        * `?code=...&state=...`      — user consented, we exchange the code
        * `?error=...&error_description=...` — user denied or Google errored

    On success/failure we redirect the browser back to the frontend's
    settings page with a query param telling it what happened. If
    FRONTEND_URL isn't a real URL we render a small HTML page instead.
    """
    # ---- Case 1: user denied consent (or Google gave up) ----
    if error:
        log.warning("gmail_oauth_error", error=error, description=error_description)
        return _frontend_redirect(
            connected=False,
            reason=error_description or error,
        )

    # ---- Case 2: normal callback — exchange & store ----
    try:
        await gmail_service.exchange_code_for_tokens(
            code=code or "", state=state or "", session=db,
        )
    except AppException as exc:
        # Never expose internal error codes — pass a safe reason string.
        log.warning("gmail_oauth_exchange_failed", code=exc.code, msg=exc.message)
        return _frontend_redirect(connected=False, reason=exc.message)

    return _frontend_redirect(connected=True, reason=None)


# ---- Helpers ----------------------------------------------------------------
def _frontend_redirect(*, connected: bool, reason: str | None) -> RedirectResponse | HTMLResponse:
    """Redirect the browser back to the frontend (or show HTML if no frontend URL)."""
    front = (settings.frontend_url or "").strip()

    # If FRONTEND_URL isn't a real URL (e.g. "*" from initial deploy or blank),
    # render a self-contained HTML success/failure page so the operator at
    # least sees a result instead of a "This site can't be reached".
    if not front.startswith(("http://", "https://")):
        return _fallback_html(connected=connected, reason=reason)

    target = f"{front.rstrip('/')}/settings"
    params = "?gmail=connected" if connected else f"?gmail=error&reason={reason or 'unknown'}"
    return RedirectResponse(url=target + params, status_code=status.HTTP_302_FOUND)


def _fallback_html(*, connected: bool, reason: str | None) -> HTMLResponse:
    """Static HTML page shown when FRONTEND_URL isn't configured."""
    if connected:
        title, body = "Gmail connected ✅", (
            "You can close this tab and return to the app."
        )
    else:
        title, body = "Gmail connection failed ❌", (
            f"Reason: {reason or 'unknown'}. You can close this tab and try again."
        )
    html = f"""<!doctype html>
<html><head><meta charset='utf-8'><title>{title}</title>
<style>body{{font-family:system-ui,sans-serif;padding:2rem;max-width:640px;
margin:auto;color:#111}}h1{{margin-bottom:1rem}}p{{color:#555}}</style>
</head><body><h1>{title}</h1><p>{body}</p></body></html>"""
    return HTMLResponse(content=html, status_code=200)
