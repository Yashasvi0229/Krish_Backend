"""
Enum constants used across ORM models, schemas, and services.

We use `str` + `enum.Enum` (i.e. StrEnum) so:
    * DB column type stays VARCHAR (matches spec §16.3, easy to inspect in psql,
      easy to add new values without a DB migration).
    * Python code gets type-safe values via `UserRole.ADMIN`.
    * Pydantic serializes them as plain strings out-of-the-box.
    * `member.value` == the raw string stored in DB — no translation layer.

If you add a value here, also update:
    1. Any DB check constraint that references it (models/*.py).
    2. Any migration that seeded default rows.
"""
from __future__ import annotations

from enum import StrEnum


# ---- Users -----------------------------------------------------------------
class UserRole(StrEnum):
    ADMIN = "Admin"
    PM = "PM"
    HOUR_VERIFIER = "HourVerifier"
    RS_APPROVER = "RSApprover"
    USER = "User"


# ---- Clients ---------------------------------------------------------------
class ClientType(StrEnum):
    INSURANCE = "Insurance"
    ADJUSTER = "Adjuster"
    CONTRACTOR = "Contractor"
    OTHER = "Other"


class RateStrategy(StrEnum):
    FLAT = "flat"
    TIERED = "tiered"
    FEE_BUDGET = "fee_budget"


# ---- Claims ----------------------------------------------------------------
class ClaimStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"


# ---- AI analyses -----------------------------------------------------------
class AnalysisType(StrEnum):
    EMAIL_CLASSIFY = "email_classify"
    ATTACHMENT_SUMMARY = "attachment_summary"
    DESCRIPTION_REWRITE = "description_rewrite"
    HOURS_RECOMMEND = "hours_recommend"


class AIProvider(StrEnum):
    CLAUDE = "claude"
    OPENAI = "openai"
    GEMINI = "gemini"


class Confidence(StrEnum):
    HIGH = "High"
    MEDIUM = "Medium"
    LOW = "Low"


# ---- Attachments -----------------------------------------------------------
class ExtractionStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


# ---- Billing rules ---------------------------------------------------------
class ChargeType(StrEnum):
    HOURLY = "hourly"
    FLAT_FEE = "flat_fee"
    PER_UNIT = "per_unit"


class UOM(StrEnum):
    HOURS = "hrs"
    MINUTES = "min"
    PER_PAGE = "per_page"
    PER_LINE = "per_line"
    PER_CALL = "per_call"
    FLAT = "flat"


class ClientScope(StrEnum):
    GLOBAL = "global"
    CLIENT_SPECIFIC = "client_specific"


# ---- Processing jobs -------------------------------------------------------
class JobType(StrEnum):
    INVOICE_SEARCH = "invoice_search"
    CLAIM_ANALYSIS = "claim_analysis"
    INVOICE_GENERATION = "invoice_generation"
    BACKFILL = "backfill"
    GMAIL_SYNC = "gmail_sync"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


# ---- Invoices / Drafts -----------------------------------------------------
class DraftStatus(StrEnum):
    """Multi-stage approval workflow. Order matters — see spec §12.2."""
    DRAFT = "DRAFT"
    PENDING_PM = "PENDING_PM"
    PENDING_HOUR_VERIFY = "PENDING_HOUR_VERIFY"
    PENDING_RS = "PENDING_RS"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class InvoiceStatus(StrEnum):
    APPROVED = "APPROVED"
    SENT = "SENT"
    CANCELLED = "CANCELLED"


# ---- Helpers ---------------------------------------------------------------
def enum_values(enum_cls: type[StrEnum]) -> list[str]:
    """Return the raw string values of a StrEnum — used to build CHECK constraints."""
    return [m.value for m in enum_cls]
