"""
File storage abstraction.

Attachments live at:  storage/attachments/{hash[:2]}/{hash}.{ext}
Extracted text at:    storage/text/{hash[:2]}/{hash}.txt
Email bodies at:      storage/bodies/{yyyy-mm}/{gmail_message_id}.txt

Two-char prefix directories cap the file-per-folder count at ~256, which
keeps filesystem operations O(1) even after millions of attachments.

Design note: this module returns the RELATIVE path stored in the DB (e.g.
"attachments/ab/abcd123....pdf"). The absolute filesystem path is derived
at read time via `storage.absolute_path(rel_path)`. This means we can move
the storage root (local disk → S3 → Render Disk) without a data migration.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)


class FileStorage:
    """Local-disk file storage. All paths are relative to `settings.storage_root`."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path(settings.storage_root)).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ---- Path helpers -----------------------------------------------------
    def absolute_path(self, rel_path: str) -> Path:
        """Resolve a relative path (as stored in DB) to an absolute filesystem path."""
        # Prevent path traversal — reject anything with `..`
        clean = rel_path.lstrip("/").replace("\\", "/")
        if ".." in clean.split("/"):
            raise ValueError(f"Refusing suspicious path: {rel_path!r}")
        return (self.root / clean).resolve()

    def attachment_path(self, hash_hex: str, extension: str) -> str:
        """Relative path for a deduped attachment file."""
        ext = extension.lstrip(".").lower() or "bin"
        return f"attachments/{hash_hex[:2]}/{hash_hex}.{ext}"

    def extracted_text_path(self, hash_hex: str) -> str:
        """Relative path for the extracted-text sidecar of an attachment."""
        return f"text/{hash_hex[:2]}/{hash_hex}.txt"

    def email_body_path(self, gmail_message_id: str, date: datetime) -> str:
        """Relative path for a stored email body."""
        # Group by YYYY-MM so listing a month's-worth of bodies is cheap.
        yyyy_mm = date.strftime("%Y-%m")
        # Sanitize gmail_message_id (alphanumeric only, ~16 chars normally)
        safe_id = "".join(c for c in gmail_message_id if c.isalnum() or c == "-")
        return f"bodies/{yyyy_mm}/{safe_id}.txt"

    # ---- IO ---------------------------------------------------------------
    def write_bytes(self, rel_path: str, data: bytes) -> str:
        """Write bytes. Idempotent — if the file already exists with the same
        size we assume it's the same (SHA already checked upstream) and skip."""
        target = self.absolute_path(rel_path)
        if target.exists() and target.stat().st_size == len(data):
            return rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write — write to .tmp, then rename.
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)
        return rel_path

    def write_text(self, rel_path: str, text: str) -> str:
        return self.write_bytes(rel_path, text.encode("utf-8"))

    def read_bytes(self, rel_path: str) -> bytes:
        return self.absolute_path(rel_path).read_bytes()

    def read_text(self, rel_path: str) -> str:
        return self.absolute_path(rel_path).read_text(encoding="utf-8", errors="replace")

    def exists(self, rel_path: str) -> bool:
        return self.absolute_path(rel_path).exists()

    def size(self, rel_path: str) -> int:
        return self.absolute_path(rel_path).stat().st_size


# Module-level singleton — cheap and safe (no state, just a Path).
storage = FileStorage()
