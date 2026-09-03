"""STACD DAG — drone_finalize (single node).

Species assignment + validation + reproject to WGS84 + species_map.kmz export,
in one blocking task that calls our backend's STACD-style compute endpoint.

Trigger (from our API):
    POST {AIRFLOW}/api/v1/dags/drone_finalize/dagRuns
    body: {"conf": {"project_id": "<uuid>", "run": <int>}}

The task posts to OUR backend:
    POST {DRONE_API_BASE}/api/v1/compute/finalize
    headers: Idempotency-Key = <dag_run_id>
    body: {"execution_id": <uuid4>, "project_id": <conf.project_id>, "run": <conf.run>}

Success criterion: HTTP 200 AND response JSON status == "success".
"""
import json
import os
import uuid

import requests
from airflow import DAG
from airflow.operators.python import PythonOperator
from datetime import datetime, timedelta

DRONE_API_BASE = os.environ.get("DRONE_API_BASE", "http://host.docker.internal:8123")
SERVICE_TOKEN = os.environ.get("DRONE_SERVICE_TOKEN", "")
COMPUTE_URL = f"{DRONE_API_BASE.rstrip('/')}/api/v1/compute/finalize"

default_args = {
    "owner": "drone",
    "depends_on_past": False,
    "retries": 1,
    "retry_delay": timedelta(minutes=2),
}

dag = DAG(
    "drone_finalize",
    default_args=default_args,
    description="Drone species assignment + validation + KMZ export (single node)",
    schedule_interval=None,
    start_date=datetime(2024, 1, 1),
    catchup=False,
    tags=["drone", "tree-crown", "finalize"],
    params={"project_id": "", "run": 1},
)


def call_finalize(**context):
    conf = (context["dag_run"].conf or {}) if context.get("dag_run") else {}
    params = context.get("params", {})
    project_id = conf.get("project_id") or params.get("project_id")
    run = conf.get("run", params.get("run"))
    if not project_id:
        raise ValueError("project_id missing from dag_run conf/params")

    execution_id = str(uuid.uuid4())
    dag_run_id = context["dag_run"].run_id
    payload = {"execution_id": execution_id, "project_id": project_id, "run": run}
    headers = {"Content-Type": "application/json", "Idempotency-Key": dag_run_id}
    if SERVICE_TOKEN:
        headers["X-Service-Token"] = SERVICE_TOKEN

    print(f"[drone_finalize] POST {COMPUTE_URL} project={project_id} run={run} exec={execution_id}")
    resp = requests.post(COMPUTE_URL, json=payload, headers=headers, timeout=7200)
    print(f"[compute] HTTP {resp.status_code}: {resp.text[:500]}")
    # Contract: 200 = success (asset produced); 400/404 = skip (graceful,
    # no dataset); 500 or anything else = hard failure (fail the DAG).
    if resp.status_code == 200:
        data = resp.json()
        context["ti"].xcom_push(key="asset_id", value=data.get("asset_id"))
        return data
    if resp.status_code in (400, 404):
        from airflow.exceptions import AirflowSkipException
        raise AirflowSkipException(f"skipped ({resp.status_code}): {resp.text[:300]}")
    raise Exception(f"compute failed ({resp.status_code}): {resp.text[:300]}")


execute_finalize = PythonOperator(
    task_id="execute_drone_finalize",
    python_callable=call_finalize,
    dag=dag,
)
