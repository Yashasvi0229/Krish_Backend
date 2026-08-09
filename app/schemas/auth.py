"""
Pydantic schemas for auth endpoints.

The frontend expects `access_token` at the top level of the login response
so its Axios interceptor can pull `res.data.access_token` directly. See
`src/services/api.js` in the frontend repo.

Note on `str` vs `EmailStr` for login: we use plain `str` because
(a) email-validator rejects RFC-reserved TLDs like `.local` and `.internal`
that are common in dev/private-network admin emails, and (b) the login
handler compares against `settings.admin_email` in constant time — a wrong
address just fails auth like a wrong password, no MX lookup is needed.
Client-facing fields (e.g. Client.email) still use EmailStr elsewhere.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254, pattern=r"^.+@.+\..+$")
    password: str = Field(min_length=1, max_length=200)


class MeResponse(BaseModel):
    """Identity of the currently-logged-in admin."""

    email: str
    name: str
    role: str


class LoginResponse(BaseModel):
    """Successful login response. Also embeds the user object so the frontend
    can populate its auth store in one shot."""

    access_token: str
    token_type: str = "Bearer"
    expires_in: int    # seconds until expiry
    user: MeResponse
