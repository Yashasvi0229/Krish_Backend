"""
Security primitives — the ONE place that touches crypto.

Provides:
    * `fernet_encrypt` / `fernet_decrypt` — symmetric encryption for secrets at
      rest (Google refresh tokens go through here before hitting the DB).
    * `create_access_token` / `decode_access_token` — JWT issue/verify for
      the admin login session.
    * `create_state_token` / `decode_state_token` — short-lived signed tokens
      used as OAuth `state` parameters (CSRF protection for the Gmail
      connect flow). No server-side state needed — the JWT signature IS
      the anti-forgery guarantee.
    * `verify_password` — constant-time comparison for the admin password.

Design principles:
    * Every crypto operation raises `AppException` subclasses on failure.
      Callers never need to catch cryptography-library-specific errors.
    * All secrets are read from `settings` — never a positional argument
      that could accidentally be logged.
    * Fernet payloads are stored as `str` (base64-safe) so DB columns can
      stay `TEXT` — no BYTEA needed.
"""
from __future__ import annotations

import base64
import hmac
import secrets as _secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken
from jose import JWTError, jwt

from app.config import settings
from app.core.exceptions import UnauthorizedError

# Token type constants — kept as literals so decoding can reject tokens
# issued for the wrong purpose (a state token can't be used as a session
# token and vice-versa, even though both are signed with the same key).
TOKEN_TYPE_ACCESS: Final[str] = "access"
TOKEN_TYPE_STATE: Final[str] = "state"


# ---------------------------------------------------------------------------
# Fernet — symmetric encryption for tokens at rest
# ---------------------------------------------------------------------------
def _get_fernet() -> Fernet:
    """Build a Fernet instance from settings.key_encryption_key.

    Fernet demands a 32-byte url-safe base64 key. If ours doesn't look right
    we fail loud so the operator fixes their env, rather than silently
    corrupting data.
    """
    key = settings.key_encryption_key.encode("utf-8")
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:  # pragma: no cover — config sanity
        raise RuntimeError(
            "KEY_ENCRYPTION_KEY is not a valid Fernet key. Generate one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        ) from exc


def fernet_encrypt(plaintext: str) -> str:
    """Encrypt a UTF-8 string. Returns base64-safe ciphertext suitable for TEXT columns."""
    if plaintext is None:
        raise ValueError("Refusing to encrypt None — pass an empty string if intentional.")
    token = _get_fernet().encrypt(plaintext.encode("utf-8"))
    return token.decode("ascii")


def fernet_decrypt(ciphertext: str) -> str:
    """Decrypt a Fernet ciphertext. Raises UnauthorizedError if the ciphertext
    was tampered with or was encrypted under a different key."""
    try:
        plaintext = _get_fernet().decrypt(ciphertext.encode("ascii"))
    except InvalidToken as exc:
        raise UnauthorizedError(
            "Stored credential is invalid or was encrypted with a different key. "
            "The Gmail account must be reconnected."
        ) from exc
    return plaintext.decode("utf-8")


# ---------------------------------------------------------------------------
# JWT — session access tokens
# ---------------------------------------------------------------------------
def _now_utc() -> datetime:
    return datetime.now(UTC)


def create_access_token(
    subject: str,
    role: str,
    extra_claims: dict[str, Any] | None = None,
    expires_in_hours: int | None = None,
) -> tuple[str, int]:
    """Issue a session JWT. Returns (token, expires_in_seconds).

    Claims:
        sub    — the admin's email (used to look up identity)
        role   — role string (Admin / PM / ... — see UserRole enum)
        type   — always "access" here; state tokens use "state"
        iat    — issued at
        exp    — expires at
        jti    — unique token id (for future revocation lists)
    """
    hours = expires_in_hours or settings.jwt_access_token_expire_hours
    now = _now_utc()
    expire = now + timedelta(hours=hours)

    payload: dict[str, Any] = {
        "sub": subject,
        "role": role,
        "type": TOKEN_TYPE_ACCESS,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "jti": _secrets.token_urlsafe(16),
    }
    if extra_claims:
        payload.update(extra_claims)

    token = jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)
    return token, hours * 3600


def decode_access_token(token: str) -> dict[str, Any]:
    """Verify + decode a session JWT. Raises UnauthorizedError on any problem."""
    try:
        claims = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub"]},
        )
    except JWTError as exc:
        raise UnauthorizedError("Invalid or expired session token.") from exc

    if claims.get("type") != TOKEN_TYPE_ACCESS:
        # A state token can't impersonate a session token.
        raise UnauthorizedError("Wrong token type for this endpoint.")
    return claims


# ---------------------------------------------------------------------------
# State tokens — CSRF protection for OAuth flows
# ---------------------------------------------------------------------------
def create_state_token(
    subject: str,
    purpose: str,
    expires_in_minutes: int = 10,
) -> str:
    """Signed short-lived token used as an OAuth `state` parameter.

    `purpose` distinguishes different OAuth flows (e.g. "gmail_connect")
    so a state token generated for one flow can't be replayed on another.
    """
    now = _now_utc()
    expire = now + timedelta(minutes=expires_in_minutes)
    payload = {
        "sub": subject,
        "type": TOKEN_TYPE_STATE,
        "purpose": purpose,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "nonce": _secrets.token_urlsafe(8),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_state_token(token: str, expected_purpose: str) -> dict[str, Any]:
    """Verify + decode a state token. Rejects if purpose mismatches."""
    try:
        claims = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[settings.jwt_algorithm],
            options={"require": ["exp", "iat", "sub", "purpose"]},
        )
    except JWTError as exc:
        raise UnauthorizedError("OAuth state token is invalid or has expired.") from exc

    if claims.get("type") != TOKEN_TYPE_STATE:
        raise UnauthorizedError("Wrong token type — not an OAuth state token.")
    if claims.get("purpose") != expected_purpose:
        raise UnauthorizedError("OAuth state token was issued for a different flow.")
    return claims


# ---------------------------------------------------------------------------
# Password check
# ---------------------------------------------------------------------------
def verify_password(plain: str, expected: str) -> bool:
    """Constant-time password comparison.

    Both arguments are compared byte-for-byte in constant time — no early
    exit on first-differing char. This defeats timing-attack password guessing.
    """
    if not plain or not expected:
        # Never let empty passwords match — even if both env and input are "".
        return False
    return hmac.compare_digest(plain.encode("utf-8"), expected.encode("utf-8"))


# ---------------------------------------------------------------------------
# Utility: generate a Fernet key (for `python -m app.core.security`)
# ---------------------------------------------------------------------------
def _generate_fernet_key() -> str:  # pragma: no cover — one-off ops helper
    return Fernet.generate_key().decode("ascii")


if __name__ == "__main__":  # pragma: no cover
    # `python -m app.core.security` prints a fresh Fernet key.
    print(_generate_fernet_key())
