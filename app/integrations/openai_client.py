"""
Thin async OpenAI wrapper for structured chat completions.

WHY the abstraction:
    * Retry on 429 / 5xx / connection errors — but NOT on 4xx auth or
      schema-validation errors (those need a fix, not a retry).
    * Cost accounting per call — we log input/output tokens and dollar
      cost so the cost-report endpoint has real data.
    * One entry point (`call_structured`) means the calling code doesn't
      touch OpenAI's SDK directly. Swap to Claude / Gemini later by
      writing another client with the same signature.

Not designed to be re-used across requests — it's cheap to instantiate
(the underlying httpx client from openai handles pooling).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.config import settings
from app.core.exceptions import ExternalServiceError
from app.core.logging import get_logger
from app.schemas.ai_analysis import openai_response_format

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pricing table — $ per 1K tokens (October 2025 published prices)
# ---------------------------------------------------------------------------
# Keep in sync with OpenAI's pricing page. Wrong numbers here mean wrong
# cost reports, not billing failures — so this is a soft dependency.
MODEL_PRICING_USD_PER_1K: dict[str, tuple[float, float]] = {
    # model:               (input, output)
    "gpt-4o-mini":         (0.00015, 0.00060),
    "gpt-4o":              (0.00250, 0.01000),
    "gpt-4o-2024-08-06":   (0.00250, 0.01000),
    "gpt-4.1":             (0.00300, 0.01200),
}


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Return USD cost for a completed call. Falls back to a middle-of-
    the-road estimate if the model is unknown (so we log SOMETHING)."""
    in_rate, out_rate = MODEL_PRICING_USD_PER_1K.get(model, (0.0025, 0.010))
    return round(input_tokens / 1000 * in_rate + output_tokens / 1000 * out_rate, 6)


# ---------------------------------------------------------------------------
# Return type
# ---------------------------------------------------------------------------
@dataclass
class StructuredCompletion:
    """Everything a caller needs from ONE AI call."""
    parsed: BaseModel                # the validated Pydantic model
    raw_response: dict[str, Any]     # full OpenAI JSON (audit trail)
    model: str                       # what actually served it (may differ from request)
    input_tokens: int
    output_tokens: int
    cost_usd: float
    latency_ms: int
    prompt_version: str


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------
class OpenAIClient:
    """Async OpenAI client that returns a Pydantic-validated response."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self.api_key = api_key or settings.openai_api_key
        self.model = model or settings.ai_model_primary
        if not self.api_key:
            raise ExternalServiceError(
                "OPENAI_API_KEY is not configured on the server. Cannot make AI calls."
            )
        self._client = AsyncOpenAI(
            api_key=self.api_key,
            timeout=settings.ai_request_timeout_seconds,
        )

    async def call_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        response_model: type[BaseModel],
        response_name: str,
        prompt_version: str,
        temperature: float = 0.1,
        max_output_tokens: int | None = None,
    ) -> StructuredCompletion:
        """
        Make one AI call and return a parsed Pydantic model.

        Retries on transient failures (429, 5xx, timeouts). Does NOT retry
        on Pydantic validation errors — those indicate the model produced
        bad JSON, and a retry with an identical prompt is unlikely to help.

        Raises:
            ExternalServiceError — after retries are exhausted OR the model
                                   returned a response that couldn't be
                                   parsed as valid JSON matching the schema.
        """
        response_format = openai_response_format(response_model, response_name)
        max_out = max_output_tokens or settings.ai_max_output_tokens

        start = time.perf_counter()
        retryer = AsyncRetrying(
            stop=stop_after_attempt(settings.ai_max_retries),
            wait=wait_exponential(multiplier=1.5, min=1, max=15),
            retry=retry_if_exception_type((
                RateLimitError,
                APIConnectionError,
                APITimeoutError,
                InternalServerError,
            )),
            reraise=True,
        )

        try:
            async for attempt in retryer:
                with attempt:
                    completion = await self._client.chat.completions.create(
                        model=self.model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user",   "content": user_prompt},
                        ],
                        response_format=response_format,
                        temperature=temperature,
                        max_completion_tokens=max_out,
                    )
        except (RateLimitError, APIConnectionError, APITimeoutError, InternalServerError) as exc:
            raise ExternalServiceError(f"OpenAI unavailable after retries: {exc}") from exc

        latency_ms = int((time.perf_counter() - start) * 1000)

        # ---- Parse ---------------------------------------------------------
        try:
            content = completion.choices[0].message.content or "{}"
        except (IndexError, AttributeError) as exc:
            raise ExternalServiceError(f"OpenAI returned malformed response: {exc}") from exc

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            log.warning("openai_json_parse_failed", raw_head=content[:200])
            raise ExternalServiceError(
                f"OpenAI returned non-JSON despite strict mode: {exc}"
            ) from exc

        try:
            parsed = response_model.model_validate(data)
        except ValidationError as exc:
            log.warning("openai_schema_validation_failed", errors=exc.errors()[:3])
            raise ExternalServiceError(
                f"OpenAI response didn't match schema: {exc}"
            ) from exc

        # ---- Cost accounting ----------------------------------------------
        usage = completion.usage
        input_tokens = getattr(usage, "prompt_tokens", 0) or 0
        output_tokens = getattr(usage, "completion_tokens", 0) or 0
        cost_usd = estimate_cost_usd(self.model, input_tokens, output_tokens)

        log.info(
            "openai_call_completed",
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )

        return StructuredCompletion(
            parsed=parsed,
            raw_response=completion.model_dump(),
            model=self.model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            prompt_version=prompt_version,
        )
