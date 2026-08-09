"""
Celery app.

Two modes controlled by `CELERY_TASK_ALWAYS_EAGER`:
    * `false` (production) — tasks queue to Redis; a separate worker
      process picks them up. Web process returns immediately.
    * `true`  (dev / free tier) — tasks execute synchronously in the
      web process's own thread. Slower for the user (blocking) but
      needs no worker service. Everything else stays the same.

We also start Celery Beat entries here for the periodic Gmail sync.
"""
from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "gnc_invoice",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.workers.email_tasks", "app.workers.ai_tasks"],
)

celery_app.conf.update(
    # Serialization — JSON only, no pickle (safer + broker-agnostic).
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone=settings.timezone,
    enable_utc=True,

    # Reliability
    task_acks_late=True,                    # ack only after successful completion
    task_reject_on_worker_lost=True,        # requeue if worker dies
    task_track_started=True,                # show 'STARTED' status in result backend
    result_expires=3600,                    # results dropped after 1h

    # Limits
    task_time_limit=settings.celery_task_time_limit,           # hard kill
    task_soft_time_limit=settings.celery_task_soft_time_limit, # SoftTimeLimitExceeded raise

    # Eager mode toggle
    task_always_eager=settings.celery_task_always_eager,
    task_eager_propagates=True,             # let eager tasks raise instead of swallow

    # Beat schedule — periodic Gmail sync
    beat_schedule={
        "sync-recent-emails-every-10-min": {
            "task": "email.sync_recent_metadata",
            "schedule": crontab(minute="*/10"),
        },
    },
)
