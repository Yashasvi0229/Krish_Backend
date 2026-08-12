"""
Job orchestration — public entry points for the API layer, plus progress
publishing so WebSocket clients see live updates.

Progress is published to Redis pub/sub (`gnc:job:<id>` channel) on every
step change. The WebSocket handler subscribes to that channel and streams
messages to the browser.
"""
from __future__ import annotations

import json
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import JobType
from app.core.logging import get_logger
from app.repositories import job_repo
from app.utils.redis_client import get_async_client, job_channel

log = get_logger(__name__)

# Fixed step list for the search-and-analyze pipeline. Sent to the
# frontend on job creation so it can render the progress bar labels.
SEARCH_STEPS: list[dict[str, Any]] = [
    {"index": 0, "name": "Searching Gmail"},
    {"index": 1, "name": "Fetching emails"},
    {"index": 2, "name": "Downloading attachments"},
    {"index": 3, "name": "Extracting text"},
    {"index": 4, "name": "Finalizing"},
]


async def create_gmail_search_job(
    session: AsyncSession,
    *,
    claim_no: str | None,
    file_name: str | None,
    gnc_file_no: str | None,
    client_id: uuid.UUID | None,
    user_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """Create a new PENDING job for a Gmail search. Returns the job UUID.

    The caller is expected to then dispatch the actual work — either via
    Celery (`.delay()`) or by calling the sync worker directly in eager mode.
    """
    job = await job_repo.create(
        session,
        job_type=JobType.INVOICE_SEARCH,
        input_params={
            "claim_no": claim_no,
            "file_name": file_name,
            "gnc_file_no": gnc_file_no,
            "client_id": str(client_id) if client_id else None,
        },
        steps=SEARCH_STEPS,
        user_id=user_id,
    )
    await session.commit()
    return job.id


# ---------------------------------------------------------------------------
# Progress publishing
# ---------------------------------------------------------------------------
async def publish_progress(
    job_id: uuid.UUID,
    *,
    status: str,
    progress: float | Decimal,
    step_index: int,
    step_name: str,
    stats: dict[str, Any] | None = None,
    message: str | None = None,
) -> None:
    """Publish a progress event to the job's pub/sub channel.

    Payload shape (frontend consumes as-is):
        {
          "type": "progress",
          "job_id": "...",
          "status": "PROCESSING",
          "progress": 42.5,
          "step_index": 2,
          "step_name": "Downloading attachments",
          "stats": {"emails_fetched": 30, ...},
          "message": null
        }
    """
    payload = {
        "type": "progress",
        "job_id": str(job_id),
        "status": status,
        "progress": float(progress),
        "step_index": step_index,
        "step_name": step_name,
        "stats": stats or {},
        "message": message,
    }
    client = get_async_client()
    try:
        await client.publish(job_channel(str(job_id)), json.dumps(payload))
    except Exception as exc:  # noqa: BLE001 — never fail a job because pub/sub is down
        log.warning("progress_publish_failed", job_id=str(job_id), error=str(exc))


async def publish_completion(
    job_id: uuid.UUID,
    *,
    status: str,   # COMPLETED | FAILED
    result: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Terminal event — WebSocket handler closes the connection after this."""
    payload = {
        "type": "completion",
        "job_id": str(job_id),
        "status": status,
        "result": result or {},
        "error": error,
    }
    client = get_async_client()
    try:
        await client.publish(job_channel(str(job_id)), json.dumps(payload))
    except Exception as exc:  # noqa: BLE001
        log.warning("completion_publish_failed", job_id=str(job_id), error=str(exc))
