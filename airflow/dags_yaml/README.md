# Drone DAGs as YAML (dag-factory)

Two DAGs, `drone_analyze` and `drone_finalize`. The Tree-Crown backend starts
them through the Airflow REST API, and each one makes a single HTTP call back to
the backend, which does all of the computing.

```
backend ──POST /api/v1/dags/drone_analyze/dagRuns──▶ Airflow
        conf: {"project_id", "job_id", "run"}
Airflow ──POST /api/v1/compute/analyze────────────▶ backend   (same for finalize)
```

Tested on Airflow 2.10.5, dag-factory 0.23.0 and apache-airflow-providers-http
5.0.0: analyze → labels → finalize ran end to end against the backend.

## Files to upload

Copy all three into the Airflow `dags/` folder, side by side:

| File | Purpose |
|---|---|
| `drone_analyze.yaml` | DAG `drone_analyze` → `POST /api/v1/compute/analyze` |
| `drone_finalize.yaml` | DAG `drone_finalize` → `POST /api/v1/compute/finalize` |
| `load_drone_dags.py` | Loader. Airflow does not read YAML itself; this turns the two files into DAGs |

Do not also upload `airflow/dags/drone_*_dag.py`. They define the same DAG ids.

## One-time Airflow setup

1. **Packages** on the scheduler and the workers:
   ```bash
   pip install "dag-factory<1" apache-airflow-providers-http
   ```
   Use `dag-factory<1` on Airflow 2; 1.x changes the YAML schema.

2. **Connection** `drone_api`, the backend's address as the workers see it:
   ```bash
   airflow connections add drone_api --conn-uri http://<backend-host>:8200
   ```
   Port 8200 is the backend's single port (UI and API).

3. **Variable** `drone_service_token`, but only if the backend sets
   `TCP_COMPUTE_TOKEN`. The values must match:
   ```bash
   airflow variables set drone_service_token '<same value as TCP_COMPUTE_TOKEN>'
   ```
   Leave it unset if the backend has no token.

4. **Proxy.** If the workers have `HTTP_PROXY`/`HTTPS_PROXY` set, add the
   backend host to `NO_PROXY`. Otherwise the callback goes through the proxy
   and fails.

## Backend side (`.env`)

```bash
TCP_AIRFLOW_BASE_URL=http://<airflow-host>:8080
TCP_ANALYZE_DAG_ID=drone_analyze
TCP_FINALIZE_DAG_ID=drone_finalize
TCP_AIRFLOW_USERNAME=...
TCP_AIRFLOW_PASSWORD=...
```

## Behaviour

- **Success:** HTTP 200. The response, including `asset_id`, is in the task log.
- **Failure:** any other status fails the task, after one retry 2 minutes later.
  This includes the 400/404 that the old Python DAGs turned into a skip;
  dag-factory cannot attach a custom response check to `HttpOperator`. The
  backend tracks progress in its own Job rows and never reads the DAG's
  outcome, so this only affects what Airflow's UI shows.
- **Retries are safe:** `Idempotency-Key` is the dag run id, so retrying a
  run that already succeeded returns the stored result without recomputing.
- **Timeouts:** the HTTP call may stay open for 2 hours. The backend's own
  limits (`TCP_ANALYZE_TIMEOUT_MIN`, `TCP_FINALIZE_TIMEOUT_MIN`) usually end a
  run first.

## Troubleshooting

- **DAGs missing from the UI with no import error.** dag-factory logs a bad
  YAML file as an error in the scheduler/DAG-processor log and loads nothing
  for it. Check that log.
- **`401` in the task log.** `drone_service_token` does not match
  `TCP_COMPUTE_TOKEN`.
- **Connection refused / timeout.** Check the `drone_api` host and port, and
  `NO_PROXY`.
- **`content-type` is lower-case in the YAML on purpose.** With
  `Content-Type`, dag-factory replaces the request body with a fixed value
  before Jinja runs, and `project_id` / `run` would never be filled in.
