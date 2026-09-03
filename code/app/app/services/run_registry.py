"""The bridge between ``Project`` (one live run) and ``Run`` (all of them).

The problem this solves
-----------------------
Run-scoped facts — state, params, model, recommended/available/chosen k, the run
name, the failure record — were stored on the ``projects`` row, so a project
described exactly one run. Moving them wholesale would have meant rewriting
every state transition in six files in one commit, with no way to ship or test
it in stages.

Instead the project keeps those fields, and this module mirrors them onto the
matching ``Run`` row at the few places they change. That gives the runs table
real content from day one while every existing caller keeps working.

Who owns what
-------------
``Project.state``   the LOCK. One computing run per project, enforced exactly as
                    before by ``transition_if``'s conditional UPDATE: a project
                    already ANALYZING fails the guard. Also a mirror of the
                    active run so ``/projects/mine`` still shows something.
``Run.state``       the TRUTH about one run. What the per-run gates read.

The two disagree on purpose in one case, and it is the case the whole feature
exists for: labelling run 2 while run 4 computes. That writes into
``work/run_2/``, touches no shared file, and moves run 2 to LABELS_SUBMITTED
while the project stays ANALYZING for run 4.

Never raises
------------
Mirroring is bookkeeping. A failure here must not take down the request that was
doing the real work, so everything is wrapped. A missing mirror is repaired on
the next transition; a 500 on a successful analyze is not repairable.
"""
from __future__ import annotations

from app.core.logging import get_logger, naive_now
from app.db import models

log = get_logger("app.run_registry")

#: Run states that mean work is happening right now.
BUSY_STATES = {"ANALYZING", "FINALIZING"}

#: A run in one of these has produced (or attempted) clustering, so it can be
#: labelled. Mirrors ``labels._LABEL_STATES``.
LABELABLE_STATES = {"AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED"}

#: A run in one of these can be exported. Mirrors ``runs._FINALIZE_FROM``.
FINALIZABLE_STATES = {"LABELS_SUBMITTED", "COMPLETED", "FAILED"}

#: Project-level states that describe the project, not any run. They must never
#: be mirrored onto a Run: UPLOADING/DELETING guard the ortho library and the
#: project folder, which are shared.
_PROJECT_ONLY_STATES = {"UPLOADING", "DELETING"}


def active_number(project) -> int:
    return getattr(project, "current_run", 1) or 1


def get_run(db, project, number: int | None = None):
    """The Run row for ``number`` (default: the active run). None if absent."""
    n = active_number(project) if number is None else number
    return (
        db.query(models.Run)
        .filter_by(project_id=project.id, number=n)
        .one_or_none()
    )


def run_ortho_id(project) -> str | None:
    """Which ortho the project's current parameters pin the run to.

    ``params['ortho_id']`` is the only trustworthy source. The old
    ``archive_current_run`` also wrote an ``ortho`` filename, but it took
    ``project.orthos[0]`` — the FIRST ortho in the library, not the one the run
    used — so with a multi-ortho project that field is simply wrong. It is not
    read here, and it should not be read anywhere.
    """
    pinned = (dict(getattr(project, "params", None) or {})).get("ortho_id")
    if pinned:
        return pinned
    # A single-ortho project has no ambiguity to resolve.
    orthos = list(getattr(project, "orthos", None) or [])
    return orthos[0].id if len(orthos) == 1 else None


def ensure_run(db, project, number: int | None = None):
    """Get, or create, the Run row for ``number``. Does not commit."""
    n = active_number(project) if number is None else number
    run = get_run(db, project, n)
    if run is None:
        run = models.Run(project_id=project.id, number=n, state="CREATED",
                         params={}, created_at=naive_now())
        db.add(run)
        db.flush()                      # give it an id without ending the txn
    return run


def mirror(db, project, number: int | None = None, commit: bool = True):
    """Copy the project's run-scoped fields onto its Run row.

    Called from every place a run-scoped field changes — see the call sites in
    ``services/state.py``, ``workers/tasks.py`` and the trigger endpoints. One
    line each, rather than a rewrite of all of them.
    """
    try:
        run = ensure_run(db, project, number)

        state = getattr(project, "state", None)
        # UPLOADING/DELETING are the project's business. Mirroring them would
        # tell the user their RUN is uploading, which is not a thing.
        if state and state not in _PROJECT_ONLY_STATES:
            if run.state != state:
                if state in BUSY_STATES and run.started_at is None:
                    run.started_at = naive_now()
                if state in ("COMPLETED", "FAILED", "AWAITING_LABELS"):
                    run.finished_at = naive_now()
            run.state = state

        run.name = getattr(project, "run_name", None)
        run.model_key = getattr(project, "model_key", None)
        run.params = dict(getattr(project, "params", None) or {})
        run.recommended_k = getattr(project, "recommended_k", None)
        run.available_k = getattr(project, "available_k", None)
        run.chosen_k = (dict(getattr(project, "params", None) or {})).get("chosen_k")
        run.error = getattr(project, "error", None)
        if run.ortho_id is None:
            run.ortho_id = run_ortho_id(project)

        db.add(run)
        if commit:
            db.commit()
        return run
    except Exception:                    # noqa: BLE001 - bookkeeping must not fail the request
        log.exception("could not mirror project %s onto its run row",
                      getattr(project, "id", "?"))
        try:
            if commit:
                db.rollback()
        except Exception:                # noqa: BLE001
            pass
        return None


def refresh_project_state(db, project, commit: bool = True) -> str | None:
    """Recompute ``Project.state`` from the run rows.

    The project's state is derived, not authored: it is whichever run is
    computing right now, and otherwise the active run's state. Deriving it is
    what makes finalizing an OLDER run safe — the project claims the lock, the
    old run does the work, and when it finishes the project goes back to
    describing the active run instead of inheriting the old one's outcome.

    Project-only states (UPLOADING, DELETING) are left alone: they describe the
    project itself and no run row can speak for them.
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
                      getattr(project, "id", "?"))
        return None


def set_run_state(db, project, number: int, state: str, error=None):
    """Move ONE run to ``state``, then re-derive the project's state from it.

    This is what the worker calls. It exists because the worker used to write
    straight to ``project.state``, which meant it could only ever describe the
    active run — finalize an older one and the project would end up reporting
    that run's outcome instead of its own.
    """
    try:
        run = ensure_run(db, project, number)
        if state in BUSY_STATES and run.started_at is None:
            run.started_at = naive_now()
        if state in ("COMPLETED", "FAILED", "AWAITING_LABELS"):
            run.finished_at = naive_now()
        run.state = state
        if error is not None:
            run.error = error
        db.add(run)
        db.commit()
        refresh_project_state(db, project)
        return run
    except Exception:                    # noqa: BLE001
        log.exception("could not set run %s of project %s to %s",
                      number, getattr(project, "id", "?"), state)
        try:
            db.rollback()
        except Exception:                # noqa: BLE001
            pass
        return None


def labels_for(db, run) -> int:
    """How many cluster labels this run has. Used by the capability flags."""
    if run is None:
        return 0
    return db.query(models.ClusterLabel).filter_by(run_id=run.id).count()


def can_label(run) -> bool:
    return bool(run) and run.state in LABELABLE_STATES


def can_finalize(db, run) -> bool:
    """Finalize needs both a permitted state and labels to export.

    Checked together because offering the button and then answering 400
    NO_LABELS is the kind of thing that costs somebody an afternoon.
    """
    return bool(run) and run.state in FINALIZABLE_STATES and labels_for(db, run) > 0
