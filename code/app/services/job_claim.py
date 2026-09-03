"""Exclusive claim on the right to compute a project run.

Three endpoints run the pipeline synchronously inside the API process:
``/compute/*`` (the Airflow callbacks), ``/project/analyze`` and
``/project/finalize``. All three share one ``work/run_<n>`` directory and each
wipes it on startup, so two running at once would destroy each other's output.

The project state machine cannot enforce this on its own. The HTTP trigger has
already moved the project into ANALYZING or FINALIZING by the time the
orchestrator calls back, so that state is a valid source state and a
conditional update onto the state the project is already in succeeds for every
caller. (Everywhere else, ``services.state.transition_if`` is still the right
tool; see invariant 1 in docs/CODEBASE_MAP.md.)

The claim lives on the Job row instead:

* the unique ``(project_id, celery_task_id)`` index lets the INSERT decide
  between two callbacks carrying the same Idempotency-Key, which a
  SELECT-then-INSERT could not;
* a separate active-job check catches duplicates arriving under different keys.

Claim keys are prefixed with ``compute:`` so the active-job check can tell a
real inline compute job from the placeholder Job that ``runs.py`` creates when
it hands work to Airflow. That placeholder stays RUNNING forever, so counting it
would reject every legitimate callback.

Callers build their own error responses: ``/compute/*`` returns a plain 400,
which the DAGs treat as a skip, and the user-facing routes return their usual
409 CONFLICT_BUSY envelope.
"""
import uuid

from sqlalchemy.exc import IntegrityError

from app.core.logging import get_logger, naive_now
from app.db import models

log = get_logger("app.api")

_KEY_PREFIX = "compute:"
_ACTIVE_JOB_STATES = ("QUEUED", "RUNNING")

# Outcomes of a claim attempt.
WON = "won"              # this caller computes
ACTIVE = "active"        # another compute job is already running
DUPLICATE = "duplicate"  # same Idempotency-Key, still in flight
REPLAY = "replay"        # same Idempotency-Key, already SUCCEEDED


def compute_key(raw: str | None) -> str:
    """Prefix an orchestrator key, generating a unique one when there is none.

    A missing Idempotency-Key must not become NULL. NULLs compare as distinct in
    the unique index, so such a row would be invisible to both the replay lookup
    and the active-job check.
    """
    return f"{_KEY_PREFIX}{raw}" if raw else f"{_KEY_PREFIX}auto:{uuid.uuid4()}"


def find_prior(db, project, key: str | None):
    """The most recent job recorded under this claim key, if any."""
    if not key:
        return None
    return (
        db.query(models.Job)
        .filter_by(project_id=project.id, celery_task_id=key)
        .order_by(models.Job.started_at.desc())
        .first()
    )


def active_job(db, project):
    """Another inline compute job already running for this project, any key."""
    return (
        db.query(models.Job)
        .filter(
            models.Job.project_id == project.id,
            models.Job.state.in_(_ACTIVE_JOB_STATES),
            models.Job.celery_task_id.like(f"{_KEY_PREFIX}%"),
        )
        .order_by(models.Job.started_at.desc())
        .first()
    )


def claim(db, project, key: str, job_type: str, request_id: str | None = None):
    """Try to claim the run. Returns ``(job, outcome)``; job is None unless WON.

    If the INSERT loses the race, the winner may have already finished. The
    outcome is then REPLAY and the caller should return the success payload.
    """
    existing = active_job(db, project)
    if existing is not None:
        log.warning(
            "compute claim rejected project=%s key=%s type=%s: job %s already %s",
            project.id, key, job_type, existing.id, existing.state,
        )
        return None, ACTIVE

    job = models.Job(
        project_id=project.id, type=job_type, state="RUNNING",
        started_at=naive_now(), celery_task_id=key, request_id=request_id,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = find_prior(db, project, key)
        if prior and prior.state == "SUCCEEDED":
            return None, REPLAY
        log.warning("compute claim duplicate project=%s key=%s type=%s",
                    project.id, key, job_type)
        return None, DUPLICATE
    db.refresh(job)
    return job, WON
