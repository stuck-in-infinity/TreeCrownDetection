"""Keeps ``Run`` rows in step with the ``Project`` row.

Run-scoped fields (state, params, model, k values, name, error) still live on
the project for its active run. This module copies them onto the matching
``Run`` row whenever they change.

``Project.state`` is the lock: one computing run per project, enforced by
``transition_if``. ``Run.state`` is the state of one run, and is what the
per-run gates read. The two can differ, e.g. run 2 is labelled while run 4 is
ANALYZING.

Mirroring is bookkeeping, so nothing here raises: a failure is logged and rolled
back rather than failing the request that did the real work.
"""
from __future__ import annotations

from sqlalchemy import inspect

from app.core.logging import get_logger, naive_now
from app.db import models

log = get_logger("app.run_registry")

#: Run states that mean work is in progress.
BUSY_STATES = {"ANALYZING", "FINALIZING"}

#: States that stamp ``finished_at``.
_FINISHED_STATES = {"AWAITING_LABELS", "COMPLETED", "FAILED"}

#: States a run can be labelled from. Mirrors ``labels._LABEL_STATES``.
LABELABLE_STATES = {"AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED"}

#: States a run can be finalized from. Mirrors ``runs._FINALIZE_FROM``.
FINALIZABLE_STATES = {"LABELS_SUBMITTED", "COMPLETED", "FAILED"}

#: Project-level locks on shared resources (ortho library, project folder).
#: Never copied onto a run.
_PROJECT_ONLY_STATES = {"UPLOADING", "DELETING"}


def _project_id(project) -> str:
    """The project's id for logging, without querying the database.

    Called from except blocks, where the Postgres transaction may be aborted
    and a lazy load of ``project.id`` would raise again.
    """
    try:
        state = inspect(project)
        if state.identity:
            return str(state.identity[0])
        return str(state.dict.get("id") or "?")
    except Exception:                    # noqa: BLE001 - not a mapped instance
        return "?"


def _rollback(db) -> None:
    # On Postgres a failed statement aborts the transaction; without this the
    # caller's next query fails with InFailedSqlTransaction.
    try:
        db.rollback()
    except Exception:                    # noqa: BLE001
        pass


def _stamp_times(run, state: str) -> None:
    if state in BUSY_STATES and run.started_at is None:
        run.started_at = naive_now()
    if state in _FINISHED_STATES:
        run.finished_at = naive_now()


def active_number(project) -> int:
    return getattr(project, "current_run", 1) or 1


def get_run(db, project, number: int | None = None):
    """The Run row for ``number`` (default: the active run), or None."""
    n = active_number(project) if number is None else number
    return (
        db.query(models.Run)
        .filter_by(project_id=project.id, number=n)
        .one_or_none()
    )


def run_ortho_id(project) -> str | None:
    """The ortho the active run uses: ``params['ortho_id']``, or the only ortho.

    Do not use the ``ortho`` filename in ``project.runs`` history; it records
    the first ortho in the library, not the one the run used.
    """
    pinned = (dict(getattr(project, "params", None) or {})).get("ortho_id")
    if pinned:
        return pinned
    orthos = list(getattr(project, "orthos", None) or [])
    return orthos[0].id if len(orthos) == 1 else None


def ensure_run(db, project, number: int | None = None):
    """Get or create the Run row for ``number``. Flushes, does not commit."""
    n = active_number(project) if number is None else number
    run = get_run(db, project, n)
    if run is None:
        run = models.Run(project_id=project.id, number=n, state="CREATED",
                         params={}, created_at=naive_now())
        db.add(run)
        db.flush()
    return run


def mirror(db, project, number: int | None = None, commit: bool = True):
    """Copy the project's run-scoped fields onto its Run row.

    Returns the row, or None on failure. ``commit=False`` leaves the commit to
    the caller.
    """
    try:
        run = ensure_run(db, project, number)
        params = dict(getattr(project, "params", None) or {})

        state = getattr(project, "state", None)
        if state and state not in _PROJECT_ONLY_STATES:
            if run.state != state:
                _stamp_times(run, state)
            run.state = state

        run.name = getattr(project, "run_name", None)
        run.model_key = getattr(project, "model_key", None)
        run.params = params
        run.recommended_k = getattr(project, "recommended_k", None)
        run.available_k = getattr(project, "available_k", None)
        run.chosen_k = params.get("chosen_k")
        run.error = getattr(project, "error", None)
        if run.ortho_id is None:
            run.ortho_id = run_ortho_id(project)

        db.add(run)
        if commit:
            db.commit()
        return run
    except Exception:                    # noqa: BLE001 - must not fail the request
        log.exception("could not mirror project %s onto its run row",
                      _project_id(project))
        _rollback(db)
        return None


def refresh_project_state(db, project, commit: bool = True) -> str | None:
    """Derive ``Project.state`` from the run rows.

    A busy run's state wins; otherwise the active run's state. This lets an
    older run be finalized without the project taking on that run's outcome.
    UPLOADING and DELETING are left alone.
    """
    try:
        if project.state in _PROJECT_ONLY_STATES:
            return project.state
        rows = db.query(models.Run).filter_by(project_id=project.id).all()
        busy = next((r for r in rows if r.state in BUSY_STATES), None)
        if busy is not None:
            new_state = busy.state
        else:
            active = next((r for r in rows if r.number == active_number(project)), None)
            new_state = active.state if active else project.state
        if new_state and new_state != project.state:
            project.state = new_state
            db.add(project)
            if commit:
                db.commit()
        return new_state
    except Exception:                    # noqa: BLE001
        log.exception("could not refresh project state for %s",
                      _project_id(project))
        _rollback(db)
        return None


def set_run_state(db, project, number: int, state: str, error=None):
    """Move one run to ``state`` and re-derive the project's state.

    Used by the worker and the trigger endpoints, so a run other than the
    active one can change state. Commits.
    """
    try:
        run = ensure_run(db, project, number)
        _stamp_times(run, state)
        run.state = state
        if error is not None:
            run.error = error
        db.add(run)
        db.commit()
        refresh_project_state(db, project)
        return run
    except Exception:                    # noqa: BLE001
        log.exception("could not set run %s of project %s to %s",
                      number, _project_id(project), state)
        _rollback(db)
        return None


def labels_for(db, run) -> int:
    """Number of cluster labels stored for ``run``."""
    if run is None:
        return 0
    return db.query(models.ClusterLabel).filter_by(run_id=run.id).count()


def can_label(run) -> bool:
    return bool(run) and run.state in LABELABLE_STATES


def can_finalize(db, run) -> bool:
    """Finalize needs a finalizable state and at least one label."""
    return bool(run) and run.state in FINALIZABLE_STATES and labels_for(db, run) > 0
