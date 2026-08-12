"""
Gmail service — OAuth 2.0 authorization-code flow against Google.

Flow overview:
    1. Admin clicks "Connect Gmail" on frontend.
    2. Frontend calls POST /api/gmail/connect (admin auth).
    3. This service builds a Google consent URL with a signed `state` token.
    4. Frontend redirects the browser to that URL.
    5. Admin picks the GNC Gmail account, approves the scopes.
    6. Google redirects the browser to our callback with `?code=xxx&state=yyy`.
    7. Callback handler calls `exchange_code_for_tokens()`:
         a. Verifies the `state` JWT (CSRF protection).
         b. POSTs to Google's token endpoint to exchange `code` → tokens.
         c. GETs userinfo to know which Gmail account was picked.
         d. Encrypts the refresh_token with Fernet.
         e. UPSERTs the singleton `gmail_connection` row.
    8. Callback handler redirects the browser back to the frontend settings page.

Google's scopes we ask for:
    * openid, email, profile — so we can identify which account connected.
    * https://www.googleapis.com/auth/gmail.readonly — read-only Gmail access.

We force `access_type=offline` + `prompt=consent` so Google always returns
a refresh_token (without these, subsequent authorizations return only an
access_token, and we'd need re-consent every hour).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ExternalServiceError,
    UnauthorizedError,
)
from app.core.logging import get_logger
from app.core.security import (
    create_state_token,
    decode_state_token,
    fernet_decrypt,
    fernet_encrypt,
)
from app.models.gmail_connection import GmailConnection
from app.repositories import gmail_connection_repo
from app.schemas.gmail import GmailStatusResponse

log = get_logger(__name__)

# ---- Google endpoints (public constants — unlikely to change) ----
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

# Scopes — space-separated per OAuth 2 convention.
GMAIL_SCOPES = " ".join([
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
])

# Purpose string embedded in the state token — must match on both sides.
STATE_PURPOSE = "gmail_connect"

# HTTP client tuning: Google's token endpoint responds in well under a
# second normally; 15 s is a generous ceiling that also survives transient
# network hiccups from Render's egress.
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


# ---------------------------------------------------------------------------
# Step 1 — build the consent URL
# ---------------------------------------------------------------------------
def build_authorization_url(admin_email: str) -> str:
    """Construct the Google OAuth consent URL for the given admin.

    Raises `ConflictError` if Google credentials aren't configured on the
    server (missing GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI).
    """
    if not settings.google_client_id or not settings.google_redirect_uri:
        raise ConflictError(
            "Google OAuth is not configured on the server. "
            "Set GOOGLE_CLIENT_ID and GOOGLE_REDIRECT_URI env vars."
        )

    state = create_state_token(subject=admin_email, purpose=STATE_PURPOSE)

    params = {
        "response_type": "code",
        "client_id": settings.google_client_id,
        "redirect_uri": settings.google_redirect_uri,
        "scope": GMAIL_SCOPES,
        # `offline` = give us a refresh_token; `consent` = always show the
        # consent screen so Google re-issues a refresh_token every time
        # (needed if the operator has to reconnect after rotating secrets).
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


# ---------------------------------------------------------------------------
# Step 2 — exchange the auth code for tokens
# ---------------------------------------------------------------------------
async def exchange_code_for_tokens(
    code: str,
    state: str,
    session: AsyncSession,
) -> GmailConnection:
    """Called by the OAuth callback.

    Verifies the state token, exchanges the auth code for tokens with
    Google, fetches user info, encrypts the refresh token, and UPSERTs
    the singleton row.
    """
    # ---- 1. CSRF: verify the state JWT we issued in build_authorization_url ----
    if not code:
        raise BadRequestError("Missing `code` query parameter from Google.")
    if not state:
        raise BadRequestError("Missing `state` query parameter from Google.")

    state_claims = decode_state_token(state, expected_purpose=STATE_PURPOSE)
    admin_email = state_claims["sub"]

    # ---- 2. Exchange code → tokens ----
    if not settings.google_client_secret:
        raise ConflictError("GOOGLE_CLIENT_SECRET not configured on server.")

    token_response = await _post_token_exchange(code)
    refresh_token = token_response.get("refresh_token")
    access_token = token_response.get("access_token")
    expires_in = token_response.get("expires_in", 3600)
    scope = token_response.get("scope", GMAIL_SCOPES)

    if not refresh_token:
        # This happens if the user has previously granted consent and
        # Google decided not to re-issue a refresh token. Our
        # `prompt=consent` should prevent this, but be defensive.
        raise ExternalServiceError(
            "Google did not return a refresh token. "
            "Revoke previous access at https://myaccount.google.com/permissions "
            "and try connecting again."
        )

    # ---- 3. Ask Google who just authenticated ----
    userinfo = await _fetch_userinfo(access_token)
    google_email = userinfo.get("email")
    google_display_name = userinfo.get("name")
    google_user_id = userinfo.get("sub")

    if not google_email:
        raise ExternalServiceError("Google userinfo response missing `email`.")

    # ---- 4. Optional domain lock ----
    allowed_domain = (settings.google_allowed_domain or "").strip().lower()
    if allowed_domain and not google_email.lower().endswith("@" + allowed_domain):
        raise UnauthorizedError(
            f"Only accounts from @{allowed_domain} may be connected."
        )

    # ---- 5. Encrypt + persist ----
    now = datetime.now(UTC)
    row = await gmail_connection_repo.upsert_singleton(
        session,
        email=google_email,
        display_name=google_display_name,
        google_user_id=google_user_id,
        refresh_token_encrypted=fernet_encrypt(refresh_token),
        access_token_encrypted=fernet_encrypt(access_token) if access_token else None,
        access_token_expiry=now + timedelta(seconds=int(expires_in)),
        scopes=scope,
        is_connected=True,
        connected_by_admin_email=admin_email,
    )
    await session.commit()

    log.info(
        "gmail_connected",
        gmail_email=google_email,
        connected_by=admin_email,
    )
    return row


# ---------------------------------------------------------------------------
# Status / disconnect
# ---------------------------------------------------------------------------
async def get_status(session: AsyncSession) -> GmailStatusResponse:
    """Return the current Gmail connection state (or `connected=False`)."""
    row = await gmail_connection_repo.get_singleton(session)
    if row is None or not row.is_connected:
        return GmailStatusResponse(connected=False)
    return GmailStatusResponse(
        connected=True,
        email=row.email,
        display_name=row.display_name,
        scopes=row.scopes,
        last_sync_at=row.last_sync_at,
        connected_by_admin_email=row.connected_by_admin_email,
        connected_at=row.created_at,
    )


async def disconnect(session: AsyncSession) -> bool:
    """Remove the singleton row. Returns True if a connection existed."""
    removed = await gmail_connection_repo.delete_singleton(session)
    if removed:
        await session.commit()
        log.info("gmail_disconnected")
    return removed


# ---------------------------------------------------------------------------
# Private HTTP helpers
# ---------------------------------------------------------------------------
async def _post_token_exchange(code: str) -> dict[str, Any]:
    """POST to Google's token endpoint, return the JSON body.
    Raises ExternalServiceError with the Google error if it fails."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": settings.google_client_id,
        "client_secret": settings.google_client_secret,
        "redirect_uri": settings.google_redirect_uri,
    }
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.post(GOOGLE_TOKEN_URL, data=payload)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Google token endpoint unreachable: {exc}") from exc

    if resp.status_code != 200:
        # Google returns {"error": "...", "error_description": "..."}
        detail = _safe_json(resp)
        log.warning("google_token_exchange_failed", status=resp.status_code, body=detail)
        raise ExternalServiceError(
            "Google rejected the authorization code: "
            f"{detail.get('error_description') or detail.get('error') or resp.text}"
        )
    return resp.json()


