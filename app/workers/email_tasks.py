"""
Celery tasks for Gmail fetching and processing.

Design note — sync-in-async:
    Celery workers are sync. But our services use SQLAlchemy async. We
    bridge by calling `asyncio.run(_async_impl())` at the top of each task.
    That gives Celery a plain sync entry point while letting the actual
    work stay async.

Design note — session scoping:
    Each task creates its own session using `AsyncSessionLocal`. We do NOT
    reuse the FastAPI request session (it doesn't exist here). Sessions
    are short-lived and closed even if the task raises.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.celery_app import celery_app
from app.core.constants import ClientType, RateStrategy
from app.core.logging import get_logger
from app.database import AsyncSessionLocal
from app.integrations.gmail_client import GmailClient
from app.models.client import Client
from app.repositories import (
    attachment_repo,
    claim_repo,
    email_repo,
    job_repo,
)
from app.services import job_service
from app.services.attachment_service import download_and_process
from app.services.gmail_service import (
    build_gmail_client,
    parse_gmail_message,
    persist_refreshed_tokens,
)
from app.utils.file_storage import storage
from app.utils.gmail_query import build_search_query
from app.utils.hashing import content_hash as compute_content_hash

log = get_logger(__name__)


# ===========================================================================
# TASK 1 — full search-and-fetch for on-demand invoice generation
# ===========================================================================
@celery_app.task(name="email.process_gmail_search", bind=True)
def process_gmail_search(self, job_id: str) -> dict[str, Any]:
    """
    Given a ProcessingJob ID, run the full search-and-fetch pipeline:
        1. Read job.input_params (claim_no / file_name / gnc_file_no / client_id)
        2. Search Gmail
        3. Fetch each message, parse, upsert into `emails`
        4. Download every attachment, extract text
        5. Link emails to a Claim (find_or_create)
        6. Mark job COMPLETED, publish completion event

    Progress is published to Redis pub/sub throughout so the frontend
    WebSocket sees it live.
    """
    return asyncio.run(_process_gmail_search_async(job_id, celery_task_id=self.request.id))


async def _process_gmail_search_async(
    job_id_str: str, celery_task_id: str | None = None
) -> dict[str, Any]:
    job_id = uuid.UUID(job_id_str)

    stats: dict[str, Any] = {
        "emails_found": 0,
        "emails_new": 0,
        "emails_dedup_skipped": 0,
        "attachments_downloaded": 0,
        "attachments_dedup_skipped": 0,
        "internal_emails": 0,
    }

    # ---- Load job + input params ------------------------------------------
    async with AsyncSessionLocal() as session:
        job = await job_repo.get_by_id(session, job_id)
        if job is None:
            log.error("job_not_found", job_id=job_id_str)
            return {"error": "job not found"}

        await job_repo.mark_started(session, job_id, celery_task_id=celery_task_id)
        await session.commit()

        params = dict(job.input_params or {})
        claim_no = params.get("claim_no")
        file_name = params.get("file_name")
        gnc_file_no = params.get("gnc_file_no")
        client_id = uuid.UUID(params["client_id"]) if params.get("client_id") else None

    await job_service.publish_progress(
        job_id, status="PROCESSING", progress=1, step_index=0,
        step_name="Searching Gmail", stats=stats,
    )

    try:
        query = build_search_query(
            claim_no=claim_no, file_name=file_name, gnc_file_no=gnc_file_no,
        )
    except ValueError as exc:
        await _fail(job_id, str(exc))
        return {"error": str(exc)}

    # ---- Build Gmail client (loads + decrypts creds) ---------------------
    async with AsyncSessionLocal() as session:
        try:
            gmail = await build_gmail_client(session)
        except Exception as exc:  # noqa: BLE001
            await _fail(job_id, f"Gmail client build failed: {exc}")
            return {"error": str(exc)}

    try:
        # ---- Step 1: search Gmail -------------------------------------
        message_ids = await gmail.list_message_ids(query=query, max_results=200)
        stats["emails_found"] = len(message_ids)
        await _progress(job_id, 10, 0, "Searching Gmail", stats)

        if not message_ids:
            async with AsyncSessionLocal() as session:
                await job_repo.mark_completed(session, job_id, {
                    "stats": stats, "message": "No emails matched the search."
                })
                await session.commit()
            await job_service.publish_completion(
                job_id, status="COMPLETED",
                result={"stats": stats, "created_email_ids": []},
            )
            return {"stats": stats}

        # ---- Step 2: fetch every message + parse ----------------------
        parsed_batch: list[Any] = []
        raw_attachments_by_gmail_id: dict[str, list[dict[str, Any]]] = {}
        total = len(message_ids)

        for i, mid in enumerate(message_ids):
            try:
                raw = await gmail.get_message(mid, fmt="full")
                parsed = parse_gmail_message(raw)
                parsed_batch.append(parsed)
                raw_attachments_by_gmail_id[parsed.gmail_message_id] = parsed.raw_attachments
                if parsed.is_internal:
                    stats["internal_emails"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad email doesn't kill the job
                log.warning("email_fetch_failed", gmail_id=mid, error=str(exc))
                continue
            if (i + 1) % 5 == 0 or i == total - 1:
                pct = 10 + (i + 1) / total * 30
                await _progress(job_id, pct, 1, f"Fetching emails ({i + 1}/{total})", stats)

        # ---- Step 3: dedupe + insert emails into DB -------------------
        async with AsyncSessionLocal() as session:
            # Find or create the claim to anchor these emails to.
            claim = await _find_or_create_claim(
                session, claim_no=claim_no, file_name=file_name,
                gnc_file_no=gnc_file_no, client_id=client_id,
            )

            rows = []
            for p in parsed_batch:
                body_path = storage.email_body_path(p.gmail_message_id, p.date)
                storage.write_text(body_path, p.body_text)
                rows.append({
                    "gmail_message_id": p.gmail_message_id,
                    "gmail_thread_id": p.gmail_thread_id,
                    "gmail_link": f"https://mail.google.com/mail/u/0/#inbox/{p.gmail_message_id}",
                    "claim_id": claim.id,
                    "content_hash": compute_content_hash(
                        p.subject, p.body_text, p.from_email, p.date
                    ),
                    "subject": p.subject[:5000],
                    "from_email": p.from_email[:255],
                    "from_name": p.from_name[:255],
                    "to_emails": p.to_emails,
                    "cc_emails": p.cc_emails,
                    "date": p.date,
                    "body_path": body_path,
                    "body_snippet": p.body_snippet[:2000],
                    "is_internal": p.is_internal,
                })

            inserted_ids = await email_repo.bulk_upsert_by_gmail_id(session, rows)
            stats["emails_new"] = len(inserted_ids)
            stats["emails_dedup_skipped"] = len(rows) - len(inserted_ids)
            await session.commit()

        await _progress(job_id, 45, 2, "Downloading attachments", stats)

        # ---- Step 4: download attachments + extract text --------------
        # We look up the actual email IDs (including those already existing)
        # so attachments still get linked to the right rows.
        async with AsyncSessionLocal() as session:
            email_by_gmail_id: dict[str, uuid.UUID] = {}
            for p in parsed_batch:
                existing = await email_repo.get_by_gmail_message_id(
                    session, p.gmail_message_id
                )
                if existing:
                    email_by_gmail_id[p.gmail_message_id] = existing.id

        total_atts = sum(
            len(raw_attachments_by_gmail_id.get(gid, []))
            for gid in email_by_gmail_id
        )
        processed_atts = 0

        for gid, email_id in email_by_gmail_id.items():
            atts_meta = raw_attachments_by_gmail_id.get(gid, [])
            if not atts_meta:
                continue

            # Skip if this email already had attachments processed
            async with AsyncSessionLocal() as session:
                existing_atts = await attachment_repo.list_by_email(session, email_id)
                if existing_atts and len(existing_atts) >= len(atts_meta):
                    processed_atts += len(atts_meta)
                    continue

            async with AsyncSessionLocal() as session:
                for att_meta in atts_meta:
                    was_dedup = False
                    try:
                        existing_hash_row = None
                        # Check if we'll dedup (peek before download only possible after we have bytes)
                        att_id = await download_and_process(
                            session, gmail=gmail, email_id=email_id,
                            gmail_message_id=gid, attachment_meta=att_meta,
                        )
                        if att_id:
                            stats["attachments_downloaded"] += 1
                    except Exception as exc:  # noqa: BLE001
                        log.warning("attachment_process_failed",
                                    filename=att_meta.get("filename"), error=str(exc))
                    processed_atts += 1
                    if processed_atts % 3 == 0 or processed_atts == total_atts:
                        pct = 45 + (processed_atts / max(total_atts, 1)) * 45
                        await _progress(
                            job_id, pct, 3,
                            f"Extracting text ({processed_atts}/{total_atts})",
                            stats,
                        )
                await session.commit()

        # ---- Step 5: finalize ---------------------------------------------
        await _progress(job_id, 95, 4, "Finalizing", stats)

        async with AsyncSessionLocal() as session:
            await job_repo.mark_completed(session, job_id, {
                "stats": stats,
                "claim_id": None,  # populated below
                "email_ids": [str(eid) for eid in email_by_gmail_id.values()],
            })
            # persist refreshed access token, if any
            await persist_refreshed_tokens(session, gmail)
            await session.commit()

        await job_service.publish_completion(
            job_id, status="COMPLETED",
            result={"stats": stats, "created_email_ids": [str(x) for x in email_by_gmail_id.values()]},
        )
        return {"stats": stats}

    except Exception as exc:  # noqa: BLE001 — always want to mark job failed
        log.exception("gmail_search_job_failed", job_id=job_id_str)
        await _fail(job_id, f"Job failed: {exc}")
        return {"error": str(exc)}
    finally:
        await gmail.aclose()


# ===========================================================================
# TASK 2 — periodic Gmail sync (metadata only)
# ===========================================================================
@celery_app.task(name="email.sync_recent_metadata")
def sync_recent_metadata() -> dict[str, Any]:
    """Beat-scheduled task: fetch last-N-days emails (metadata only) into DB.

    Doesn't download attachments; just gives us a searchable index.
    Full content + attachments come in the on-demand path.
    """
    return asyncio.run(_sync_recent_async())


async def _sync_recent_async() -> dict[str, Any]:
    from app.config import settings
    stats = {"emails_found": 0, "emails_new": 0}

    async with AsyncSessionLocal() as session:
        try:
            gmail = await build_gmail_client(session)
        except Exception as exc:  # noqa: BLE001
            log.info("sync_skipped_no_gmail_connection", reason=str(exc))
            return stats

    try:
        query = f"newer_than:{settings.gmail_sync_lookback_days}d"
        message_ids = await gmail.list_message_ids(query=query, max_results=500)
        stats["emails_found"] = len(message_ids)

        rows: list[dict[str, Any]] = []
        for mid in message_ids:
            try:
                raw = await gmail.get_message(mid, fmt="metadata")
                p = parse_gmail_message(raw)
            except Exception as exc:  # noqa: BLE001
                log.warning("sync_email_fetch_failed", gmail_id=mid, error=str(exc))
                continue

            body_path = ""  # metadata-only, no body persisted
            rows.append({
                "gmail_message_id": p.gmail_message_id,
                "gmail_thread_id": p.gmail_thread_id,
                "gmail_link": f"https://mail.google.com/mail/u/0/#inbox/{p.gmail_message_id}",
                "content_hash": compute_content_hash(
                    p.subject, p.body_text or "", p.from_email, p.date,
                ),
                "subject": p.subject[:5000],
                "from_email": p.from_email[:255],
                "from_name": p.from_name[:255],
                "to_emails": p.to_emails,
                "cc_emails": p.cc_emails,
                "date": p.date,
                "body_path": body_path,
                "body_snippet": (p.body_snippet or "")[:2000],
                "is_internal": p.is_internal,
            })

        async with AsyncSessionLocal() as session:
            inserted = await email_repo.bulk_upsert_by_gmail_id(session, rows)
            stats["emails_new"] = len(inserted)
            await persist_refreshed_tokens(session, gmail)
            await session.commit()

        # Update `last_sync_at` on the singleton
        from app.repositories import gmail_connection_repo
        async with AsyncSessionLocal() as session:
            await gmail_connection_repo.upsert_singleton(
                session, last_sync_at=datetime.now(UTC),
            )
            await session.commit()

        return stats
    finally:
        await gmail.aclose()


# ===========================================================================
# Helpers
# ===========================================================================
async def _progress(
    job_id: uuid.UUID, pct: float, step_index: int, step_name: str,
    stats: dict[str, Any],
) -> None:
    """Update DB + publish to pub/sub in one shot."""
    async with AsyncSessionLocal() as session:
        await job_repo.update_progress(
            session, job_id, progress=pct,
            current_step_index=step_index,
            current_step_name=step_name,
            stats_delta=stats,
        )
        await session.commit()
    await job_service.publish_progress(
        job_id, status="PROCESSING",
        progress=pct, step_index=step_index, step_name=step_name, stats=stats,
    )


async def _fail(job_id: uuid.UUID, msg: str) -> None:
    async with AsyncSessionLocal() as session:
        await job_repo.mark_failed(session, job_id, msg)
        await session.commit()
    await job_service.publish_completion(job_id, status="FAILED", error=msg)


async def _find_or_create_claim(
    session, *, claim_no: str | None, file_name: str | None,
    gnc_file_no: str | None, client_id: uuid.UUID | None,
):
    """Locate an existing claim or create a stub one so emails have somewhere
    to hang. In Phase 1 we auto-create a placeholder Client if none provided
    (so single-tenant testing works out of the box).

    IMPORTANT — this function is idempotent by design: it can be safely
    called from a re-run of the same job. Previous versions had a race:
    if the job was retried after crashing mid-transaction, the claim from
    the first attempt already existed, but we skipped the `find_by_client_
    and_claim_no` check because `client_id` was None at that point (only
    resolved later, when we auto-provision the placeholder). That would
    trigger a UniqueViolationError on the (client_id, claim_no) uq index.

    The fix: resolve client_id FIRST, then do the lookup, so we always
    match an existing row before attempting insert.
    """
    from sqlalchemy import select

    # ---- Step 1: try gnc_file_no lookup (globally unique, no client needed)
    if gnc_file_no:
        claim = await claim_repo.find_by_gnc_file_no(session, gnc_file_no)
        if claim:
            return claim

    # ---- Step 2: resolve client_id BEFORE the (client_id, claim_no) lookup
    # so a retry finds the existing claim instead of re-inserting.
    if client_id is None:
        stmt = select(Client).where(Client.name == "Unassigned").limit(1)
        result = await session.execute(stmt)
        placeholder = result.scalar_one_or_none()
        if placeholder is None:
            placeholder = Client(
                name="Unassigned",
                company_legal_name="Unassigned",
                client_type=ClientType.OTHER.value,
                primary_contact_name="—",
                email="unassigned@example.com",
                phone="—",
                address_line1="—",
                rate_strategy=RateStrategy.FLAT.value,
                # Default $150/hr so drafts against the placeholder still
                # produce non-zero totals. Admin can override per-client.
                rate_config={"hourly_rate": 150},
                template_path="",
            )
            session.add(placeholder)
            await session.flush()
        client_id = placeholder.id

    # ---- Step 3: NOW look up by (client_id, claim_no) — idempotent path
    if claim_no:
        claim = await claim_repo.find_by_client_and_claim_no(session, client_id, claim_no)
        if claim:
            return claim

    # ---- Step 4: fresh claim — synthesize any missing identifiers
    resolved_gnc = gnc_file_no or (claim_no or "auto") + "-" + uuid.uuid4().hex[:6]
    resolved_claim_no = claim_no or "auto-" + uuid.uuid4().hex[:6]
    resolved_file_name = file_name or resolved_claim_no

    return await claim_repo.create(
        session, client_id=client_id,
        gnc_file_no=resolved_gnc, claim_no=resolved_claim_no,
        file_name=resolved_file_name, loss_type="Unknown",
    )
