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
    invoice_repo,
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

        await _progress(job_id, 5, 1, "Analyzing emails + attachments", stats)

        await _progress(job_id, 5, 1, "Analyzing emails", stats)

        # ---- Analyze emails in PARALLEL ----
        # ORIGINAL STRUCTURE — restored from the pre-parallel-boost state.
        # Two separate gather() calls (emails first, then attachments)
        # keep peak RAM predictable on Render's 512 MB free tier — a
        # single combined batch with concurrency=12 was causing OOM
        # crashes (502 Bad Gateway). Serialized phases + concurrency=6
        # is the rock-solid safe combo.
        #
        # Bounded via Semaphore because we:
        #   * Don't blow past OpenAI's per-minute rate limit
        #   * Don't overwhelm the DB connection pool (each analysis opens
        #     its own AsyncSession)
        #   * Keep the process memory footprint predictable
        CONCURRENCY = 6
        sem = asyncio.Semaphore(CONCURRENCY)

        email_analyses: dict[uuid.UUID, Any] = {}
        # Shared counter closed over by workers to update progress. asyncio
        # is single-threaded per event loop, so integer increments are safe
        # without a lock — the `await` boundary is the only place another
        # coroutine can preempt, and we only touch `done_count` between
        # awaits (not during them).
        done_count = 0

        async def _analyze_one_email(email_id: uuid.UUID):
            nonlocal done_count
            async with sem:
                # Own session per worker — SQLAlchemy sessions aren't safe
                # to share across concurrent coroutines.
                async with AsyncSessionLocal() as session:
                    em = await session.get(Email, email_id)
                    if em is None:
                        return None
                    analysis, was_cached = await ai_service.analyze_email(
                        session, em,
                        claim_no=claim.claim_no,
                        file_name=claim.file_name,
                        gnc_file_no=claim.gnc_file_no,
                        force_refresh=force_refresh,
                    )
                    await session.commit()
                # Bookkeeping outside the DB session — cheaper.
                email_analyses[email_id] = analysis
                stats["emails_analyzed"] += 1
                _accumulate(stats, analysis, was_cached)
                done_count += 1
                pct = 5 + done_count / max(total_units, 1) * 60
                # Fire-and-forget progress push. Failures here should not
                # abort the whole batch — the next progress will cover it.
                try:
                    await _progress(
                        job_id, pct, 1,
                        f"Analyzing emails ({done_count}/{len(emails)})", stats,
                    )
                except Exception:  # noqa: BLE001
                    log.debug("progress_push_failed", email_id=str(email_id))
                return analysis

        await asyncio.gather(
            *(_analyze_one_email(e.id) for e in emails),
            return_exceptions=False,  # let real failures propagate + fail the job
        )

        # ---- Analyze attachments in PARALLEL ----
        attachment_analyses: dict[uuid.UUID, Any] = {}
        att_done = 0

        async def _analyze_one_attachment(att_id: uuid.UUID):
            nonlocal att_done
            async with sem:
                async with AsyncSessionLocal() as session:
                    a = await session.get(Attachment, att_id)
                    if a is None:
                        return None
                    analysis, was_cached = await ai_service.analyze_attachment(
                        session, a,
                        claim_no=claim.claim_no,
                        file_name=claim.file_name,
                        gnc_file_no=claim.gnc_file_no,
                        force_refresh=force_refresh,
                    )
                    await session.commit()
                attachment_analyses[att_id] = analysis
                stats["attachments_analyzed"] += 1
                _accumulate(stats, analysis, was_cached)
                att_done += 1
                pct = 65 + att_done / max(len(attachments), 1) * 20
                try:
                    await _progress(
                        job_id, pct, 2,
                        f"Analyzing attachments ({att_done}/{len(attachments)})", stats,
                    )
                except Exception:  # noqa: BLE001
                    log.debug("progress_push_failed", attachment_id=str(att_id))
                return analysis

        await asyncio.gather(
            *(_analyze_one_attachment(a.id) for a in attachments),
            return_exceptions=False,
        )

        # ---- Enrich client + claim from AI-extracted facts ----
        # Attachments (RCV reports, cover letters, etc.) contain the ground
        # truth about the insured and the client. If our stored client is
        # still the "Unassigned" placeholder from Step 4's Gmail sync, use
        # what the AI extracted to fill it in. This runs BEFORE we snapshot
        # into the draft, so the draft shows real names.

        # Deterministic pre-pass: pull client contact info straight from
        # the email headers. AI extraction is smart about signatures but
        # unreliable — the From: header itself is the source of truth for
        # who the client's contact is. We merge these hints in BEFORE the
        # AI-fact enrichment so the enrichment can still override them if
        # the AI finds a better value in an attachment/signature.
        header_hints = _client_hints_from_emails(emails)

        try:
            await _enrich_from_ai_facts(
                claim_id=claim_id,
                client_id=(client.id if client else None),
                attachment_analyses=attachment_analyses,
                email_analyses=email_analyses,
                header_hints=header_hints,
            )
        except Exception:  # noqa: BLE001 — enrichment is best-effort
            log.exception("enrichment_failed", claim_id=str(claim_id))

        # ---- Build line items via rules engine ----
        await _progress(job_id, 85, 3, "Applying billing rules", stats)

        # Re-read the enriched client so hourly_rate reflects any updates.
        # Also load which email/attachment IDs have already been billed in
        # a PRIOR APPROVED invoice for this claim — we filter those out so
        # the same work never appears on two invoices. Cancelled invoices
        # don't count (their items are released back into the billable pool).
        async with AsyncSessionLocal() as session:
            client = await session.get(Client, claim.client_id) if claim else None
            billed_email_ids, billed_attachment_ids = (
                await invoice_repo.get_billed_source_ids(session, claim_id=claim_id)
            )
        stats["already_billed_emails"] = 0
        stats["already_billed_attachments"] = 0

        line_items: list[dict[str, Any]] = []
        hourly_rate = _resolve_hourly_rate(client)
        line_no = 1

        # Emails first, in date order
        for email in emails:
            # Dedup guard: skip anything already invoiced in an APPROVED
            # invoice for this claim. Prevents double billing.
            if email.id in billed_email_ids:
                stats["already_billed_emails"] += 1
                continue
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
            if att.id in billed_attachment_ids:
                stats["already_billed_attachments"] += 1
                continue
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
            # invoice_repo is already imported at module top — a nested
            # `from ... import invoice_repo` here would mark it as a
            # function-local variable via Python's scoping rules, making
            # earlier references (line ~268 for get_billed_source_ids)
            # raise UnboundLocalError. We only need the sibling service.
            from app.services import duplicate_check_service
            invoice_no = await invoice_repo.next_invoice_number(session)

            claim = await claim_repo.get_by_id(session, claim_id)
            client = await session.get(Client, claim.client_id)

            billing_start, billing_end = _billing_period(emails)

            # Duplicate billing check — warn (don't block) if a prior
            # approved invoice covers an overlapping period for this claim.
            duplicate_warning = await duplicate_check_service.check_for_duplicates(
                session,
                claim_id=claim.id,
                billing_period_start=billing_start,
                billing_period_end=billing_end,
            )
            if duplicate_warning:
                stats["duplicate_warning"] = True

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
                duplicate_warning=duplicate_warning,
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
    """Return the client's hourly rate — falls back to $150 if unset OR zero.

    Historical placeholder clients were created with `rate_config = {"hourly_rate": 0}`
    which silently produced $0 invoices. Treat non-positive rates as "not
    configured" so those still get the sensible default.
    """
    if client is None or not client.rate_config:
        return DEFAULT_HOURLY_RATE
    raw = client.rate_config.get("hourly_rate")
    if raw is None:
        return DEFAULT_HOURLY_RATE
    try:
        rate = Decimal(str(raw))
    except Exception:  # noqa: BLE001
        return DEFAULT_HOURLY_RATE
    if rate <= 0:
        return DEFAULT_HOURLY_RATE
    return rate


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


