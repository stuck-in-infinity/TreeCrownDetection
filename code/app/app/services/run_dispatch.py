"""Dispatch the heavy compute for a project run.

Two modes, chosen automatically:

* **Airflow** (``airflow_base_url`` set) — trigger the configured single-node DAG.
  The DAG task calls back into this service's ``POST /api/v1/compute/analyze`` /
  ``/compute/finalize`` endpoints (body-style, STACD contract), which do the actual
  work. Returns the Airflow ``dag_run_id``. The DAG forwards that ``dag_run_id`` as a
  stable ``Idempotency-Key`` so retries replay instead of recomputing.

* **Local** (no Airflow configured) — run the Celery task body in a daemon thread
  in-process. The HTTP trigger returns immediately and the frontend polls state.

Either way the endpoint returns fast; progress is tracked via the project state
machine + the latest Job row (exposed at GET /projects/{id}/runs/status).
"""
import threading

from app.core.settings import settings
from app.services.airflow_client import airflow_enabled, trigger_dag


def _conf(project_id: str, job_id: str, run: int | None = None) -> dict:
    conf = {"project_id": project_id, "job_id": job_id}
    if run is not None:
        conf["run"] = run
    return conf


def _run_local(task_name: str, project_id: str, job_id: str,
               run: int | None = None) -> str:
    """Execute a Celery task body in a background daemon thread.

    ``run`` is passed explicitly rather than left for the task to read off
    ``project.current_run``. That default is only correct while a project has
    one workable run; finalizing an EARLIER run needs to name it, or the worker
    exports the wrong folder.
    """

    def _target():
        from app.workers.tasks import job_a_analyze, job_b_finalize

        task = {"job_a_analyze": job_a_analyze, "job_b_finalize": job_b_finalize}[task_name]
        try:
            task.apply(args=[project_id, job_id, run])
        except Exception:
            # The task's own _fail() already recorded FAILED state + traceback.
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
