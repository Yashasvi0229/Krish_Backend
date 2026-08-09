"""
Celery task — analyze one claim's emails + attachments with AI, then
build a draft invoice.

Pipeline (each step publishes progress):
    1. Load the claim + emails + attachments
    2. For each email → analyze_email (AI) [cache hit → skip]
    3. For each attachment → analyze_attachment (AI)
    4. For each billable analysis → billing engine → line item
    5. Build InvoiceDraft with all lines
    6. Publish COMPLETED with draft_id in result

Cost + resilience considerations:
    * We call AI only when analysis doesn't exist for the (input_hash).
      A rerun on a claim that's already analyzed costs $0 — only new
      emails/attachments trigger fresh calls.
    * Any single failed analysis becomes a placeholder row and is
      flagged; it does NOT crash the job.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.celery_app import celery_app
from app.core.constants import ClaimStatus
from app.core.logging import get_logger
from app.database import AsyncSessionLocal
from app.models.attachment import Attachment
from app.models.claim import Claim
from app.models.client import Client
from app.models.email import Email
from app.repositories import (
    ai_analysis_repo,
    claim_repo,
    draft_repo,
    job_repo,
)
from app.services import ai_service, billing_service, job_service
from app.services.billing_service import ComputedLine, compute_hours
from app.utils.file_storage import storage  # noqa: F401  (indirect use via ai_service)

log = get_logger(__name__)


# Default hourly rate when a client has none configured (per spec §16.3 —
# clients have `rate_config.hourly_rate`; if missing, fall back to a
# sensible default so we never produce a $0 invoice by accident).
DEFAULT_HOURLY_RATE = Decimal("150.00")


# ===========================================================================
# Celery entry point
# ===========================================================================
@celery_app.task(name="ai.analyze_claim", bind=True)
def analyze_claim(self, job_id: str) -> dict[str, Any]:
    return asyncio.run(_analyze_claim_async(job_id, celery_task_id=self.request.id))


# ===========================================================================
# Async implementation
# ===========================================================================
async def _analyze_claim_async(
    job_id_str: str, celery_task_id: str | None = None,
) -> dict[str, Any]:
    job_id = uuid.UUID(job_id_str)

    stats: dict[str, Any] = {
        "emails_analyzed": 0,
        "attachments_analyzed": 0,
        "cached_hits": 0,
        "ai_calls": 0,
        "billable_lines": 0,
        "flagged_lines": 0,
        "total_cost_usd": 0.0,
    }

    async with AsyncSessionLocal() as session:
        job = await job_repo.get_by_id(session, job_id)
        if job is None:
            log.error("job_not_found", job_id=job_id_str)
            return {"error": "job not found"}

        await job_repo.mark_started(session, job_id, celery_task_id=celery_task_id)
        await session.commit()

        claim_id = uuid.UUID(job.input_params["claim_id"])
        force_refresh = bool(job.input_params.get("force_refresh", False))

    await job_service.publish_progress(
        job_id, status="PROCESSING", progress=1, step_index=0,
        step_name="Loading claim data", stats=stats,
    )

    try:
        # ---- Load ----
        async with AsyncSessionLocal() as session:
            claim = await claim_repo.get_by_id(session, claim_id)
            if claim is None:
                await _fail(job_id, f"Claim {claim_id} not found")
                return {"error": "claim not found"}

            client = await session.get(Client, claim.client_id)
            emails = (await session.execute(
                select(Email).where(Email.claim_id == claim_id).order_by(Email.date)
            )).scalars().all()

            attachments: list[Attachment] = []
            for e in emails:
                atts = (await session.execute(
                    select(Attachment).where(Attachment.email_id == e.id)
                )).scalars().all()
                attachments.extend(atts)

        total_units = len(emails) + len(attachments)
        if total_units == 0:
            await _fail(job_id, "No emails or attachments found for this claim")
            return {"error": "no data"}

        await _progress(job_id, 5, 1, "Analyzing emails", stats)

        # ---- Analyze emails ----
        email_analyses: dict[uuid.UUID, Any] = {}
        for i, email in enumerate(emails):
            async with AsyncSessionLocal() as session:
                # Re-attach the email into this session
                em = await session.get(Email, email.id)
                if em is None:
                    continue
                analysis, was_cached = await ai_service.analyze_email(
                    session, em,
                    claim_no=claim.claim_no,
                    file_name=claim.file_name,
                    gnc_file_no=claim.gnc_file_no,
                    force_refresh=force_refresh,
                )
                await session.commit()
                email_analyses[email.id] = analysis
            stats["emails_analyzed"] += 1
            _accumulate(stats, analysis, was_cached)
            pct = 5 + (i + 1) / max(total_units, 1) * 60
            await _progress(job_id, pct, 1,
                            f"Analyzing emails ({i + 1}/{len(emails)})", stats)

        # ---- Analyze attachments ----
        attachment_analyses: dict[uuid.UUID, Any] = {}
        for i, att in enumerate(attachments):
            async with AsyncSessionLocal() as session:
                a = await session.get(Attachment, att.id)
                if a is None:
                    continue
                analysis, was_cached = await ai_service.analyze_attachment(
                    session, a,
                    claim_no=claim.claim_no,
                    file_name=claim.file_name,
                    gnc_file_no=claim.gnc_file_no,
                    force_refresh=force_refresh,
                )
                await session.commit()
                attachment_analyses[att.id] = analysis
            stats["attachments_analyzed"] += 1
            _accumulate(stats, analysis, was_cached)
            pct = 65 + (i + 1) / max(len(attachments), 1) * 20
            await _progress(job_id, pct, 2,
                            f"Analyzing attachments ({i + 1}/{len(attachments)})", stats)

        # ---- Build line items via rules engine ----
        await _progress(job_id, 85, 3, "Applying billing rules", stats)

        line_items: list[dict[str, Any]] = []
        hourly_rate = _resolve_hourly_rate(client)
        line_no = 1

        # Emails first, in date order
        for email in emails:
            analysis = email_analyses.get(email.id)
            if analysis is None or not analysis.is_billable:
                continue
            raw = (analysis.raw_response or {}).get("parsed", {}) or {}
            qty_obj = raw.get("quantity") or {}
            qty = float(qty_obj.get("value") or 1.0)
            estimate = raw.get("estimate_amount_usd")
            buildings = raw.get("building_count")

            computed = compute_hours(
                analysis.rule_code or "",
                quantity=qty,
                estimate_amount_usd=estimate,
                building_count=buildings,
            )
            if computed is None:
                # AI hallucinated a code we don't know — flag it, don't include.
                stats["flagged_lines"] += 1
                continue

            line_items.append(_line_dict(
                line_no=line_no, computed=computed,
                description=analysis.invoice_description or computed.description,
                rate=float(hourly_rate),
                source_email_id=email.id,
                source_attachment_id=None,
                confidence=analysis.confidence,
                reasoning=analysis.reasoning,
                is_flagged=analysis.should_flag,
                flag_reason=analysis.flag_reason,
            ))
            line_no += 1
            stats["billable_lines"] += 1
            if analysis.should_flag:
                stats["flagged_lines"] += 1

        # Attachments next
        for att in attachments:
            analysis = attachment_analyses.get(att.id)
            if analysis is None or not analysis.is_billable:
                continue
            raw = (analysis.raw_response or {}).get("parsed", {}) or {}
            qty_obj = raw.get("quantity") or {}
            qty = float(qty_obj.get("value") or (att.page_count or 1))
            estimate = raw.get("estimate_amount_usd")
            buildings = raw.get("building_count")

            computed = compute_hours(
                analysis.rule_code or "",
                quantity=qty,
                estimate_amount_usd=estimate,
                building_count=buildings,
            )
            if computed is None:
                stats["flagged_lines"] += 1
                continue

            line_items.append(_line_dict(
                line_no=line_no, computed=computed,
                description=analysis.invoice_description or computed.description,
                rate=float(hourly_rate),
                source_email_id=None,
                source_attachment_id=att.id,
                confidence=analysis.confidence,
                reasoning=analysis.reasoning,
                is_flagged=analysis.should_flag,
                flag_reason=analysis.flag_reason,
            ))
            line_no += 1
            stats["billable_lines"] += 1
            if analysis.should_flag:
                stats["flagged_lines"] += 1

        # ---- Compute totals ----
        subtotal = sum(Decimal(str(x["total"])) for x in line_items) or Decimal("0")
        grand_total = subtotal   # no tax/discount at draft time

        # ---- Save draft ----
        await _progress(job_id, 92, 4, "Creating invoice draft", stats)

        async with AsyncSessionLocal() as session:
            from app.repositories import invoice_repo
            invoice_no = await invoice_repo.next_invoice_number(session)

            claim = await claim_repo.get_by_id(session, claim_id)
            client = await session.get(Client, claim.client_id)

            billing_start, billing_end = _billing_period(emails)

            draft = await draft_repo.create(
                session,
                claim_id=claim.id,
                client_id=client.id,
                job_id=job_id,
                created_by=None,
                invoice_no=invoice_no,
                invoice_date=date.today(),
                gnc_file_no=claim.gnc_file_no,
                client_details={
                    "name": client.name,
                    "company_legal_name": client.company_legal_name,
                    "email": client.email,
                    "phone": client.phone,
                    "address_line1": client.address_line1,
                },
                insured_details={
                    "insured_name": claim.insured_details.get("insured_name") if claim.insured_details else None,
                },
                loss_details={
                    "claim_no": claim.claim_no,
                    "file_name": claim.file_name,
                    "loss_type": claim.loss_type,
                    "date_of_loss": claim.date_of_loss.isoformat() if claim.date_of_loss else None,
                },
                line_items=line_items,
                billing_period_start=billing_start,
                billing_period_end=billing_end,
                subtotal=subtotal,
                grand_total=grand_total,
                currency=(client.rate_config or {}).get("currency", "CAD"),
                total_emails=len(emails),
                emails_reviewed=0,
            )
            await session.commit()
            draft_id = draft.id

        # ---- Done ----
        await _progress(job_id, 100, 4, "Complete", stats)

        async with AsyncSessionLocal() as session:
            await job_repo.mark_completed(session, job_id, {
                "stats": stats,
                "draft_id": str(draft_id),
                "line_count": len(line_items),
                "subtotal": float(subtotal),
            })
            await session.commit()
        await job_service.publish_completion(
            job_id, status="COMPLETED",
            result={"draft_id": str(draft_id), "stats": stats},
        )
        return {"draft_id": str(draft_id), "stats": stats}

    except Exception as exc:  # noqa: BLE001
        log.exception("analyze_claim_job_failed", job_id=job_id_str)
        await _fail(job_id, f"Analyze job failed: {exc}")
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _accumulate(stats: dict[str, Any], analysis, was_cached: bool) -> None:
    """Fold one analysis's cost + cache-hit status into running stats.

    was_cached is authoritative: it tells us whether the CURRENT call
    hit the network. Looking at stored `input_tokens` would be wrong —
    a cached row has the token count from its original creation.
    """
    if was_cached:
        stats["cached_hits"] += 1
    else:
        stats["ai_calls"] += 1
        # cost + tokens only accrue when we ACTUALLY called the API
        stats["total_cost_usd"] = round(
            stats["total_cost_usd"] + float(analysis.cost_usd or 0), 6
        )


def _resolve_hourly_rate(client) -> Decimal:
    if client is None or not client.rate_config:
        return DEFAULT_HOURLY_RATE
    raw = client.rate_config.get("hourly_rate")
    if raw is None:
        return DEFAULT_HOURLY_RATE
    try:
        return Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return DEFAULT_HOURLY_RATE


def _billing_period(emails) -> tuple[date, date]:
    if not emails:
        today = date.today()
        return today - timedelta(days=30), today
    dates = [e.date.date() for e in emails if e.date]
    if not dates:
        today = date.today()
        return today - timedelta(days=30), today
    return min(dates), max(dates)


def _line_dict(
    *, line_no: int, computed: ComputedLine, description: str, rate: float,
    source_email_id: uuid.UUID | None, source_attachment_id: uuid.UUID | None,
    confidence: str, reasoning: str | None,
    is_flagged: bool, flag_reason: str | None,
) -> dict[str, Any]:
    total = float(computed.hours) * rate
    return {
        "line_number": line_no,
        "description": description,
        "category": computed.category,
        "rule_code": computed.rule_code,
        "quantity": computed.quantity,
        "quantity_unit": computed.quantity_unit,
        "quantity_hours": float(computed.hours),
        "rate": rate,
        "total": round(total, 2),
        "source_email_id": str(source_email_id) if source_email_id else None,
        "source_attachment_id": str(source_attachment_id) if source_attachment_id else None,
        "ai_confidence": confidence,
        "ai_reasoning": reasoning,
        "hours_reasoning": computed.hours_reasoning,
        "is_flagged": is_flagged,
        "flag_reason": flag_reason,
        "hit_cap": computed.hit_cap,
        "manual_override": False,
    }


async def _progress(
    job_id: uuid.UUID, pct: float, step_index: int, step_name: str,
    stats: dict[str, Any],
) -> None:
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
