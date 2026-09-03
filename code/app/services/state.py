"""Project state transitions that two callers cannot both win.

Reading the state, deciding, then writing the new state leaves a gap in which
two callers can both pass the check and both run the destructive
``reset_dirs``. ``transition_if`` uses one conditional UPDATE instead,
``SET state=:new WHERE id=:id AND state IN (:allowed)``, which the database
applies as a single step, so only one caller can succeed. The other gets
``False`` back and answers with 409 CONFLICT_BUSY.

Two things to get right when calling it:

* ``allowed`` must be the state you actually checked, normally the exact
  ``pre_state`` you read, not a wide set. A wider set still updates in one step
  but allows the very overlap the check was meant to prevent.
* A transition into a state already in ``allowed`` excludes nobody: every
  concurrent caller's UPDATE matches a row and they all get True. The inline
  compute endpoints are in exactly that position, because the trigger has
  already moved the project into ANALYZING or FINALIZING, so they claim a Job
  row instead. See ``services/job_claim.py``.

Note that this also clears ``Project.error``, so read any failure text you still
need before calling it.
"""
from app.db import models


def transition_if(db, project, allowed: set[str], new_state: str) -> bool:
    """Move ``project.state`` to ``new_state`` only if it is now in ``allowed``.

    Returns True if this caller made the change, False otherwise. On success the
    ``project`` object in this session is refreshed to the new state.
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
    return bool(affected)
