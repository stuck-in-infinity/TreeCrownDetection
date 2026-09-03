"""Release runs that a restart killed, so their projects are not stuck forever.

The problem
-----------
A run is dispatched by putting the project into ``ANALYZING`` (or
``FINALIZING``) and starting the work. When that work runs **in this process** —
``run_dispatch._run_local`` starts a daemon thread — a container restart kills
the thread without anybody updating the database. The project is left in
``ANALYZING`` with nothing running.

There is no way out of that state through the API:

  * ``_ANALYZE_FROM`` excludes ``ANALYZING``          -> cannot re-run
  * ``_BUSY_STATES`` blocks ``PATCH /project``        -> cannot reconfigure
  * ``_DELETABLE_STATES`` excludes ``ANALYZING``      -> cannot even delete it

The project is unusable until somebody edits the database by hand. On a server
that restarts on its own schedule, every project mid-run at that moment is lost
this way.

What is and is NOT recovered
----------------------------
The distinction that matters is **who was doing the work**, and ``Job.celery_task_id``
records exactly that, because ``_mark_dispatched`` stores whatever
``dispatch_analyze`` returned:

  * ``"local:<job_id>"`` — the work was a daemon thread in this process. The
    restart killed it. It is never coming back. **Recover.**
  * anything else — an Airflow ``dag_run_id``. Airflow is a separate service; the
    DAG is very likely still running and will call back into ``/compute/*`` when
    it finishes. Marking that project FAILED would destroy a live run.
    **Leave alone.**
  * ``NULL`` — the process died between ``_new_job`` and ``_mark_dispatched``, so
    nothing was ever dispatched. **Recover.**

That last case is why this cannot simply key off ``airflow_enabled()``: even with
Airflow configured, a crash before dispatch leaves an orphan.

Deliberately not a general sweeper. It runs once at start-up, when by definition
no request is in flight, and only touches rows it can prove are dead.
"""
from __future__ import annotations

import json

from sqlalchemy import or_

from app.core.logging import ERROR_CODES, get_logger
from app.core.settings import settings
from app.db import models

log = get_logger("app.startup_recovery")

#: The two transient in-progress states. A project sitting in either of these at
#: start-up had a run going when the process stopped.
_IN_PROGRESS = ("ANALYZING", "FINALIZING")

#: Jobs that had not reported a terminal state.
_LIVE_JOB_STATES = ("QUEUED", "RUNNING")

#: Prefix `run_dispatch._run_local` puts in the dispatch id for in-process work.
_LOCAL_PREFIX = "local:"


def _failure_record(job, orchestrated: bool = False) -> str:
    """The same shape ``app.core.failures.classify`` produces, so
    ``_last_error`` serves it and the frontend renders it like any other
    failure — message, stage, hint, and the detail behind a toggle.

    ``orchestrated`` distinguishes the two ways a run can end up here. Both are
    "this run is not coming back", but telling a user the server restarted when
    it did not sends them to the wrong person with the wrong question.
    """
    if orchestrated:
        message = ("This run was handed to the job scheduler, and the scheduler "
                   "has no record of it finishing. It is not still running.")
        hint = ("nothing was saved from it — start the run again; if it keeps "
                "ending this way, ask an administrator to check the scheduler's "
                "logs for this run")
    else:
        message = ("The server restarted while this run was in progress, so the "
                   "run was stopped before it finished.")
        hint = ("nothing was saved from it — start the run again; if this keeps "
                "happening, ask an administrator why the service is restarting")
    return json.dumps({
        "code": ERROR_CODES["SERVER_RESTARTED"],
        "stage": getattr(job, "current_stage", None) if job else None,
        "message": message,
        "hint": hint,
        "details": {
            "exception": None,
            "raw": None,
            "job_id": getattr(job, "id", None) if job else None,
            "recovered_at_startup": True,
            "orchestrated": orchestrated,
        },
    })


#: Airflow DAG-run states that mean the run is over, whatever the database says.
_AIRFLOW_DONE = ("success", "failed", "skipped", "upstream_failed")


