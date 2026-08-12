"""
Business logic for the login endpoint.

Phase 1 (this file today): admin credentials come from env vars — no DB
lookup, no users table row. Login just validates the incoming email +
password against `settings.admin_email` + `settings.admin_password` in
constant time and issues a JWT.

Phase 2 (later): flip to DB-backed users. This module's public interface
(`login()`) stays the same — only the internals change.
"""
from __future__ import annotations

from app.config import settings
from app.core.constants import UserRole
from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger
from app.core.security import create_access_token, verify_password
from app.schemas.auth import LoginResponse, MeResponse

log = get_logger(__name__)


def login(email: str, password: str) -> LoginResponse:
    """Validate credentials, issue a JWT, return the login response payload.

    Raises:
        UnauthorizedError — bad credentials OR the admin password is not
            configured on the server. Both cases return an identical error
            message to the client so an attacker can't tell whether an email
            exists.
    """
    # Guard: reject login attempts before ADMIN_PASSWORD is set in env.
    if not settings.admin_password:
        log.warning("login_attempt_but_admin_password_not_configured", email=email)
        raise UnauthorizedError("Invalid email or password.")

    # Constant-time comparisons prevent user-enumeration via timing.
    email_ok = verify_password(email.lower(), settings.admin_email.lower())
    password_ok = verify_password(password, settings.admin_password)

    # Always run BOTH comparisons before deciding — no early-exit — so
    # response timing doesn't leak which field was wrong.
    if not (email_ok and password_ok):
        log.info("login_failed", email=email)
        raise UnauthorizedError("Invalid email or password.")

    token, expires_in = create_access_token(
        subject=settings.admin_email,
        role=UserRole.ADMIN.value,
        extra_claims={"name": settings.admin_display_name},
    )

    log.info("login_success", email=settings.admin_email)
    return LoginResponse(
        access_token=token,
        token_type="Bearer",
        expires_in=expires_in,
        user=MeResponse(
            email=settings.admin_email,
            name=settings.admin_display_name,
            role=UserRole.ADMIN.value,
        ),
    )
