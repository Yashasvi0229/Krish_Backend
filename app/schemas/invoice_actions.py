"""Request schemas for invoice-level actions."""
from __future__ import annotations

from pydantic import BaseModel, Field


class InvoiceDeleteRequest(BaseModel):
    """DELETE payload — reason recommended for audit."""
    reason: str | None = Field(default=None, max_length=500)


class InvoiceDuplicateRequest(BaseModel):
    """POST /invoices/{id}/duplicate — payload is optional.

    We deliberately do NOT allow overriding the client on duplicate;
    "duplicate" means "same invoice, new number/date so it can be edited
    fresh". If the reviewer wants a different client, they should create
    a brand new invoice through the search flow.
    """
    note: str | None = Field(default=None, max_length=500)
