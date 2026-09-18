"""Run a pipeline task in its own process, under a wall-clock limit.

Every dispatch path calls ``task.apply()``, which runs the body in the calling
process — a daemon thread for local dispatch, the request threadpool for the
``/compute/*`` and ``drone_api`` callbacks. A thread cannot be killed and the
pipeline has no cancellation point to poll, so the limit needs its own process.
Celery's ``task_time_limit`` does not apply either: ``.apply()`` never reaches a
worker.

The child holds its own deadline rather than the API supervising it, so an API
restart mid-run cannot leave the run going forever.

Two mechanisms, because a signal handler only runs when the interpreter next
executes bytecode and a long call inside torch or GDAL never returns to it:

1. ``SIGALRM`` raises ``RunTimeout`` in the task body, which records the failure
   through its own ``except`` clause like any other.
2. A watchdog thread calls ``os._exit()`` after ``run_kill_grace_s``. It records
   nothing, so the parent writes that case, recognising it by exit code.

Neither can stop a C call that holds the GIL past the grace period.
"""
import multiprocessing
import os
import signal
import threading

from app.core.failures import RunTimeout
from app.core.logging import get_logger
from app.core.settings import settings

log = get_logger("app.runguard")

#: Exit codes the child uses to tell the parent how it died.
EXIT_OK = 0
EXIT_FAILED = 1          # ordinary failure; the task's _fail() recorded it
EXIT_TIMED_OUT = 75      # SIGALRM path; the task's _fail() recorded it
EXIT_HARD_KILLED = 76    # watchdog path; nothing recorded, the parent must

#: task name -> the settings attribute holding its budget, in minutes.
_BUDGETS = {
    "job_a_analyze": "analyze_timeout_min",
    "job_b_finalize": "finalize_timeout_min",
}

_STAGES = {"job_a_analyze": "analyze", "job_b_finalize": "finalize"}

#: Set by the child when its run ends, so the watchdog stands down.
_FINISHED: threading.Event | None = None


class RunFailed(Exception):
    """The run did not succeed. Details are already on the Job and the run row."""


def budget_seconds(task_name: str) -> float | None:
    """This task's wall-clock budget in seconds, or None for no limit."""
    if not settings.run_timeout_enabled:
        return None
    minutes = getattr(settings, _BUDGETS.get(task_name, ""), 0) or 0
    return float(minutes) * 60 if minutes > 0 else None


def _arm_self_timeout(budget_s: float, stage: str, job_id: str) -> None:
    """Make the current process stop itself once ``budget_s`` has passed."""
    minutes = budget_s / 60

    def _on_alarm(_signum, _frame):
        raise RunTimeout(
            f"The {stage} was stopped after {minutes:.0f} minutes, which is the "
            f"time limit set for it on this server."
        )

    # Fires only between bytecodes, so a call inside a C extension will not see
    # it until that call returns.
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.setitimer(signal.ITIMER_REAL, budget_s)

    global _FINISHED
    _FINISHED = threading.Event()
    done = _FINISHED

    def _watchdog():
        if not done.wait(budget_s + settings.run_kill_grace_s):
            log.error(
                "job %s did not stop %ss after its %.0f minute limit - killing "
                "the process (stage=%s)",
                job_id, settings.run_kill_grace_s, minutes, stage,
            )
            # os._exit, not sys.exit: SystemExit would only unwind this thread
            # and leave the run going.
            os._exit(EXIT_HARD_KILLED)

    threading.Thread(target=_watchdog, name=f"timeout:{job_id[:12]}",
                     daemon=True).start()


def _disarm() -> None:
    """Stop both mechanisms once the run has finished on its own."""
    if hasattr(signal, "SIGALRM"):
        signal.setitimer(signal.ITIMER_REAL, 0)
    if _FINISHED is not None:
        _FINISHED.set()


def _child(task_name: str, project_id: str, job_id: str, run: int | None) -> int:
    """The child process's entry point: arm the clock, then run the task."""
    budget = budget_seconds(task_name)
    stage = _STAGES.get(task_name, "run")
    if budget:
        _arm_self_timeout(budget, stage, job_id)

    from app.workers.tasks import job_a_analyze, job_b_finalize

    task = {"job_a_analyze": job_a_analyze, "job_b_finalize": job_b_finalize}[task_name]
    try:
        task.apply(args=[project_id, job_id, run])
    except RunTimeout:
        # The task body already caught this, called _fail() and re-raised.
        return EXIT_TIMED_OUT
    except Exception:                          # noqa: BLE001
        return EXIT_FAILED
    finally:
        _disarm()
    return EXIT_OK


def _spawn(task_name: str, project_id: str, job_id: str, run: int | None):
    """Start the run in its own interpreter. Separate so tests can patch it.

    Spawn rather than fork: uvicorn runs threads, and a forked child can inherit
    a lock held by a thread that does not exist on its side. CUDA also does not
    survive fork.
    """
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(
        target=_child,
        args=(task_name, project_id, job_id, run),
        daemon=True,
        name=f"{task_name}:{job_id[:12]}",
    )
    proc.start()
    return proc


def _record(project_id: str, job_id: str, run: int | None, exc: Exception) -> None:
    """Write the failure a child that died abruptly could not write itself.

    The exception is raised and caught rather than passed in ready-made, because
    ``tasks._fail`` calls ``traceback.format_exc()``, which outside an active
    exception context yields "NoneType: None".
    """
    from app.db.session import SessionLocal
    from app.workers import tasks

    db = SessionLocal()
    try:
        try:
            raise exc
        except Exception as raised:            # noqa: BLE001
            tasks._fail(db, project_id, job_id, raised, run)
    except Exception:                          # noqa: BLE001
        log.exception("could not record the failure for job %s", job_id)
        try:
            db.rollback()                      # else the session is poisoned on Postgres
        except Exception:                      # noqa: BLE001
            pass
    finally:
        db.close()


def run_guarded(task_name: str, project_id: str, job_id: str,
                run: int | None = None) -> None:
    """Run one task body in a self-timing child process. Blocks until it ends.

    Raises on every failure, as ``task.apply().get(propagate=True)`` did: the
    inline callers catch it and build their 500 from ``project.error``.
    """
    stage = _STAGES.get(task_name, "run")
    proc = _spawn(task_name, project_id, job_id, run)
    proc.join()                     # the child owns the clock, so no timeout here
    code = proc.exitcode

    if code == EXIT_OK:
        return

    if code == EXIT_TIMED_OUT:
        minutes = (budget_seconds(task_name) or 0) / 60
        log.error("job %s stopped itself after its %.0f minute limit "
                  "(project=%s stage=%s)", job_id, minutes, project_id, stage)
        raise RunFailed(f"The {stage} exceeded its time limit.")

    if code == EXIT_HARD_KILLED:
        minutes = (budget_seconds(task_name) or 0) / 60
        _record(project_id, job_id, run,
                RunTimeout(f"The {stage} was stopped after {minutes:.0f} minutes, "
                           f"which is the time limit set for it on this server."))
        raise RunFailed(f"The {stage} was killed at its time limit.")

    if code is not None and code < 0:
        # Killed from outside, almost always the OOM killer. The Job row would
        # stay RUNNING forever without this.
        log.error("job %s was killed by signal %s (project=%s stage=%s)",
                  job_id, -code, project_id, stage)
        killed = RunFailed(f"The run was killed by signal {-code} "
                           f"(exitcode {code}).")
        _record(project_id, job_id, run, killed)
        raise killed

    raise RunFailed(f"The {stage} failed (exit code {code}).")
