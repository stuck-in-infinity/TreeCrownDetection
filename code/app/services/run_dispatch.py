"""Start the heavy compute for a project run.

There are two modes, picked automatically:

* Airflow, when ``airflow_base_url`` is set. This starts the configured
  single-node DAG, whose task calls back into
  ``POST /api/v1/compute/analyze`` or ``/compute/finalize`` to do the work, and
  returns the Airflow ``dag_run_id``. The DAG passes that same id back as the
  ``Idempotency-Key``, so a retry replays the earlier result instead of
  computing again.

* Local, when Airflow is not configured. This runs the Celery task body in a
  daemon thread inside this process.

Either way the HTTP trigger returns straight away, and progress is read from the
project state and the latest Job row through GET /projects/{id}/runs/status.
"""
import threading

from app.core.settings import settings
from app.services.airflow_client import airflow_enabled, trigger_dag


def _conf(project_id: str, job_id: str, run: int | None = None) -> dict:
    conf = {"project_id": project_id, "job_id": job_id}
    if run is not None:
        conf["run"] = run
    return conf


def _run_local(task_name: str, project_id: str, job_id: str, run: int | None = None) -> str:
    """Run a Celery task body in a background daemon thread."""

    def _target():
        from app.workers.tasks import job_a_analyze, job_b_finalize

        task = {"job_a_analyze": job_a_analyze, "job_b_finalize": job_b_finalize}[task_name]
        try:
            task.apply(args=[project_id, job_id, run])
        except Exception:
            # The task's own _fail() has already stored the FAILED state and the
            # traceback, so there is nothing left to do here.
            pass

    threading.Thread(target=_target, name=f"{task_name}:{job_id}", daemon=True).start()
    return f"local:{job_id}"


def dispatch_analyze(project_id: str, job_id: str, run: int | None = None) -> str:
    if airflow_enabled():
        return trigger_dag(settings.analyze_dag_id, _conf(project_id, job_id, run))
    return _run_local("job_a_analyze", project_id, job_id, run)


def dispatch_finalize(project_id: str, job_id: str, run: int | None = None) -> str:
    if airflow_enabled():
        return trigger_dag(settings.finalize_dag_id, _conf(project_id, job_id, run))
    return _run_local("job_b_finalize", project_id, job_id, run)
