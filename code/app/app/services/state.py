"""Atomic project state transitions (v4 §9.4).

The check-then-set pattern (read state, decide, write new state) has a race
window: two concurrent callers can both pass the guard and then both run the
destructive ``reset_dirs``. ``transition_if`` instead performs a single
conditional UPDATE — ``SET state=:new WHERE id=:id AND state IN (:allowed)`` —
which the database applies atomically, so exactly one caller wins. The loser
sees ``False`` and the caller raises 409 CONFLICT_BUSY.

Two things to get right when calling it:

* ``allowed`` must be the state you actually validated against — usually the
  exact ``pre_state`` you read — not a broad set. Widen it and the update stays
  "atomic" while permitting the very interleaving the guard was there to stop.
* A self-transition excludes nobody. If ``new_state`` is already in ``allowed``,
  every concurrent caller's UPDATE matches a row and all of them return True.
  The inline-compute endpoints hit exactly this (the trigger has already moved
  the project into ANALYZING/FINALIZING), which is why they claim a Job row
  instead — see ``services/job_claim.py``.

Note it also clears ``Project.error``: read any failure text you still need
before transitioning.
"""
from app.db import models


def transition_if(db, project, allowed: set[str], new_state: str) -> bool:
    """Move ``project.state`` to ``new_state`` iff it is currently in ``allowed``.

    Returns True if this caller performed the transition, False otherwise. On
    success the in-session ``project`` is refreshed to reflect the new state.
    """
    affected = (
        db.query(models.Project)
        .filter(models.Project.id == project.id, models.Project.state.in_(allowed))
        .update(
            {models.Project.state: new_state, models.Project.error: None},
            synchronize_session=False,
        )
    )
    db.commit()
    if affected:
        db.refresh(project)
        # Keep the run row in step. Every project state change of consequence
        # comes through here, which makes this the one place worth hooking.
        from app.services.run_registry import mirror
        mirror(db, project)
    return bool(affected)
