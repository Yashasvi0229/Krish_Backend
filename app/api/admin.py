"""
Administrative endpoints — DESTRUCTIVE operations.

    POST /api/admin/reset  → wipe test data (invoices, drafts, claims,
                             emails, attachments, AI analyses, jobs).

Every operation in this file:
    * Requires an authenticated admin
    * Requires an explicit `confirm=true` in the request body — no accidents
    * Runs in a single transaction — atomic (all-or-nothing)
    * Returns detailed counts of what was cleared

Preserved by default:
    * `clients`    — you probably want to keep the ones you set up.
                     Set `include_clients=true` to nuke those too.
    * `billing_rules` — seeded via migration, never cleared.
    * `users` / admin identity — never touched.
    * `gmail_connections` — Gmail OAuth tokens (re-authorizing is painful).
    * `billing_ledger`, `audit_log` — historical record kept for compliance.
                                       Set `include_history=true` to clear.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentAdmin, get_current_admin
from app.config import settings
from app.core.exceptions import ConflictError
from app.core.logging import get_logger
from app.database import get_db

log = get_logger(__name__)
router = APIRouter()


class ResetRequest(BaseModel):
    """POST /api/admin/reset body.

    `confirm` is required. `include_*` flags let the caller expand the
    blast radius from the default (safe) scope of test-data-only.
    """
    confirm: bool = Field(..., description="Must be true — safety check.")
    include_clients: bool = Field(
        default=False,
        description=(
            "Also delete client rows. Only turn this on if you're sure — "
            "clients you added manually will be lost."
        ),
    )
    include_history: bool = Field(
        default=False,
        description="Also clear billing_ledger and audit_log tables.",
    )
    include_files: bool = Field(
        default=True,
        description="Also delete files under STORAGE_ROOT (Excel + attachments).",
    )


@router.post("/reset")
async def reset_database(
    payload: ResetRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[CurrentAdmin, Depends(get_current_admin)],
) -> dict:
    """Wipe test data. See module docstring for what's cleared vs preserved."""
    if not payload.confirm:
        raise ConflictError(
            "Set `confirm: true` in the request body to proceed. "
            "This operation is destructive."
        )

    # Delete order matters — children first, parents last (FK constraints).
    # We use raw SQL for speed and to avoid loading rows into Python.
    delete_order = [
        # Analysis + billing traces first (reference invoice_drafts + emails)
        "ai_analyses",
        # Approved invoices reference drafts + claims
        "invoices",
        # Drafts reference claims + clients
        "invoice_drafts",
        # Emails + attachments reference claims
        "attachments",
        "emails",
        # Claims reference clients
        "claims",
        # Jobs are standalone — remove last
        "processing_jobs",
    ]
    if payload.include_history:
        # These reference invoices/drafts/emails — safe to delete after them.
        # Insert at the top so they run first (children of everything).
        delete_order = ["billing_ledger", "audit_logs"] + delete_order
    if payload.include_clients:
        delete_order.append("clients")

    cleared: dict[str, int] = {}
    for table in delete_order:
        # Verify the table actually exists before TRUNCATEing — otherwise
        # a rename or refactor upstream could silently swallow the error.
        exists = (await db.execute(
            text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table},
        )).scalar_one()
        if not exists:
            log.warning("reset_table_missing", table=table)
            continue
        count = (await db.execute(
            text(f"SELECT COUNT(*) FROM {table}")
        )).scalar_one()
        if count > 0:
            # Use DELETE (not TRUNCATE) so it participates in our
            # transaction and can be rolled back if anything downstream
            # fails. Slower on huge tables but our scale is tiny.
            await db.execute(text(f"DELETE FROM {table}"))
        cleared[table] = int(count)

    await db.commit()

    # Wipe on-disk files (Excel invoices, attachment downloads, etc.).
    # Done AFTER the DB commit so a DB failure doesn't leave orphaned files.
    files_deleted = 0
    if payload.include_files:
        root = Path(settings.storage_root).resolve()
        # Extra safety — refuse to delete anything that isn't clearly
        # inside a `storage`/`gnc_storage` directory. This is a paranoid
        # guard against a misconfigured STORAGE_ROOT pointing at $HOME.
        if root.exists() and any(
            seg in root.name.lower() for seg in ("storage", "gnc")
        ):
            for child in root.iterdir():
                try:
                    if child.is_dir():
                        files_deleted += sum(1 for _ in child.rglob("*") if _.is_file())
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                        files_deleted += 1
                except OSError as exc:
                    log.warning("reset_file_delete_failed",
                                path=str(child), error=str(exc))
        else:
            log.warning("reset_storage_root_looks_wrong",
                        root=str(root),
                        message="Refusing to delete files — root name doesn't look like a storage dir.")

    log.warning(
        "database_reset_executed",
        actor=admin.email,
        cleared=cleared,
        files_deleted=files_deleted,
        include_clients=payload.include_clients,
        include_history=payload.include_history,
    )

    return {
        "success": True,
        "cleared": cleared,
        "files_deleted": files_deleted,
        "preserved": [
            "billing_rules (25 seeded rules)",
            "users / admin",
            "gmail_connections",
            *(["clients"] if not payload.include_clients else []),
            *(["billing_ledger, audit_logs"] if not payload.include_history else []),
        ],
        "message": (
            f"Cleared {sum(cleared.values())} rows across "
            f"{len([k for k, v in cleared.items() if v > 0])} tables. "
            f"System is ready for fresh tests."
        ),
    }
