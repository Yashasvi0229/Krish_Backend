"""
Pydantic schemas for job endpoints.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class GmailSearchRequest(BaseModel):
    """Payload for POST /api/jobs/gmail-search."""
    claim_no: str | None = Field(None, max_length=100)
    file_name: str | None = Field(None, max_length=255)
    gnc_file_no: str | None = Field(None, max_length=50)
    client_id: uuid.UUID | None = None


class JobCreatedResponse(BaseModel):
    """Response after successfully queueing a job."""
    job_id: uuid.UUID
    status: str
    steps: list[dict[str, Any]]
    websocket_url: str    # relative URL the frontend should connect to


class JobDetailResponse(BaseModel):
    """Response for GET /api/jobs/{id} — polling fallback for WS."""
    id: uuid.UUID
    job_type: str
    status: str
    progress: float
    current_step_index: int
    current_step_name: str
    steps: list[dict[str, Any]]
    stats: dict[str, Any]
    result_data: dict[str, Any] | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