async def _fetch_userinfo(access_token: str) -> dict[str, Any]:
    """GET userinfo with the freshly-issued access_token."""
    async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
        try:
            resp = await client.get(
                GOOGLE_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            )
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Google userinfo endpoint unreachable: {exc}") from exc

    if resp.status_code != 200:
        detail = _safe_json(resp)
        raise ExternalServiceError(
            f"Google userinfo call failed: {detail.get('error') or resp.text}"
        )
    return resp.json()


def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError:
        return {}


# ===========================================================================
# Step 4 additions — fetch / search / parse
# ===========================================================================
"""
Additions below extend the Gmail service beyond OAuth (Step 3) to the
actual email-fetching pipeline. Kept in the same module so the connection
lifecycle and the fetch lifecycle share one namespace.
"""

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any

from app.integrations.gmail_client import GmailClient, GmailCredentials
from app.repositories import gmail_connection_repo as _gmail_conn_repo
from app.utils.gmail_query import is_internal_email


@dataclass
class ParsedEmail:
    """The subset of a Gmail message we persist to `emails`."""
    gmail_message_id: str
    gmail_thread_id: str
    subject: str
    from_email: str
    from_name: str
    to_emails: list[str]
    cc_emails: list[str]
    date: datetime
    body_text: str            # UTF-8, plain (HTML stripped if that's all we had)
    body_snippet: str
    is_internal: bool
    raw_attachments: list[dict[str, Any]]      # {attachmentId, filename, mimeType, size}


# ---------------------------------------------------------------------------
# Credential bootstrap
# ---------------------------------------------------------------------------
async def build_gmail_client(session) -> GmailClient:
    """Load the singleton gmail_connection row and return a ready GmailClient.

    Raises ConflictError if no connection exists yet.
    """
    row = await _gmail_conn_repo.get_singleton(session)
    if row is None or not row.is_connected:
        raise ConflictError(
            "Gmail is not connected. Have an admin visit /api/gmail/connect first."
        )
    creds = GmailCredentials(
        refresh_token_encrypted=row.refresh_token_encrypted,
        access_token=(
            fernet_decrypt(row.access_token_encrypted)
            if row.access_token_encrypted else None
        ),
        access_token_expiry=row.access_token_expiry,
    )
    return GmailClient(creds)