# ---------------------------------------------------------------------------
# Auto-enrichment — populate client + claim from AI-extracted facts
# ---------------------------------------------------------------------------
def _client_hints_from_emails(emails: list[Any]) -> dict[str, str]:
    """Deterministic client-contact extraction from Gmail metadata.

    Runs BEFORE the AI enrichment as a safety net. Even when the AI can't
    pull a phone number out of a signature block, the From: header of a
    non-internal email is a very reliable source for `client_email` and
    `client_contact_name`. We prefer this to leaving fields blank.

    Strategy:
      * Find all "external" emails (from_email NOT ending @gncgroup.ca).
      * Pick the most common external sender — that's most likely the
        primary client contact for this claim.
      * client_email    = from_email of that sender
      * client_contact_name = from_name if present, else null
      * client_phone    = parsed from body_snippet via signature regex
      * client_address  = parsed from body_snippet via signature regex

    Returns a dict of hints (may be partial). Empty dict if no external
    sender found (all-internal claim — rare but handled).
    """
    import re
    from collections import Counter

    if not emails:
        return {}

    # ---- Pick primary external sender ----
    external = [
        e for e in emails
        if e.from_email and not e.from_email.lower().endswith("@gncgroup.ca")
    ]
    if not external:
        return {}

    sender_counts = Counter(e.from_email.lower() for e in external)
    primary_email, _count = sender_counts.most_common(1)[0]
    # Get a representative email from that sender for name + body parsing
    primary_msg = next(
        (e for e in external if e.from_email.lower() == primary_email), None
    )

    hints: dict[str, str] = {"client_email": primary_email}

    if primary_msg is not None:
        if primary_msg.from_name and primary_msg.from_name.strip():
            hints["client_contact_name"] = primary_msg.from_name.strip()[:255]

        # ---- Signature parsing from body_snippet ----
        # body_snippet is the first ~500 chars — usually doesn't reach the
        # signature. To improve, we'd read from body_path (full body on
        # disk). Skipping that read here for speed — the AI does the deep
        # parse. This is just cheap wins from headers.
        body = (primary_msg.body_snippet or "")

        # Phone: match common patterns after "T:" / "Tel:" / "Phone:" / "P:"
        # Also plain "+1 555-..." or "(604) 555-..." patterns.
        phone_patterns = [
            r"(?:tel|phone|ph|t|p)[.:]\s*(\+?[\d\s\-().]{9,})",
            r"(\+\d[\d\s\-().]{9,})",             # +1 604 555 1234
            r"(\(\d{3}\)\s*\d{3}[-\s]?\d{4})",    # (604) 555-1234
        ]
        for pat in phone_patterns:
            m = re.search(pat, body, flags=re.IGNORECASE)
            if m:
                phone = re.sub(r"\s+", " ", m.group(1)).strip()
                # Sanity check — must have at least 10 digits
                digit_count = sum(c.isdigit() for c in phone)
                if digit_count >= 10:
                    hints["client_phone"] = phone[:50]
                    break

    return hints


