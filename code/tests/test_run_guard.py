"""The run's own process must stop itself when its time is up.

The two process tests use a real child and check it is dead on time. They use
the fork context so the child body can live in this module; production uses
spawn, but the timeout code under test is the same either way. The parent-side
tests use a stand-in process.
"""
import multiprocessing
import os
import signal
import time

import pytest


def _guard(fresh_app, **env):
    base = {"TCP_RUN_TIMEOUT_ENABLED": "true",
            "TCP_ANALYZE_TIMEOUT_MIN": "10",
            "TCP_FINALIZE_TIMEOUT_MIN": "10",
            "TCP_RUN_KILL_GRACE_S": "1"}
    base.update(env)
    fresh_app(base)
    from app.services import run_guard
    return run_guard


class _FakeProc:
    """Already finished, with the exit code under test."""

    def __init__(self, exitcode):
        self.exitcode = exitcode

    def join(self, _timeout=None):
        return None


# ------------------------------------------------------------ .env budgets
def test_budgets_come_from_env(fresh_app):
    g = _guard(fresh_app, TCP_ANALYZE_TIMEOUT_MIN="10", TCP_FINALIZE_TIMEOUT_MIN="3")
    assert g.budget_seconds("job_a_analyze") == 600
    assert g.budget_seconds("job_b_finalize") == 180


def test_zero_or_disabled_means_no_limit(fresh_app):
    g = _guard(fresh_app, TCP_ANALYZE_TIMEOUT_MIN="0")
    assert g.budget_seconds("job_a_analyze") is None
    g = _guard(fresh_app, TCP_RUN_TIMEOUT_ENABLED="false")
    assert g.budget_seconds("job_a_analyze") is None
    assert g.budget_seconds("job_b_finalize") is None


# ------------------------------------------- the process stopping itself
def _run_child(body) -> tuple[int, float]:
    """Run ``body`` in a child and return (exit code, seconds it lived)."""
    ctx = multiprocessing.get_context("fork")
    proc = ctx.Process(target=body, daemon=True)
    started = time.monotonic()
    proc.start()
    proc.join(20)
    elapsed = time.monotonic() - started
    if proc.is_alive():
        proc.kill()
        proc.join(5)
        pytest.fail(f"the child never stopped itself ({elapsed:.1f}s)")
    return proc.exitcode, elapsed


def test_child_interrupts_itself_at_the_deadline(fresh_app):
    """SIGALRM raises inside the run, which is what lets the task body record
    the failure through its own except clause."""
    g = _guard(fresh_app)
    from app.core.failures import RunTimeout

    def body():
        g._arm_self_timeout(1.0, "analyze", "job-alarm")
        try:
            time.sleep(60)
        except RunTimeout:
            os._exit(g.EXIT_TIMED_OUT)
        os._exit(99)                          # never interrupted

    code, elapsed = _run_child(body)
    assert code == g.EXIT_TIMED_OUT, f"expected the alarm to fire, got exit {code}"
    assert elapsed < 10, f"took {elapsed:.1f}s for a 1s budget"


def test_child_kills_itself_when_the_alarm_cannot_be_delivered(fresh_app):
    """A long call inside torch or GDAL never returns to the interpreter, so
    the handler above would never run. Blocking SIGALRM reproduces that: the
    signal stays pending and only the watchdog can end the process."""
    g = _guard(fresh_app)

    def body():
        signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
        g._arm_self_timeout(1.0, "analyze", "job-stuck")
        time.sleep(60)
        os._exit(99)                          # watchdog never fired

    code, elapsed = _run_child(body)
    assert code == g.EXIT_HARD_KILLED, f"watchdog did not kill it (exit {code})"
    # budget 1s + grace 1s, plus scheduling slack
    assert elapsed < 10, f"took {elapsed:.1f}s for a 1s budget + 1s grace"


def test_a_run_that_finishes_in_time_is_not_touched(fresh_app):
    g = _guard(fresh_app)

    def body():
        g._arm_self_timeout(5.0, "analyze", "job-quick")
        time.sleep(0.2)
        g._disarm()
        time.sleep(6)                         # past the old deadline, disarmed
        os._exit(g.EXIT_OK)

    code, _ = _run_child(body)
    assert code == g.EXIT_OK, "a disarmed timeout still fired"


# ------------------------------------------------- what the parent makes of it
def test_clean_exit_returns(fresh_app, monkeypatch):
    g = _guard(fresh_app)
    monkeypatch.setattr(g, "_spawn", lambda *a: _FakeProc(g.EXIT_OK))
    monkeypatch.setattr(g, "_record", lambda *a: pytest.fail("nothing to record"))
    assert g.run_guarded("job_a_analyze", "p1", "j1") is None


def test_self_recorded_timeout_is_not_recorded_twice(fresh_app, monkeypatch):
    """EXIT_TIMED_OUT means the task's own _fail() already wrote it."""
    g = _guard(fresh_app)
    monkeypatch.setattr(g, "_spawn", lambda *a: _FakeProc(g.EXIT_TIMED_OUT))
    monkeypatch.setattr(g, "_record", lambda *a: pytest.fail("child already recorded"))
    with pytest.raises(g.RunFailed):
        g.run_guarded("job_a_analyze", "p1", "j1")


def test_hard_kill_is_recorded_by_the_parent(fresh_app, monkeypatch):
    """The watchdog killed the interpreter, so nothing was written and the Job
    row would stay RUNNING."""
    g = _guard(fresh_app)
    from app.core.failures import RunTimeout, classify

    recorded = []
    monkeypatch.setattr(g, "_spawn", lambda *a: _FakeProc(g.EXIT_HARD_KILLED))
    monkeypatch.setattr(g, "_record",
                        lambda pid, jid, run, exc: recorded.append(exc))
    with pytest.raises(g.RunFailed):
        g.run_guarded("job_a_analyze", "p1", "j1")

    assert len(recorded) == 1
    assert isinstance(recorded[0], RunTimeout)
    assert classify(recorded[0], stage="analyze")["code"] == "RUN_TIMEOUT"


def test_ordinary_failure_raises_without_recording(fresh_app, monkeypatch):
    g = _guard(fresh_app)
    monkeypatch.setattr(g, "_spawn", lambda *a: _FakeProc(g.EXIT_FAILED))
    monkeypatch.setattr(g, "_record", lambda *a: pytest.fail("task already recorded"))
    with pytest.raises(g.RunFailed):
        g.run_guarded("job_a_analyze", "p1", "j1")


def test_oom_kill_is_recorded(fresh_app, monkeypatch):
    g = _guard(fresh_app)
    from app.core.failures import classify

    recorded = []
    monkeypatch.setattr(g, "_spawn", lambda *a: _FakeProc(-9))
    monkeypatch.setattr(g, "_record",
                        lambda pid, jid, run, exc: recorded.append(exc))
    with pytest.raises(g.RunFailed):
        g.run_guarded("job_b_finalize", "p1", "j1")
    assert classify(recorded[0], stage="finalize")["code"] == "COMPUTE_KILLED"


# ------------------------------------------------------------- the message
def test_timeout_reads_like_any_other_failure(fresh_app):
    _guard(fresh_app)
    from app.core.failures import RunTimeout, classify

    out = classify(RunTimeout("The analyze was stopped after 10 minutes, which "
                              "is the time limit set for it on this server."),
                   stage="analyze")
    assert out["code"] == "RUN_TIMEOUT"
    assert "10 minutes" in out["message"]
    assert "TCP_ANALYZE_TIMEOUT_MIN" in out["hint"]
    assert out["stage"] == "analyze"
