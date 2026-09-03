"""Per-user activity log, written as JSON lines.

Follows the same ``activity-YYYY-MM-DD.jsonl`` pattern the other team uses: one
JSON object per line, one file per day, appended under
``<storage_root>/activity/``.

This is for auditing only. The identity comes from the ``X-User-Email`` and
``X-User-Id`` headers the frontend sets after Google sign-in, and the backend
does not verify the token. Writing is best effort and never raises into the
request.
"""
import json
import os
import threading
from datetime import datetime, timezone

from app.core.logging import get_logger, now_ist
from app.core.settings import settings

log = get_logger("app.activity")
_LOCK = threading.Lock()


def _log_dir() -> str:
    root = getattr(settings, "storage_root", "/data/storage") or "/data/storage"
    return os.path.join(root, "activity")


def _log_path(now: datetime) -> str:
    return os.path.join(_log_dir(), f"activity-{now:%Y-%m-%d}.jsonl")


def append(
    email: str | None,
    action: str,
    *,
    user_id: str | None = None,
    method: str | None = None,
    path: str | None = None,
    project_id: str | None = None,
    status: int | None = None,
    **extra,
) -> None:
    """Append one record. Never raises."""
    try:
        now = now_ist()   # IST, so the timestamps and the daily filename agree
        record = {
            "ts": now.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "email": email or "anonymous",
            "user_id": user_id,
            "action": action,
            "method": method,
            "path": path,
            "project_id": project_id,
            "status": status,
        }
        if extra:
            record.update(extra)
        line = json.dumps(record, ensure_ascii=False)
        with _LOCK:
            os.makedirs(_log_dir(), exist_ok=True)
            with open(_log_path(now), "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception:
        # A failed write here must not break the request. Report it, with the
        # logging call itself guarded so it cannot raise either.
        try:
            log.exception("activity_log write failed")
        except Exception:
            pass
