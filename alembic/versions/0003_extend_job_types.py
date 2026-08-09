"""Extend job_type CHECK constraint to include claim_analysis + invoice_generation.

Revision ID: 0003_extend_job_types
Revises: 0002_seed_billing_rules
Create Date: 2026-08-06

Step 5 adds two new JobType values (`claim_analysis`, `invoice_generation`).
Migration 0001 hard-coded the original enum into a CHECK constraint —
Postgres has no ALTER CONSTRAINT so we drop-and-recreate.

Idempotent: dropping a non-existent constraint would fail, so we use IF EXISTS.
"""
from __future__ import annotations

from alembic import op


revision = "0003_extend_job_types"
down_revision = "0002_seed_billing_rules"
branch_labels = None
depends_on = None


NEW_JOB_TYPES = [
    "invoice_search", "backfill", "gmail_sync",
    "claim_analysis", "invoice_generation",
]

OLD_JOB_TYPES = ["invoice_search", "backfill", "gmail_sync"]


def _quoted(values: list[str]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def upgrade() -> None:
    op.execute("ALTER TABLE processing_jobs DROP CONSTRAINT IF EXISTS ck_processing_jobs_job_type_valid")
    op.execute(
        f"ALTER TABLE processing_jobs ADD CONSTRAINT ck_processing_jobs_job_type_valid "
        f"CHECK (job_type IN ({_quoted(NEW_JOB_TYPES)}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE processing_jobs DROP CONSTRAINT IF EXISTS ck_processing_jobs_job_type_valid")
    op.execute(
        f"ALTER TABLE processing_jobs ADD CONSTRAINT ck_processing_jobs_job_type_valid "
        f"CHECK (job_type IN ({_quoted(OLD_JOB_TYPES)}))"
    )
