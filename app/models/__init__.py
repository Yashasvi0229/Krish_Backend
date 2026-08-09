"""
Models package — imports every ORM class so that:

    1. `Base.metadata` sees them all (needed for Alembic autogenerate and
       for `Base.metadata.create_all()` in dev).
    2. Consumers can `from app.models import User, Client, ...` without
       knowing individual module paths.

Order below follows dependency order but SQLAlchemy handles forward refs
via string relationships, so order doesn't strictly matter here.
"""
from app.models.base import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin

# ---- Independent / leaf tables ----
from app.models.user import User
from app.models.client import Client
from app.models.billing_rule import BillingRule
from app.models.gmail_connection import GmailConnection

# ---- Depends on Client ----
from app.models.claim import Claim

# ---- Depends on Claim ----
from app.models.email import Email

# ---- Depends on Email ----
from app.models.attachment import Attachment

# ---- Depends on Email + Attachment ----
from app.models.ai_analysis import AIAnalysis

# ---- Depends on User ----
from app.models.processing_job import ProcessingJob

# ---- Depends on Claim, Client, User, ProcessingJob ----
from app.models.invoice_draft import InvoiceDraft

# ---- Depends on InvoiceDraft, Claim, Client, User ----
from app.models.invoice import Invoice

# ---- Depends on Client, Claim, Invoice ----
from app.models.billing_ledger import BillingLedger

# ---- Depends on User ----
from app.models.audit_log import AuditLog


__all__ = [
    # Base
    "Base",
    "UUIDPKMixin",
    "TimestampMixin",
    "SoftDeleteMixin",
    # Tables
    "User",
    "Client",
    "BillingRule",
    "GmailConnection",
    "Claim",
    "Email",
    "Attachment",
    "AIAnalysis",
    "ProcessingJob",
    "InvoiceDraft",
    "Invoice",
    "BillingLedger",
    "AuditLog",
]
