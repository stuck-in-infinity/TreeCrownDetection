"""Start the heavy compute for a project run.

There are two modes, picked automatically:

* Airflow, when ``airflow_base_url`` is set. This starts the configured
  single-node DAG, whose task calls back into
  ``POST /api/v1/compute/analyze`` or ``/compute/finalize`` to do the work, and
  returns the Airflow ``dag_run_id``. With both DAG ids set to the combined
  DAG, its one task calls ``POST /api/v1/project/drone_api`` instead, naming
  the ``action`` sent in the conf. The DAG passes that same id back as the
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

def dag_id_for_job(job_type: str) -> str:
    """The DAG id this job type is dispatched to, from TCP_*_DAG_ID in .env."""
    return settings.finalize_dag_id if job_type == "finalize" else settings.analyze_dag_id

def _conf(project_id: str, job_id: str, run: int | None = None,
          action: str | None = None) -> dict:
    conf = {"project_id": project_id, "job_id": job_id}
    if run is not None:
        conf["run"] = run
    if action:
        # Both DAG ids may name the one combined DAG (drone_pipeline), which
        # reads what to do from here and passes it back to /project/drone_api.
        # The per-job DAGs ignore the extra keys.
        conf["action"] = action
        conf["execution_type"] = "fullexec"
    return conf

def _run_local(task_name: str, project_id: str, job_id: str, run: int | None = None) -> str:
    """Start the run in the background and return at once.

    The thread only waits: the run itself happens in its own process, which
    holds its own wall-clock deadline (services/run_guard.py). A thread could
    not do that, nothing can kill one, and the pipeline has no cancellation
    point to poll.
    """

    def _target():
        from app.services.run_guard import run_guarded

        try:
            run_guarded(task_name, project_id, job_id, run)
        except Exception:
            # Nothing above this thread to report to, the trigger returned
            # long ago. Whatever happened is already on the Job and the run
            # row, written by the task's own _fail() or, for a run that had to
            # kill its own process, by run_guarded.
            pass

    threading.Thread(target=_target, name=f"{task_name}:{job_id}", daemon=True).start()
    return f"local:{job_id}"

def dispatch_analyze(project_id: str, job_id: str, run: int | None = None) -> str:
    if airflow_enabled():
        return trigger_dag(dag_id_for_job("analyze"), _conf(project_id, job_id, run, "analyze"))
    return _run_local("job_a_analyze", project_id, job_id, run)

def dispatch_finalize(project_id: str, job_id: str, run: int | None = None) -> str:
    if airflow_enabled():
        return trigger_dag(dag_id_for_job("finalize"), _conf(project_id, job_id, run, "finalize"))
    return _run_local("job_b_finalize", project_id, job_id, run)
