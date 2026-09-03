#!/usr/bin/env python3
"""Retention cleanup that respects each project's consent setting.

This script stands alone and runs from cron; it needs no Celery or Redis. It
deletes or trims projects whose last activity, ``updated_at``, is older than
``settings.retention_days``, and what it does depends on the consent value:

    0 (no)              -> delete the storage folder and the database row
    1 (yes, everything) -> keep, as long as settings.retain_consent_all is set
    2 (unlabelled only) -> keep everything through Step 1, delete the labelled
                           output in step2/3/4 and the ClusterLabel rows, set
                           the state to PRUNED and fill in pruned_at

It runs on one machine and may be interrupted part-way, so:
  * a non-blocking file lock lets only one run happen at a time, and the lock is
    released when the process exits, including on a crash;
  * it commits after each project, so an interruption leaves the finished ones
    done and the next run picks up the rest;
  * every filesystem operation can be repeated safely, and a consent=2 project
    that already has pruned_at set is skipped, so a half-finished sweep simply
    carries on;
  * a project that is ANALYZING or FINALIZING is never touched.

Run it from the ``code/`` directory with the same environment as the API, so it
sees the same database and storage volume:
    python scripts/run_retention.py            # do the work
    python scripts/run_retention.py --dry-run  # only report what it would do

A daily cron entry at 03:00:
    0 3 * * *  cd /code && python scripts/run_retention.py >> /data/storage/retention.log 2>&1
"""
import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

# Make the app package importable when this is run as
# `python scripts/run_retention.py` from the code/ directory, since scripts/ and
# app/ sit side by side.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.core.logging import (                               # noqa: E402
    configure_logging,
    naive_from_ts,
    naive_now,
    now_ist,
)
from app.core.settings import settings                       # noqa: E402
from app.core.storage import (                               # noqa: E402
    delete_project_dir,
    prune_labelled_outputs,
    project_root,
)
from app.db import models                                    # noqa: E402
from app.db.session import SessionLocal                      # noqa: E402

_BUSY_STATES = {"ANALYZING", "FINALIZING"}

log = logging.getLogger("app.retention")


def _log(msg: str) -> None:
    # Print for the cron redirect into retention.log, and also log it, so the
    # line ends up in the central app.log as well.
    print(f"[retention {now_ist():%Y-%m-%d %H:%M:%S IST}] {msg}", flush=True)
    log.info(msg)


def _acquire_lock():
    """Take a file lock without waiting.

    Returns the open file handle, which the caller must keep open for as long as
    the lock is needed, or None if another run already holds it. The lock is
    released when the process exits.
    """
    lock_path = os.path.join(settings.storage_root, ".retention.lock")
    try:
        os.makedirs(settings.storage_root, exist_ok=True)
        import fcntl

        fh = open(lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return None
        fh.write(f"{os.getpid()} {datetime.utcnow().isoformat()}\n")
        fh.flush()
        return fh
    except Exception:
        # No fcntl, on Windows for instance, or some other error. Carry on
        # without a lock, which is fine for a single scheduled run.
        return open(lock_path, "a") if os.path.isdir(settings.storage_root) else None


def _runs_of(project) -> range:
    return range(1, (getattr(project, "current_run", 1) or 1) + 1)


def run(dry_run: bool = False) -> dict:
    cutoff = naive_now() - timedelta(days=settings.retention_days)
    summary = {"deleted": [], "pruned": [], "skipped_busy": [],
               "retained": [], "orphans": [], "dry_run": dry_run}
    db = SessionLocal()
    try:
        expired = (
            db.query(models.Project)
            .filter(models.Project.updated_at < cutoff)
            .all()
        )
        for p in expired:
            if p.state in _BUSY_STATES:
                summary["skipped_busy"].append(p.id)
                continue
            consent = getattr(p, "consent", 0) or 0

            if consent == 1 and settings.retain_consent_all:
                summary["retained"].append(p.id)
                continue

            if consent == 2:
                if getattr(p, "pruned_at", None):
                    continue                                  # already pruned
                summary["pruned"].append(p.id)
                if dry_run:
                    continue
                try:
                    for r in _runs_of(p):
                        prune_labelled_outputs(p.id, r)
                    db.query(models.ClusterLabel).filter_by(project_id=p.id).delete()
                    p.state = "PRUNED"
                    p.pruned_at = naive_now()
                    db.add(p)
                    db.commit()                               # per-project commit
                except Exception:
                    log.error("prune failed for project %s", p.id, exc_info=True)
                    db.rollback()
                continue

            # Consent 0, or consent 1 with retention disabled: delete it all.
            summary["deleted"].append(p.id)
            if dry_run:
                continue
            try:
                delete_project_dir(p.id)
                db.delete(p)
                db.commit()
            except Exception:
                log.error("delete failed for project %s", p.id, exc_info=True)
                db.rollback()

        # Also remove folders on disk that have no database row and are older
        # than the cutoff.
        live_ids = {pid for (pid,) in db.query(models.Project.id).all()}
        projects_dir = os.path.join(settings.storage_root, "projects")
        if os.path.isdir(projects_dir):
            for name in os.listdir(projects_dir):
                full = os.path.join(projects_dir, name)
                if name in live_ids or not os.path.isdir(full):
                    continue
                try:
                    mtime = naive_from_ts(os.path.getmtime(full))
                except OSError:
                    continue
                if mtime < cutoff:
                    summary["orphans"].append(name)
                    if not dry_run:
                        import shutil
                        shutil.rmtree(full, ignore_errors=True)
    finally:
        db.close()
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Consent-aware retention cleanup.")
    ap.add_argument("--dry-run", action="store_true",
                    help="log what would happen; change nothing")
    args = ap.parse_args()

    configure_logging()

    lock = _acquire_lock()
    if lock is None:
        _log("another retention run holds the lock — exiting.")
        return 0
    try:
        _log(f"start (retention_days={settings.retention_days}, dry_run={args.dry_run})")
        s = run(dry_run=args.dry_run)
        _log("deleted={d} pruned={p} retained={r} skipped_busy={b} orphans={o}".format(
            d=len(s["deleted"]), p=len(s["pruned"]), r=len(s["retained"]),
            b=len(s["skipped_busy"]), o=len(s["orphans"])))
        for k in ("deleted", "pruned", "orphans"):
            if s[k]:
                _log(f"{k}: {', '.join(s[k])}")
        return 0
    finally:
        try:
            lock.close()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
