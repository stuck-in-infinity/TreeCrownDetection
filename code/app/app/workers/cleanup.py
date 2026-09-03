"""Periodic retention cleanup.

A Celery Beat task deletes projects whose last activity (``updated_at``) is older
than ``settings.retention_days`` — removing both the database row and the
project's storage folder. It:

  * skips projects that are currently running (ANALYZING / FINALIZING),
  * is guarded by a Redis lock so two beat ticks / workers can't overlap,
  * also sweeps *orphan* folders on disk (a project folder with no DB row whose
    last modification is older than the cutoff), in case the DB and disk drift.

Schedule and registration live in app/workers/celery_app.py.
"""
import os
import shutil
from datetime import datetime, timedelta

from app.core.logging import naive_from_ts, naive_now
from app.core.settings import settings
from app.core.storage import delete_project_dir
from app.db import models
from app.db.session import SessionLocal
from app.workers.celery_app import celery_app

_LOCK_KEY = "treecrown:cleanup:lock"
_LOCK_TTL = 3600                       # seconds; auto-expires if a run crashes
_BUSY_STATES = {"ANALYZING", "FINALIZING"}


def _acquire_lock():
    """Return (redis_client_or_None, acquired_bool). Proceeds lock-free if Redis
    is unreachable rather than skipping the cleanup entirely."""
    try:
        import redis

        client = redis.from_url(settings.redis_url)
        acquired = bool(
            client.set(_LOCK_KEY, datetime.utcnow().isoformat(), nx=True, ex=_LOCK_TTL)
        )
        return client, acquired
    except Exception:
        return None, True


def _release_lock(client) -> None:
    if client is not None:
        try:
            client.delete(_LOCK_KEY)
        except Exception:
            pass


@celery_app.task(name="app.workers.cleanup.cleanup_expired_projects")
def cleanup_expired_projects() -> dict:
    """Delete projects + folders idle for >= retention_days. Returns a summary."""
    client, acquired = _acquire_lock()
    if not acquired:
        return {"skipped": "another cleanup run holds the lock"}

    cutoff = naive_now() - timedelta(days=settings.retention_days)
    deleted, skipped = [], []
    try:
        db = SessionLocal()
        try:
            expired = (
                db.query(models.Project)
                .filter(models.Project.updated_at < cutoff)
                .all()
            )
            for project in expired:
                if project.state in _BUSY_STATES:
                    skipped.append(project.id)        # never delete a running project
                    continue
                delete_project_dir(project.id)        # remove storage folder
                db.delete(project)                    # remove DB row (+ cascades)
                deleted.append(project.id)
            db.commit()

            live_ids = {pid for (pid,) in db.query(models.Project.id).all()}
        finally:
            db.close()

        orphans = _sweep_orphan_folders(live_ids, cutoff)
        return {
            "cutoff": cutoff.isoformat(),
            "retention_days": settings.retention_days,
            "deleted_projects": len(deleted),
            "skipped_running": len(skipped),
            "deleted_orphan_folders": len(orphans),
        }
    finally:
        _release_lock(client)


def _sweep_orphan_folders(live_ids: set, cutoff: datetime) -> list:
    """Remove project folders on disk that have no DB row and are older than cutoff."""
    base = os.path.join(settings.storage_root, "projects")
    removed = []
    if not os.path.isdir(base):
        return removed
    for name in os.listdir(base):
        path = os.path.join(base, name)
        if not os.path.isdir(path) or name in live_ids:
            continue
        try:
            mtime = naive_from_ts(os.path.getmtime(path))
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(name)
    return removed
