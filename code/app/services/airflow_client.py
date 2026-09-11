"""Small Airflow REST client used by the /runs/* trigger endpoints.

Starting a DAG run is the only operation needed. It uses ``urllib`` from the
standard library, so the API process gains no new dependency. Authentication is
optional: with no credentials configured, the request goes out unauthenticated.

The Airflow v1 REST call being made:
    POST {base}/api/v1/dags/{dag_id}/dagRuns
    body: {"conf": {...}}
    -> {"dag_run_id": "...", "state": "queued", ...}
"""
import base64
import json
import urllib.error
import urllib.request

from app.core.logging import ERROR_CODES, classify_conn_error, get_logger
from app.core.settings import settings

log = get_logger("app.airflow")


def airflow_enabled() -> bool:
    """True when an Airflow base URL is set; otherwise the compute runs here."""
    return bool((settings.airflow_base_url or "").strip())


def _auth_header() -> dict:
    """Build the auth header. A bearer token takes priority over basic auth, and
    both are optional."""
    if settings.airflow_auth_token:
        return {"Authorization": f"Bearer {settings.airflow_auth_token}"}
    if settings.airflow_username:
        raw = f"{settings.airflow_username}:{settings.airflow_password or ''}".encode()
        return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
    return {}


def trigger_dag(dag_id: str, conf: dict, timeout: int = 30) -> str:
    """Start a DAG run and return its ``dag_run_id``.

    Raises ``RuntimeError`` with a readable message on any HTTP or connection
    error, so the caller can return a 502 to the client.
    """
    base = settings.airflow_base_url.rstrip("/")
    url = f"{base}/api/v1/dags/{dag_id}/dagRuns"
    body = json.dumps({"conf": conf or {}}).encode()

    headers = {"Content-Type": "application/json", **_auth_header()}
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode() or "{}")
        return data.get("dag_run_id", "")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        log.error("Airflow HTTP error triggering %s: %s", dag_id, e.code, exc_info=True)
        err = RuntimeError(f"Airflow returned {e.code} for {dag_id}: {detail}")
        err.code = ERROR_CODES["AIRFLOW_HTTP_ERROR"]
        raise err from e
    except urllib.error.URLError as e:
        reason = classify_conn_error(e, timeout=timeout)
        log.error("Airflow unreachable at %s: %s", base, reason, exc_info=True)
        err = RuntimeError(f"Airflow unreachable at {base}: {reason}")
        err.code = ERROR_CODES["AIRFLOW_UNREACHABLE"]
        raise err from e


def trigger_drone_dag(conf: dict, timeout: int = 30) -> str:
    """Start the combined drone_pipeline DAG and return its dag_run_id."""
    return trigger_dag(settings.drone_dag_id, conf, timeout=timeout)


def get_dag_run_state(dag_run_id: str, dag_id: str | None = None, timeout: int = 10) -> str:
    """Return a DAG run's state: queued, running, success or failed.

    ``dag_id`` must name the DAG the run was started under. It defaults to the
    combined drone DAG, which is what ``trigger_drone_dag`` uses.
    """
    base = settings.airflow_base_url.rstrip("/")
    dag_id = dag_id or settings.drone_dag_id
    url = f"{base}/api/v1/dags/{dag_id}/dagRuns/{dag_run_id}"

    headers = {"Content-Type": "application/json", **_auth_header()}
    req = urllib.request.Request(url, method="GET", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode() or "{}")
        return data.get("state", "unknown")
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        log.error("Airflow HTTP error polling %s: %s", dag_run_id, e.code, exc_info=True)
        err = RuntimeError(f"Airflow returned {e.code} for {dag_id}: {detail}")
        err.code = ERROR_CODES["AIRFLOW_HTTP_ERROR"]
        raise err from e
    except urllib.error.URLError as e:
        reason = classify_conn_error(e, timeout=timeout)
        log.error("Airflow unreachable at %s: %s", base, reason, exc_info=True)
        err = RuntimeError(f"Airflow unreachable at {base}: {reason}")
        err.code = ERROR_CODES["AIRFLOW_UNREACHABLE"]
        raise err from e