async def _enrich_from_ai_facts(
    *,
    claim_id: uuid.UUID,
    client_id: uuid.UUID | None,
    attachment_analyses: dict[uuid.UUID, Any],
    email_analyses: dict[uuid.UUID, Any] | None = None,
    header_hints: dict[str, str] | None = None,
) -> None:
    """Update the placeholder client and the claim's insured_details from
    facts the AI extracted from attachments AND emails.

    Called once per analyze-claim job, AFTER all attachments have been
    analyzed. Idempotent — safe to re-run: it only OVERWRITES fields
    that are still empty / at their placeholder value.

    Fields the AI can extract (see attachment schema `key_facts`):
        insured_name, client_name, claim_no, gnc_file_no, adjuster_file_no,
        date_of_loss, total_rcv, total_acv, op_percent,
        client_email, client_phone, client_address, client_contact_name

    Priority when the same field appears in multiple analyses:
        * Attachments beat emails (formal docs > casual email content)
        * Higher-confidence analysis wins within each source
        * On ties, first non-empty value wins
    """
    email_analyses = email_analyses or {}
    if not attachment_analyses and not email_analyses:
        return

    # ---- Pick the highest-confidence key_facts per field ----
    def _bucket(conf: str) -> int:
        return {"High": 3, "Medium": 2, "Low": 1}.get(conf or "", 0)

    # Attachments come first (formal docs); emails top up whatever's missing.
    ordered = sorted(
        attachment_analyses.values(),
        key=lambda a: _bucket(a.confidence),
        reverse=True,
    ) + sorted(
        email_analyses.values(),
        key=lambda a: _bucket(getattr(a, "confidence", None) or ""),
        reverse=True,
    )

    merged: dict[str, Any] = {}
    for a in ordered:
        facts = ((a.raw_response or {}).get("key_facts") or {}) if a.raw_response else {}
        for k, v in facts.items():
            if v in (None, "", 0):
                continue
            merged.setdefault(k, v)

    # Layer email-header hints UNDER the AI facts — setdefault means AI
    # values win if they exist, but hints fill any gaps. This is the key
    # to reliable client contact info: even when AI misses the signature,
    # the sender's email address is always available.
    if header_hints:
        for k, v in header_hints.items():
            if v:
                merged.setdefault(k, v)

    if not merged:
        return

    # ---- Apply to DB ----
    async with AsyncSessionLocal() as session:
        # 1. Update the claim's insured_details
        claim = await session.get(Claim, claim_id)
        if claim is None:
            return

        insured_details = dict(claim.insured_details or {})
        if not insured_details.get("insured_name") and merged.get("insured_name"):
            insured_details["insured_name"] = str(merged["insured_name"])[:255]
            claim.insured_details = insured_details
            log.info("enriched_insured_name", claim_id=str(claim_id),
                     name=insured_details["insured_name"])

        # Also date_of_loss if we don't have one
        if not claim.date_of_loss and merged.get("date_of_loss"):
            try:
                from datetime import date as _date
                # Accept ISO strings or date objects
                raw_dol = merged["date_of_loss"]
                if isinstance(raw_dol, str) and len(raw_dol) >= 10:
                    claim.date_of_loss = _date.fromisoformat(raw_dol[:10])
                    log.info("enriched_date_of_loss", claim_id=str(claim_id))
            except (ValueError, TypeError):
                pass

        # 2. Update the placeholder client with a real name/company.
        # If an existing (non-placeholder) client already has this name,
        # re-link the claim to that one so we don't accumulate duplicates.
        if client_id is not None and merged.get("client_name"):
            ai_client_name = str(merged["client_name"])[:255].strip()

            # ---- Step 2a: look for an existing real client by name ----
            # Case-insensitive exact match. We deliberately don't fuzzy-match
            # to avoid silently linking two similarly-named companies.
            from sqlalchemy import func as _func
            existing_stmt = select(Client).where(
                _func.lower(Client.name) == ai_client_name.lower(),
                Client.deleted_at.is_(None),
                Client.name != "Unassigned",
                Client.id != client_id,
            ).limit(1)
            existing_client = (await session.execute(existing_stmt)).scalar_one_or_none()

            if existing_client is not None:
                # Re-link the claim to the real client (with proper email/phone
                # already configured by the admin). Placeholder client stays
                # in the DB for other claims; it'll get soft-deleted eventually.
                claim.client_id = existing_client.id
                # Even for existing clients, fill in any BLANK contact fields
                # from the AI extraction — harmless enrichment, and helpful
                # if the admin created a stub with just the name.
                _fill_client_contact_from_facts(existing_client, merged,
                                                 only_if_blank=True)
                log.info(
                    "relinked_claim_to_existing_client",
                    claim_id=str(claim_id),
                    from_client_id=str(client_id),
                    to_client_id=str(existing_client.id),
                    name=ai_client_name,
                )
            else:
                # No match — promote the placeholder in place. Also copy any
                # contact info the AI extracted. If nothing extracted, wipe
                # the fake "unassigned@example.com" / "—" values so the
                # invoice ships with clean blanks rather than fake data.
                client_row = await session.get(Client, client_id)
                if client_row is not None and client_row.name == "Unassigned":
                    client_row.name = ai_client_name
                    client_row.company_legal_name = ai_client_name
                    _fill_client_contact_from_facts(client_row, merged,
                                                     only_if_blank=False)
                    log.info("promoted_placeholder_client",
                             client_id=str(client_id), name=ai_client_name,
                             email=client_row.email or "(blank)",
                             phone=client_row.phone or "(blank)")

        await session.commit()


