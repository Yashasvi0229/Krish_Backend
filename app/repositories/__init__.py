"""Repositories package — data-access layer.

Every module is a thin wrapper over SQLAlchemy queries so that services /
API layer never touches the ORM directly. This makes it possible to:
    * mock a single repo in tests
    * swap in a caching decorator later
    * keep query logic auditable in one place
"""
from app.repositories import (
    ai_analysis_repo,
    attachment_repo,
    billing_rule_repo,
    claim_repo,
    client_repo,
    draft_repo,
    email_repo,
    gmail_connection_repo,
    invoice_repo,
    job_repo,
    user_repo,
)

__all__ = [
    "ai_analysis_repo",
    "attachment_repo",
    "billing_rule_repo",
    "claim_repo",
    "client_repo",
    "draft_repo",
    "email_repo",
    "gmail_connection_repo",
    "invoice_repo",
    "job_repo",
    "user_repo",
]
