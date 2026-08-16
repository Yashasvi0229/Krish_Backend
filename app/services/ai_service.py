"""
AI service — the orchestrator that ties together prompt + OpenAI + cache.

Responsibilities:
    1. Compute the cache key (`input_hash`) for a given input
    2. Look up ai_analyses by that hash — HIT: return cached, don't call AI
    3. MISS: build the prompt, call OpenAI, validate response, persist
    4. Cost + confidence bucketing before returning

Every call funnels through `analyze_email` or `analyze_attachment` — no
other module speaks to OpenAI directly.
"""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.constants import AIProvider, AnalysisType, Confidence
from app.core.logging import get_logger
from app.integrations.openai_client import OpenAIClient, StructuredCompletion
from app.models.ai_analysis import AIAnalysis
from app.models.attachment import Attachment
from app.models.email import Email
from app.repositories import ai_analysis_repo
from app.schemas.ai_analysis import (
    AttachmentAnalysisOutput,
    EmailAnalysisOutput,
)
from app.services.prompt_service import (
    PROMPT_VERSION,
    AttachmentPromptContext,
    EmailPromptContext,
    SYSTEM_PROMPT,
    build_attachment_user_prompt,
    build_email_user_prompt,
)
from app.utils.file_storage import storage

log = get_logger(__name__)


# Truncation limits — matched to `ai_max_input_tokens` in config.
# 1 token ≈ 4 chars for English; we leave headroom for the system prompt.
MAX_EMAIL_BODY_CHARS = 20_000       # ~5K tokens
MAX_ATTACHMENT_TEXT_CHARS = 60_000  # ~15K tokens


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------
def _compute_input_hash(
    *,
    subject: str,
    body: str,
    from_email: str,
    prompt_version: str,
    model: str,
) -> str:
    """SHA-256 of a stable canonical form. Two callers with the same input
    get the same hash; a prompt version bump automatically invalidates."""
    h = hashlib.sha256()
    for part in (subject, body, from_email, prompt_version, model):
        h.update(b"\x00")
        h.update((part or "").encode("utf-8"))
    return h.hexdigest()


