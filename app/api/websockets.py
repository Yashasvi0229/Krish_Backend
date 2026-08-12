"""
WebSocket endpoint for live job progress.

Route:
    WS /api/jobs/{job_id}/ws?token=<jwt>

Auth: WebSockets can't easily carry Authorization headers from the browser
side, so we accept the token as a query param. Same JWT as the REST API.
Short session lifetime (JWT expires in 24h) keeps this acceptable.

Streaming: on connect, we send the current job state, then subscribe to
Redis pub/sub `gnc:job:<id>` and forward every message. When we see a
"completion" event (status ∈ COMPLETED|FAILED), we close the socket.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, status

from app.core.exceptions import UnauthorizedError
from app.core.logging import get_logger
from app.core.security import decode_access_token
from app.database import AsyncSessionLocal
from app.repositories import job_repo
from app.utils.redis_client import job_channel, new_pubsub_subscription

log = get_logger(__name__)
router = APIRouter()


@router.websocket("/jobs/{job_id}/ws")
async def job_progress_ws(
    websocket: WebSocket,
    job_id: uuid.UUID,
    token: str = Query(..., description="JWT access token"),
) -> None:
    """Live progress stream for a job."""
    # ---- 1. Auth --------------------------------------------------------
    try:
        decode_access_token(token)
    except UnauthorizedError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()

    # ---- 2. Send current job state -------------------------------------
    try:
        async with AsyncSessionLocal() as session:
            job = await job_repo.get_by_id(session, job_id)
        if job is None:
            await websocket.send_json({
                "type": "error", "message": "Job not found.",
            })
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return

        await websocket.send_json({
            "type": "state",
            "job_id": str(job.id),
            "status": job.status,
            "progress": float(job.progress),
            "step_index": job.current_step_index,
            "step_name": job.current_step_name,
            "stats": job.stats or {},
        })

        # If already terminal, close immediately.
        if job.status in ("COMPLETED", "FAILED", "CANCELLED"):
            await websocket.send_json({
                "type": "completion",
                "job_id": str(job.id),
                "status": job.status,
                "result": job.result_data or {},
                "error": job.error_message,
            })
            await websocket.close()
            return

        # ---- 3. Subscribe to Redis pub/sub and stream --------------
        async with new_pubsub_subscription(job_channel(str(job_id))) as ps:
            async for msg in ps.listen():
                if msg.get("type") != "message":
                    continue
                try:
                    payload = json.loads(msg["data"]) if isinstance(msg["data"], str) else msg["data"]
                except (TypeError, ValueError):
                    continue

                await websocket.send_json(payload)

                # Close after a terminal event.
                if payload.get("type") == "completion":
                    await websocket.close()
                    return

    except WebSocketDisconnect:
        log.info("ws_client_disconnected", job_id=str(job_id))
    except Exception:  # noqa: BLE001
        log.exception("ws_error", job_id=str(job_id))
        try:
            await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        except Exception:  # noqa: BLE001
            pass