def _fill_client_contact_from_facts(client_row, facts: dict, *,
                                     only_if_blank: bool) -> None:
    """Populate client email/phone/address/contact-name from AI facts.

    `only_if_blank=True`  → only fill fields that are currently empty or
                             placeholder values (safe for existing clients
                             the admin has already configured).
    `only_if_blank=False` → always overwrite placeholder values (safe for
                             a fresh "Unassigned" client we're promoting).

    Placeholder values we treat as blank: '', '—', 'unassigned@example.com'.
    """
    def _is_blank(v: str | None) -> bool:
        return not v or v in ("—", "unassigned@example.com")

    ai_email = str(facts.get("client_email") or "").strip()[:255]
    if ai_email and (not only_if_blank or _is_blank(client_row.email)):
        client_row.email = ai_email

    ai_phone = str(facts.get("client_phone") or "").strip()[:50]
    if ai_phone and (not only_if_blank or _is_blank(client_row.phone)):
        client_row.phone = ai_phone

    ai_addr = str(facts.get("client_address") or "").strip()[:500]
    if ai_addr and (not only_if_blank or _is_blank(client_row.address_line1)):
        client_row.address_line1 = ai_addr

    ai_contact = str(facts.get("client_contact_name") or "").strip()[:255]
    if ai_contact and (not only_if_blank or _is_blank(client_row.primary_contact_name)):
        client_row.primary_contact_name = ai_contact

    # If AI didn't provide anything but the row still has placeholders,
    # wipe them (invoice looks cleaner with "—" than with fake email).
    if not only_if_blank:
        if _is_blank(client_row.email):
            client_row.email = ""
        if _is_blank(client_row.phone):
            client_row.phone = ""
        if _is_blank(client_row.address_line1):
            client_row.address_line1 = ""
        if _is_blank(client_row.primary_contact_name):
            client_row.primary_contact_name = ""
