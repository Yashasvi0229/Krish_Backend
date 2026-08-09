"""
Reusable FastAPI dependencies.

Every route that needs auth pulls its identity from `get_current_admin`.
Callers get a `CurrentAdmin` dataclass — a small, typed identity object —
never a raw JWT dict. This means route handlers can't accidentally read
untrusted claims off a token.

In Phase 2 (multi-user) we'll swap the internals: `get_current_admin`
becomes `get_current_user` and does a DB lookup + role check. The public
interface stays the same, so route handlers don't change.

We use FastAPI's `HTTPBearer` security scheme (rather than reading the
Authorization header manually) so Swagger UI renders an "Authorize" button
that developers can click to paste a token once and have it applied to
every protected endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings
from app.core.constants import UserRole
from app.core.exceptions import ForbiddenError, UnauthorizedError
from app.core.security import decode_access_token


@dataclass(frozen=True, slots=True)
class CurrentAdmin:
    """Identity of the currently-authenticated admin."""

    email: str
    name: str
    role: str


# `auto_error=False` — we raise our OWN UnauthorizedError with a clean
# JSON envelope instead of letting HTTPBearer raise its default 403.
_bearer_scheme = HTTPBearer(
    auto_error=False,
    scheme_name="BearerAuth",
    description="Paste the `access_token` returned by /api/auth/login.",
)


async def get_current_admin(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(_bearer_scheme),
    ],
) -> CurrentAdmin:
    """FastAPI dependency — returns the current admin, or 401/403.

    Phase 1: verifies the JWT and returns the admin identity built from
    env-var credentials. The JWT's `sub` claim MUST match `settings.admin_email`
    — this prevents a stolen dev token from being valid in prod, or vice-versa.
    """
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("Missing or invalid Authorization header.")

    claims = decode_access_token(credentials.credentials)

    # Cross-check: the token's subject must match the currently configured
    # admin. If the admin email is rotated in env, all old tokens die.
    subject = claims.get("sub", "")
    if subject != settings.admin_email:
        raise UnauthorizedError("Token identity does not match current admin.")

    role = claims.get("role", UserRole.USER.value)
    if role != UserRole.ADMIN.value:
        # Phase 1: only Admin exists. Explicit check so Phase 2 doesn't
        # accidentally leak admin-only endpoints to lower roles.
        raise ForbiddenError("Admin role required.")

    return CurrentAdmin(
        email=subject,
        name=claims.get("name", settings.admin_display_name),
        role=role,
    )


# Re-exported for convenience — matches conventional FastAPI usage.
__all__ = ["CurrentAdmin", "get_current_admin"]