def _airflow_finished(dag_run_id: str) -> bool:
    """True when Airflow says this DAG run is over, or has never heard of it.

    Only ever returns True on a definite answer. Anything else — Airflow
    unreachable, an unexpected reply, a state this does not recognise — returns
    False, because the two mistakes do not cost the same. Leaving a project
    stuck wastes somebody's afternoon; wrongly declaring a LIVE run dead
    destroys work in progress and writes a failure nobody can explain.
    """
    from app.services.airflow_client import get_dag_run_state

    try:
        state = (get_dag_run_state(dag_run_id, timeout=5) or "").strip().lower()
    except Exception as exc:                  # noqa: BLE001
        # 404 means Airflow has no such run: it was purged, or it never survived
        # whatever killed this service. Either way nothing is running it.
        # `get_dag_run_state` raises RuntimeError carrying the status in its text.
        if "404" in str(exc):
            log.info("airflow has no dag run %s; treating it as finished", dag_run_id)
            return True
        log.warning("could not ask airflow about dag run %s (%s); leaving it alone",
                    dag_run_id, exc)
        return False
    if state in _AIRFLOW_DONE:
        log.info("airflow reports dag run %s as %s", dag_run_id, state)
        return True
    log.info("airflow reports dag run %s as %s; leaving it alone", dag_run_id, state)
    return False


def _is_orphaned(job) -> bool:
    """True when this job's worker cannot possibly still be running.

    The first three cases are decided from the row alone. The fourth has to ask
    a question over the network, which is why this is the only part of start-up
    that talks to another service.
    """
    if job is None:
        return True                       # no job row at all — nothing can be running
    task_id = (job.celery_task_id or "").strip()
    if not task_id:
        return True                       # died before dispatch was recorded
    if task_id.startswith(_LOCAL_PREFIX):
        return True                       # our own thread; the restart killed it

    # What is left is an Airflow dag_run_id.
    #
    # This used to stop here and return False, reasoning that Airflow is a
    # separate service whose DAG is probably still going. That reasoning holds
    # for the minutes after a restart and stops holding immediately after: a DAG
    # run that died in July is not "probably still running". The check could not
    # tell the difference, so those projects sat in ANALYZING for months with no
    # way out through the API at all.
    #
    # Airflow is the only thing that actually knows, so ask it.
    from app.services.airflow_client import airflow_enabled

    if not airflow_enabled():
        # No orchestrator is configured, so nothing can be running this DAG run;
        # it is a leftover from a previous deployment.
        log.info("airflow is not configured; treating dag run %s as dead", task_id)
        return True
    return _airflow_finished(task_id)


def recover_interrupted_runs(db) -> dict:
    """Mark runs the restart killed as FAILED. Returns a summary for the log.

    Never raises: a failure here must not stop the service from starting. A
    project left stuck is bad; a service that will not boot is worse.
    """
    if not getattr(settings, "startup_recovery_enabled", True):
        log.info("startup recovery disabled by configuration")
        return {"recovered": 0, "left_to_airflow": 0, "disabled": True}

    recovered, left = [], []
    try:
        stuck = (
            db.query(models.Project)
            .filter(models.Project.state.in_(_IN_PROGRESS))
            .all()
        )
        for project in stuck:
            job = (
                db.query(models.Job)
                .filter(models.Job.project_id == project.id)
                .filter(models.Job.state.in_(_LIVE_JOB_STATES))
                .order_by(models.Job.started_at.desc())
                .first()
            )
            if not _is_orphaned(job):
                # Airflow says this DAG run is still going. Touching it would
                # destroy a live run.
                left.append(project.id)
                continue

            # Was the work ours, or the orchestrator's? Only the wording of the
            # failure differs, but it decides who the user goes to about it.
            task_id = (getattr(job, "celery_task_id", None) or "").strip()
            orchestrated = bool(task_id) and not task_id.startswith(_LOCAL_PREFIX)

            # State and error set in ONE conditional update. `transition_if`
            # cannot be used here: it clears Project.error, which would wipe the
            # explanation in the same statement that needs to write it.
            affected = (
                db.query(models.Project)
                .filter(models.Project.id == project.id,
                        models.Project.state.in_(_IN_PROGRESS))
                .update({models.Project.state: "FAILED",
                         models.Project.error: _failure_record(job, orchestrated)},
                        synchronize_session=False)
            )
            if not affected:
                continue
            if job is not None:
                job.state = "FAILED"
                job.error = ("the job scheduler has no record of this run finishing"
                             if orchestrated
                             else "server restarted while this job was running")
                db.add(job)
            recovered.append(project.id)

        db.commit()
    except Exception:                      # noqa: BLE001 - must never block start-up
        log.exception("startup recovery failed; continuing without it")
        try:
            db.rollback()
        except Exception:                  # noqa: BLE001
            pass
        return {"recovered": 0, "left_to_airflow": 0, "error": True}

    if recovered or left:
        log.warning(
            "startup recovery: %d run(s) released after restart, %d left to Airflow",
            len(recovered), len(left),
        )
    return {"recovered": len(recovered), "left_to_airflow": len(left),
            "project_ids": recovered}
