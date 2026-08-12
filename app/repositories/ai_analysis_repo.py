"""Repository for `ai_analyses`."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import AIProvider, AnalysisType
from app.models.ai_analysis import AIAnalysis


async def get_by_id(session: AsyncSession, analysis_id: uuid.UUID) -> AIAnalysis | None:
    return await session.get(AIAnalysis, analysis_id)


async def get_by_input_hash(session: AsyncSession, input_hash: str) -> AIAnalysis | None:
    """Cache lookup. `input_hash` is UNIQUE so at most one row exists."""
    result = await session.execute(
        select(AIAnalysis).where(AIAnalysis.input_hash == input_hash).limit(1)
    )
    return result.scalar_one_or_none()


async def get_by_email(
    session: AsyncSession, email_id: uuid.UUID
) -> AIAnalysis | None:
    """Latest analysis for one email (there's typically only one per email
    per prompt_version)."""
    result = await session.execute(
        select(AIAnalysis)
        .where(AIAnalysis.email_id == email_id)
        .order_by(AIAnalysis.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_by_attachment(
    session: AsyncSession, attachment_id: uuid.UUID
) -> AIAnalysis | None:
    result = await session.execute(
        select(AIAnalysis)
        .where(AIAnalysis.attachment_id == attachment_id)
        .order_by(AIAnalysis.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def create(
    session: AsyncSession,
    *,
    email_id: uuid.UUID | None,
    attachment_id: uuid.UUID | None,
    input_hash: str,
    analysis_type: AnalysisType,
    provider: AIProvider,
    model: str,
    prompt_version: str,
    is_billable: bool | None,
    category: str | None,
    rule_code: str | None,
    recommended_hours: Decimal | None,
    confidence: str,
    summary: str | None,
    invoice_description: str | None,
    reasoning: str | None,
    should_flag: bool,
    flag_reason: str | None,
    input_tokens: int,
    output_tokens: int,
    cost_usd: Decimal,
    latency_ms: int,
    raw_response: dict[str, Any] | None,
) -> AIAnalysis:
    row = AIAnalysis(
        email_id=email_id, attachment_id=attachment_id,
        input_hash=input_hash,
        analysis_type=analysis_type.value,
        provider=provider.value,
        model=model, prompt_version=prompt_version,
        is_billable=is_billable, category=category, rule_code=rule_code,
        recommended_hours=recommended_hours, confidence=confidence,
        summary=summary, invoice_description=invoice_description,
        reasoning=reasoning,
        should_flag=should_flag, flag_reason=flag_reason,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cost_usd=cost_usd, latency_ms=latency_ms,
        raw_response=raw_response,
    )
    session.add(row)
    await session.flush()
    return row