async def persist_refreshed_tokens(session, client: GmailClient) -> None:
    """After a Gmail session, write the (possibly refreshed) access_token
    back to the singleton row so the next request doesn't need to refresh."""
    if client.refreshed is None:
        return
    await _gmail_conn_repo.upsert_singleton(
        session,
        access_token_encrypted=client.refreshed.access_token_encrypted,
        access_token_expiry=client.refreshed.access_token_expiry,
    )
    await session.commit()


# ---------------------------------------------------------------------------
# Gmail message → ParsedEmail
# ---------------------------------------------------------------------------
def parse_gmail_message(raw: dict[str, Any]) -> ParsedEmail:
    """Convert Gmail's `messages.get` JSON into our internal shape.

    Handles the payload/parts recursion, prefers text/plain over text/html,
    and collects attachment metadata (bytes are fetched later, on demand).
    """
    payload = raw.get("payload", {}) or {}
    headers_by_name = {
        h["name"].lower(): h["value"]
        for h in payload.get("headers", []) or []
    }

    subject = headers_by_name.get("subject", "")
    from_raw = headers_by_name.get("from", "")
    to_raw = headers_by_name.get("to", "")
    cc_raw = headers_by_name.get("cc", "")
    date_raw = headers_by_name.get("date", "")

    from_name, from_email = _parse_from(from_raw)
    to_emails = _parse_addr_list(to_raw)
    cc_emails = _parse_addr_list(cc_raw)
    date_dt = _parse_date(date_raw, fallback_epoch_ms=raw.get("internalDate"))

    body_text, attachments = _walk_parts(payload)
    snippet = raw.get("snippet") or body_text[:500]

    internal = is_internal_email(
        from_email=from_email,
        to_emails=to_emails,
        cc_emails=cc_emails,
        internal_domain=settings.gmail_internal_domain,
    )

    return ParsedEmail(
        gmail_message_id=raw.get("id", ""),
        gmail_thread_id=raw.get("threadId", ""),
        subject=subject,
        from_email=from_email,
        from_name=from_name,
        to_emails=to_emails,
        cc_emails=cc_emails,
        date=date_dt,
        body_text=body_text,
        body_snippet=snippet,
        is_internal=internal,
        raw_attachments=attachments,
    )


def _walk_parts(part: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Recursive walk. Prefer text/plain over text/html.
    Returns (best_body_text, [attachment_meta, ...])."""
    plain_chunks: list[str] = []
    html_chunks: list[str] = []
    attachments: list[dict[str, Any]] = []

    def visit(p: dict[str, Any]) -> None:
        mime = (p.get("mimeType") or "").lower()
        body = p.get("body") or {}
        att_id = body.get("attachmentId")
        filename = p.get("filename") or ""

        # Attachment: has an attachmentId, or has a filename + no children
        if att_id or (filename and not p.get("parts")):
            attachments.append({
                "attachmentId": att_id,
                "filename": filename,
                "mimeType": mime,
                "size": body.get("size", 0),
            })
            return

        # Inline body
        if mime.startswith("text/") and body.get("data"):
            decoded = _b64_decode(body["data"])
            if mime == "text/plain":
                plain_chunks.append(decoded)
            elif mime == "text/html":
                html_chunks.append(_html_to_text(decoded))

        # Recurse
        for child in p.get("parts") or []:
            visit(child)

    visit(part)

    body = "\n\n".join(plain_chunks).strip() or "\n\n".join(html_chunks).strip()
    return body, attachments


def _b64_decode(data: str) -> str:
    padding = "=" * (-len(data) % 4)
    try:
        raw = base64.urlsafe_b64decode(data + padding)
        return raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return ""


def _html_to_text(html: str) -> str:
    """Cheap HTML → text. We don't need perfect rendering — just strip
    tags for AI/search purposes."""
    import re
    text = re.sub(r"<script.*?>.*?</script>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style.*?>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p\s*>", "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    # Collapse whitespace
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _parse_from(raw: str) -> tuple[str, str]:
    if not raw:
        return "", ""
    addrs = getaddresses([raw])
    if not addrs:
        return "", ""
    name, email = addrs[0]
    return name.strip() or email.split("@")[0], email.strip().lower()


def _parse_addr_list(raw: str) -> list[str]:
    if not raw:
        return []
    return [e.strip().lower() for _, e in getaddresses([raw]) if e]


def _parse_date(raw: str, fallback_epoch_ms: str | int | None = None) -> datetime:
    if raw:
        try:
            dt = parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except Exception:  # noqa: BLE001
            pass
    if fallback_epoch_ms:
        try:
            return datetime.fromtimestamp(int(fallback_epoch_ms) / 1000, tz=UTC)
        except Exception:  # noqa: BLE001
            pass
    return datetime.now(UTC)
