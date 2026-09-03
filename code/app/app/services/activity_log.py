"""Per-user activity audit log (JSONL ledger).

Mirrors the other team's ``activity-YYYY-MM-DD.jsonl`` pattern: one JSON object
per line, one file per UTC day, appended under ``<storage_root>/activity/``.

Audit-only: identity comes from the frontend's Google-sign-in headers
(``X-User-Email`` / ``X-User-Id``); the backend does not verify the token (see
docs/OAUTH_GIS_INTEGRATION_PLAN.md §4). Best-effort — logging never raises into
the request path.
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
    """Append one audit record. Never raises."""
    try:
        now = now_ist()   # IST for the admin; daily file is IST-dated too
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
        # Best-effort: a ledger write failure must never propagate into the
        # request path. Log it (the log call is guarded so it, too, can't raise).
        try:
            log.exception("activity_log write failed")
        except Exception:
            pass