def _compute_attachment_hash(
    *,
    file_hash: str,
    extracted_text: str,
    prompt_version: str,
    model: str,
) -> str:
    """Cache key for attachments — file_hash + extracted_text + prompt +
    model. If we re-extract the text later (e.g. we added OCR), the hash
    changes and we re-analyze."""
    h = hashlib.sha256()
    for part in (file_hash, extracted_text[:MAX_ATTACHMENT_TEXT_CHARS],
                 prompt_version, model):
        h.update(b"\x00")
        h.update(part.encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Public API — analyze_email
# ---------------------------------------------------------------------------
async def analyze_email(
    session: AsyncSession,
    email: Email,
    *,
    claim_no: str | None = None,
    file_name: str | None = None,
    gnc_file_no: str | None = None,
    force_refresh: bool = False,
) -> tuple[AIAnalysis, bool]:
    """Analyze one email. Returns (analysis, was_cached).

    was_cached=True means we reused a stored row and did NOT hit the AI.
    Callers use this to count real API spend vs cache reuse.
    """
    body = _load_body(email)
    input_hash = _compute_input_hash(
        subject=email.subject or "",
        body=body[:MAX_EMAIL_BODY_CHARS],
        from_email=email.from_email or "",
        prompt_version=PROMPT_VERSION,
        model=settings.ai_model_primary,
    )

    if not force_refresh:
        cached = await ai_analysis_repo.get_by_input_hash(session, input_hash)
        if cached:
            log.info("ai_email_cache_hit", email_id=str(email.id))
            return cached, True

    # Attachment filenames — query explicitly (relationship is lazy in async).
    from sqlalchemy import select as _select
    from app.models.attachment import Attachment as _Att
    att_rows = (await session.execute(
        _select(_Att.filename).where(_Att.email_id == email.id)
    )).all()
    attachment_filenames = [r[0] for r in att_rows]

    # ---- Build prompt ----
    ctx = EmailPromptContext(
        subject=email.subject or "",
        from_email=email.from_email or "",
        from_name=email.from_name or "",
        to_emails=list(email.to_emails or []),
        cc_emails=list(email.cc_emails or []),
        date_iso=email.date.isoformat() if email.date else "",
        body_text=body[:MAX_EMAIL_BODY_CHARS],
        is_internal=bool(email.is_internal),
        attachment_filenames=attachment_filenames,
        claim_no=claim_no,
        file_name=file_name,
        gnc_file_no=gnc_file_no,
    )
    user_prompt = build_email_user_prompt(ctx)

    # ---- Call AI ----
    client = OpenAIClient()
    try:
        result: StructuredCompletion = await client.call_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=EmailAnalysisOutput,
            response_name="email_analysis",
            prompt_version=PROMPT_VERSION,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("ai_email_analysis_failed", email_id=str(email.id))
        # Persist a placeholder failure so the reviewer sees "we tried but
        # something went wrong" instead of a silent missing analysis.
        failed = await _save_failed(
            session, email_id=email.id, attachment_id=None,
            analysis_type=AnalysisType.EMAIL_CLASSIFY,
            input_hash=input_hash, error=str(exc),
        )
        return failed, False

    parsed: EmailAnalysisOutput = result.parsed
    saved = await _save_email_success(
        session, email=email, input_hash=input_hash,
        result=result, parsed=parsed,
    )
    return saved, False


# ---------------------------------------------------------------------------
# Public API — analyze_attachment
# ---------------------------------------------------------------------------
async def analyze_attachment(
    session: AsyncSession,
    attachment: Attachment,
    *,
    claim_no: str | None = None,
    file_name: str | None = None,
    gnc_file_no: str | None = None,
    force_refresh: bool = False,
) -> tuple[AIAnalysis, bool]:
    """Analyze one attachment. Returns (analysis, was_cached)."""
    extracted = _load_attachment_text(attachment)
    input_hash = _compute_attachment_hash(
        file_hash=attachment.file_hash,
        extracted_text=extracted,
        prompt_version=PROMPT_VERSION,
        model=settings.ai_model_primary,
    )

    if not force_refresh:
        cached = await ai_analysis_repo.get_by_input_hash(session, input_hash)
        if cached:
            log.info("ai_attachment_cache_hit", attachment_id=str(attachment.id))
            return cached, True

    ctx = AttachmentPromptContext(
        filename=attachment.filename or "",
        file_extension=attachment.file_extension or "",
        page_count=attachment.page_count,
        file_size_bytes=attachment.file_size or 0,
        document_type_hint=attachment.document_type,
        extracted_text=extracted[:MAX_ATTACHMENT_TEXT_CHARS],
        claim_no=claim_no,
        file_name=file_name,
        gnc_file_no=gnc_file_no,
    )
    user_prompt = build_attachment_user_prompt(ctx)

    client = OpenAIClient()
    try:
        result = await client.call_structured(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
            response_model=AttachmentAnalysisOutput,
            response_name="attachment_analysis",
            prompt_version=PROMPT_VERSION,
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("ai_attachment_analysis_failed",
                      attachment_id=str(attachment.id))
        failed = await _save_failed(
            session, email_id=None, attachment_id=attachment.id,
            analysis_type=AnalysisType.ATTACHMENT_SUMMARY,
            input_hash=input_hash, error=str(exc),
        )
        return failed, False

    parsed: AttachmentAnalysisOutput = result.parsed
    saved = await _save_attachment_success(
        session, attachment=attachment, input_hash=input_hash,
        result=result, parsed=parsed,
    )
    return saved, False


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _confidence_bucket(score: int) -> str:
    if score >= 85:
        return Confidence.HIGH.value
    if score >= 60:
        return Confidence.MEDIUM.value
    return Confidence.LOW.value


async def _save_email_success(
    session, *, email: Email, input_hash: str,
    result: StructuredCompletion, parsed: EmailAnalysisOutput,
) -> AIAnalysis:
    is_billable = parsed.classification in ("BILLABLE", "CALLING_TASK")
    should_flag = (
        parsed.requires_manual_review
        or parsed.confidence < settings.ai_manual_review_threshold
        or bool(parsed.warnings)
    )
    flag_reason = "; ".join(parsed.warnings) if parsed.warnings else (
        f"Confidence {parsed.confidence} below threshold" if should_flag else None
    )
    # Race-safe insert — see _save_failed for the full rationale. In the
    # success path a collision is extremely unlikely (deterministic hash
    # for identical content usually short-circuits at the cache layer
    # before we ever call OpenAI), but the guard costs nothing and
    # prevents pathological corner cases from killing the pipeline.
    from sqlalchemy.exc import IntegrityError
    savepoint = await session.begin_nested()
    try:
        analysis = await ai_analysis_repo.create(
            session,
            email_id=email.id, attachment_id=None,
            input_hash=input_hash,
            analysis_type=AnalysisType.EMAIL_CLASSIFY,
            provider=AIProvider.OPENAI,
            model=result.model,
            prompt_version=result.prompt_version,
            is_billable=is_billable,
            category=parsed.classification,
            rule_code=parsed.billing_rule_code,
            recommended_hours=None,   # engine computes; we store the raw AI proposal
            confidence=_confidence_bucket(parsed.confidence),
            summary=parsed.summary,
            invoice_description=parsed.invoice_description,
            reasoning=parsed.reasoning,
            should_flag=should_flag,
            flag_reason=flag_reason,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=Decimal(str(result.cost_usd)),
            latency_ms=result.latency_ms,
            raw_response={
                "parsed": parsed.model_dump(),
                "confidence_int": parsed.confidence,
                "quantity": parsed.quantity.model_dump() if parsed.quantity else None,
                "estimate_amount_usd": parsed.estimate_amount_usd,
                "building_count": parsed.building_count,
            },
        )
        await savepoint.commit()
        return analysis
    except IntegrityError:
        await savepoint.rollback()
        existing = await ai_analysis_repo.get_by_input_hash(session, input_hash)
        if existing is not None:
            return existing
        raise


async def _save_attachment_success(
    session, *, attachment: Attachment, input_hash: str,
    result: StructuredCompletion, parsed: AttachmentAnalysisOutput,
) -> AIAnalysis:
    is_billable = parsed.billing_rule_code is not None
    should_flag = (
        parsed.requires_manual_review
        or parsed.confidence < settings.ai_manual_review_threshold
        or bool(parsed.warnings)
    )
    flag_reason = "; ".join(parsed.warnings) if parsed.warnings else (
        f"Confidence {parsed.confidence} below threshold" if should_flag else None
    )
    # Race-safe insert (see _save_failed for full rationale).
    from sqlalchemy.exc import IntegrityError
    savepoint = await session.begin_nested()
    try:
        analysis = await ai_analysis_repo.create(
            session,
            email_id=None, attachment_id=attachment.id,
            input_hash=input_hash,
            analysis_type=AnalysisType.ATTACHMENT_SUMMARY,
            provider=AIProvider.OPENAI,
            model=result.model,
            prompt_version=result.prompt_version,
            is_billable=is_billable,
            category=parsed.document_type,
            rule_code=parsed.billing_rule_code,
            recommended_hours=None,
            confidence=_confidence_bucket(parsed.confidence),
            summary=parsed.summary,
            invoice_description=parsed.invoice_description,
            reasoning=parsed.reasoning,
            should_flag=should_flag,
            flag_reason=flag_reason,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=Decimal(str(result.cost_usd)),
            latency_ms=result.latency_ms,
            raw_response={
                "parsed": parsed.model_dump(),
                "confidence_int": parsed.confidence,
                "quantity": parsed.quantity.model_dump() if parsed.quantity else None,
                "estimate_amount_usd": parsed.estimate_amount_usd,
                "building_count": parsed.building_count,
                "key_facts": parsed.key_facts.model_dump() if parsed.key_facts else {},
            },
        )
        await savepoint.commit()
        return analysis
    except IntegrityError:
        await savepoint.rollback()
        existing = await ai_analysis_repo.get_by_input_hash(session, input_hash)
        if existing is not None:
            return existing
        raise


async def _save_failed(
    session, *, email_id: uuid.UUID | None, attachment_id: uuid.UUID | None,
    analysis_type: AnalysisType, input_hash: str, error: str,
) -> AIAnalysis:
    """Persist a row so the reviewer knows analysis was attempted and failed.

    Race-safe: multiple parallel workers can hit an OpenAI 429 for the
    SAME email/attachment content in quick succession. Each tries to save
    a failure row keyed by the same `input_hash`, and only the first wins
    the UNIQUE constraint. Without this guard the second worker crashes
    the entire draft creation with a UniqueViolationError — even though
    the "failure" itself is already properly recorded by the first worker.

    Behavior on collision: rollback the failed INSERT (savepoint style so
    the outer transaction stays clean), fetch and return whatever the
    winning worker saved. The caller neither knows nor cares which
    coroutine "won" — both get an AIAnalysis object back and the pipeline
    continues.
    """
    from sqlalchemy.exc import IntegrityError

    # Use a nested savepoint so the outer transaction survives the
    # collision. Without this, the whole session enters a poisoned state
    # after the IntegrityError and subsequent queries all fail.
    savepoint = await session.begin_nested()
    try:
        analysis = await ai_analysis_repo.create(
            session,
            email_id=email_id, attachment_id=attachment_id,
            input_hash=input_hash,
            analysis_type=analysis_type,
            provider=AIProvider.OPENAI,
            model=settings.ai_model_primary,
            prompt_version=PROMPT_VERSION,
            is_billable=None, category=None, rule_code=None,
            recommended_hours=None,
            confidence=Confidence.LOW.value,
            summary=None, invoice_description=None,
            reasoning=None,
            should_flag=True,
            flag_reason=f"AI call failed: {error[:800]}",
            input_tokens=0, output_tokens=0, cost_usd=Decimal("0"),
            latency_ms=0,
            raw_response={"error": error[:2000]},
        )
        await savepoint.commit()
        return analysis
    except IntegrityError:
        # Another worker beat us to this input_hash. Roll back JUST our
        # attempted insert (savepoint), then fetch and return the row
        # they wrote. This is a semantically identical outcome — a
        # persisted "AI call failed" record for that content.
        await savepoint.rollback()
        existing = await ai_analysis_repo.get_by_input_hash(session, input_hash)
        if existing is not None:
            return existing
        # Truly unexpected — hash conflict but row not found. Re-raise
        # to surface a real bug instead of silent data loss.
        raise


# ---------------------------------------------------------------------------
# IO helpers
# ---------------------------------------------------------------------------
def _load_body(email: Email) -> str:
    if not email.body_path:
        return email.body_snippet or ""
    try:
        return storage.read_text(email.body_path)
    except FileNotFoundError:
        return email.body_snippet or ""


def _load_attachment_text(att: Attachment) -> str:
    if not att.extracted_text_path:
        return att.extracted_text_snippet or ""
    try:
        return storage.read_text(att.extracted_text_path)
    except FileNotFoundError:
        return att.extracted_text_snippet or ""
