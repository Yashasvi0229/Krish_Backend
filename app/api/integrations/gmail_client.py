"""
Async Gmail REST API client.

Wraps just the Gmail endpoints we need — no google-api-python-client
dependency, no sync-in-async awkwardness. Every call is native async via
httpx. Google's REST API surface is simple enough that this is cleaner
than pulling in the giant client library.

Token lifecycle handled here:
    * `access_token` in memory + DB (short-lived, ~1 hour)
    * `refresh_token` in DB (Fernet-encrypted, long-lived)
    * On 401 from Gmail → refresh, retry ONCE, then bail

Rate-limit strategy:
    * Google returns 429 with a `Retry-After` header. We wait, then retry
      up to 3 times with exponential backoff.

Endpoints we call:
    users.messages.list       — search
    users.messages.get        — full message
    users.messages.attachments.get — attachment bytes
    users.getProfile          — for history checkpoints
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings
from app.core.exceptions import ExternalServiceError, UnauthorizedError
from app.core.logging import get_logger
from app.core.security import fernet_decrypt, fernet_encrypt

log = get_logger(__name__)

# ---- Google endpoint constants (public, stable) ----
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1"

# HTTP timeouts — Gmail can be slow on large attachments, so be generous.
_TIMEOUT = httpx.Timeout(60.0, connect=10.0)

# Retry policy for rate-limited / transient failures
_MAX_RETRIES = 3
_BACKOFF_BASE = 1.5


# ---------------------------------------------------------------------------
# Credential container
# ---------------------------------------------------------------------------
@dataclass
class GmailCredentials:
    """In-memory credentials passed to GmailClient.

    `refresh_token_encrypted` is what we store in `gmail_connection`. We
    decrypt it lazily just before hitting Google's token endpoint.
    `access_token` is the fresh short-lived one. If expired, the client
    refreshes it automatically.
    """
    refresh_token_encrypted: str
    access_token: str | None
    access_token_expiry: datetime | None

    def is_access_token_valid(self, safety_margin_seconds: int = 60) -> bool:
        """Return True if we can still use the access_token as-is."""
        if not self.access_token or not self.access_token_expiry:
            return False
        now = datetime.now(UTC)
        return self.access_token_expiry > now + timedelta(seconds=safety_margin_seconds)


@dataclass
class RefreshResult:
    """New credentials produced by a token refresh. Persist these back to DB."""
    access_token: str
    access_token_expiry: datetime
    access_token_encrypted: str    # encrypted variant ready for DB write


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class GmailClient:
    """
    Stateful async Gmail client.

    Typical usage:
        client = GmailClient(creds)
        try:
            ids = await client.list_message_ids(query="claim 123", max_results=50)
            for mid in ids:
                msg = await client.get_message(mid)
                ...
        finally:
            await client.aclose()

    If the access token was refreshed during the session, `client.refreshed`
    is populated — persist it back to the `gmail_connection` row.
    """

    def __init__(self, credentials: GmailCredentials, user_id: str = "me") -> None:
        self.credentials = credentials
        self.user_id = user_id          # "me" = the mailbox this token belongs to
        self.refreshed: RefreshResult | None = None
        self._http = httpx.AsyncClient(timeout=_TIMEOUT)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "GmailClient":
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self.aclose()

    # ---- Public API -----------------------------------------------------
    async def list_message_ids(
        self,
        query: str,
        max_results: int | None = None,
        include_spam_trash: bool = False,
    ) -> list[str]:
        """
        Return message IDs matching a Gmail search query.

        Google returns up to 500 per page. We follow `nextPageToken` until
        `max_results` reached OR the search is exhausted.
        """
        cap = max_results or settings.gmail_max_results_per_search
        out: list[str] = []
        page_token: str | None = None
        while len(out) < cap:
            page_size = min(500, cap - len(out))
            params: dict[str, Any] = {
                "q": query,
                "maxResults": page_size,
                "includeSpamTrash": "true" if include_spam_trash else "false",
            }
            if page_token:
                params["pageToken"] = page_token
            data = await self._get(f"/users/{self.user_id}/messages", params=params)
            for m in data.get("messages") or []:
                out.append(m["id"])
                if len(out) >= cap:
                    break
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return out

    async def get_message(self, message_id: str, fmt: str = "full") -> dict[str, Any]:
        """Fetch full message JSON.

        `fmt`:
          * "full"     — headers + parsed body (default)
          * "metadata" — headers only (cheaper, use for sync scan)
          * "minimal"  — just IDs / snippet / thread
          * "raw"      — RFC-822 encoded (heaviest)
        """
        return await self._get(
            f"/users/{self.user_id}/messages/{message_id}",
            params={"format": fmt},
        )

    async def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Download an attachment's raw bytes."""
        data = await self._get(
            f"/users/{self.user_id}/messages/{message_id}/attachments/{attachment_id}"
        )
        b64 = data.get("data", "")
        # Gmail uses URL-safe base64 without padding.
        padding = "=" * (-len(b64) % 4)
        return base64.urlsafe_b64decode(b64 + padding)

    async def get_profile(self) -> dict[str, Any]:
        """Returns { emailAddress, messagesTotal, threadsTotal, historyId }.
        Handy for storing `last_history_id` in gmail_connection."""
        return await self._get(f"/users/{self.user_id}/profile")

    # ---- HTTP + auth + retry -------------------------------------------
    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """GET wrapper with automatic token refresh and retry-on-429."""
        for attempt in range(_MAX_RETRIES + 1):
            token = await self._ensure_access_token()
            resp = await self._http.get(
                GMAIL_BASE + path,
                params=params or {},
                headers={"Authorization": f"Bearer {token}"},
            )

            # ---- 200: parse & return --------------------------------
            if resp.status_code == 200:
                return resp.json()

            # ---- 401: token likely expired — force refresh & retry ONCE
            if resp.status_code == 401 and attempt == 0:
                log.info("gmail_token_expired_mid_call", path=path)
                self.credentials.access_token = None
                self.credentials.access_token_expiry = None
                continue

            # ---- 429 / 5xx: back off & retry ------------------------
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if attempt < _MAX_RETRIES:
                    delay = float(resp.headers.get("Retry-After") or (_BACKOFF_BASE ** attempt))
                    log.warning(
                        "gmail_rate_limited_or_5xx",
                        status=resp.status_code, attempt=attempt, delay=delay,
                    )
                    await asyncio.sleep(delay)
                    continue

            # ---- Any other error: bubble up as ExternalServiceError -
            body = _safe_json(resp)
            raise ExternalServiceError(
                f"Gmail {path} failed: {resp.status_code} "
                f"{body.get('error', {}).get('message') or resp.text[:200]}"
            )

        # Only reachable if we exhausted retries.
        raise ExternalServiceError(f"Gmail {path} failed after {_MAX_RETRIES} retries.")

    async def _ensure_access_token(self) -> str:
        """Return a valid access_token, refreshing from Google if needed."""
        if self.credentials.is_access_token_valid():
            return self.credentials.access_token or ""

        # ---- Refresh ---------------------------------------------------
        refresh_token = fernet_decrypt(self.credentials.refresh_token_encrypted)

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
        }

        try:
            resp = await self._http.post(GOOGLE_TOKEN_URL, data=payload)
        except httpx.HTTPError as exc:
            raise ExternalServiceError(f"Token refresh unreachable: {exc}") from exc

        if resp.status_code != 200:
            body = _safe_json(resp)
            # A revoked refresh_token gives {"error": "invalid_grant"}.
            # This is unrecoverable — user must reconnect Gmail.
            if body.get("error") == "invalid_grant":
                raise UnauthorizedError(
                    "Stored Gmail refresh token has been revoked. "
                    "Please reconnect the Gmail account in Settings."
                )
            raise ExternalServiceError(
                f"Token refresh failed: {body.get('error_description') or resp.text}"
            )

        j = resp.json()
        access_token = j["access_token"]
        expires_in = int(j.get("expires_in", 3600))
        expiry = datetime.now(UTC) + timedelta(seconds=expires_in)

        # Update in-memory creds
        self.credentials.access_token = access_token
        self.credentials.access_token_expiry = expiry

        # Signal to the caller so they persist to DB
        self.refreshed = RefreshResult(
            access_token=access_token,
            access_token_expiry=expiry,
            access_token_encrypted=fernet_encrypt(access_token),
        )
        log.info("gmail_access_token_refreshed", expires_in=expires_in)
        return access_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _safe_json(resp: httpx.Response) -> dict[str, Any]:
    try:
        return resp.json()
    except ValueError:
        return {}
