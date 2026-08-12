"""
SHA-256 hashing helpers.

Two distinct hashes are used across the app:
    * `content_hash` — for emails. SHA-256 over normalized (subject, body,
      from_email, date). Same email fetched twice → same hash → skip.
      Also lets us re-use AI analysis if the same body appears in a
      different thread.
    * `file_hash` — for attachments. SHA-256 over the raw bytes. Same PDF
      attached to 10 emails → one file on disk, 10 attachment rows all
      pointing to it.
"""
from __future__ import annotations

import hashlib
from datetime import datetime


def content_hash(
    subject: str,
    body: str,
    from_email: str,
    date: datetime,
) -> str:
    """Hash of an email's content. Stable across fetches.

    Normalization:
        * subject/body: strip leading/trailing whitespace, lowercase
        * from_email: lowercase (email addresses are case-insensitive)
        * date: ISO-8601 UTC (so timezone shifts don't change the hash)

    Returns 64 lowercase hex chars.
    """
    h = hashlib.sha256()
    h.update((subject or "").strip().lower().encode("utf-8"))
    h.update(b"\x00")
    h.update((body or "").strip().lower().encode("utf-8"))
    h.update(b"\x00")
    h.update((from_email or "").strip().lower().encode("utf-8"))
    h.update(b"\x00")
    # Ensure timezone-aware; use UTC ISO format for a stable representation.
    if date.tzinfo is None:
        raise ValueError("content_hash requires a timezone-aware datetime")
    h.update(date.astimezone().isoformat().encode("utf-8"))
    return h.hexdigest()


def file_hash(data: bytes) -> str:
    """SHA-256 of raw file bytes. Used for attachment deduplication."""
    return hashlib.sha256(data).hexdigest()


def file_hash_stream(chunks) -> str:
    """Hash a byte stream chunk-by-chunk. Use when the file doesn't fit in memory."""
    h = hashlib.sha256()
    for chunk in chunks:
        h.update(chunk)
    return h.hexdigest()
