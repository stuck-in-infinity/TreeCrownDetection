# Version 1.0 — Changelog

Author : Susmit

| Section | Contents |
|---|---|
| [1. Code changes](#1-code-changes) | every code-level change, file by file |
| [2. Docker changes & variables](#2-docker-changes--variables) | `docker-compose.hub.yml` only |
| [3. `.env` and `frontend/config.js`](#3-env-and-frontendconfigjs) | every value, and what to put in it |

---

## 1. Code changes

### 1.1 New modules

**`code/app/core/logging.py`** — new, 316 lines. Central logging + the error-code catalog.

- ContextVars `request_id_var`, `job_id_var`, `project_id_var`, `dag_run_id_var`, `stage_var`; `with_context(...)` binds them for a block.
- `new_request_id()` mints the correlation id; `"-"` is the "no request scope" sentinel.
- `configure_logging(force=False)` installs a rotating `app.log` + an ERROR-only `errors.jsonl` under `TCP_LOG_DIR`, plus a stdout handler bound to the *original* `sys.stdout` object (so the worker's stream proxy cannot detach container logging).
- JSON formatter (`TCP_LOG_JSON=true`) emitting one object per line with the bound context ids.
- `naive_now()`, `now_ist()`, `naive_from_ts()` — the timezone helpers that replace `datetime.utcnow` everywhere.
- `ERROR_CODES` — the canonical machine-code catalog: `CONFLICT_BUSY`, `INVALID_STATE`, `MISSING_PARAM`, `NO_ORTHO`, `NO_LABELS`, `AIRFLOW_UNREACHABLE`, `AIRFLOW_HTTP_ERROR`, `FILEBROWSER_AUTH`, `FILEBROWSER_UNREACHABLE`, `FILEBROWSER_SHARE_FAILED`.
- `classify_conn_error(exc, timeout=…)` — turns a `urllib.error.URLError` into a readable reason (DNS / refused / timed out).

**`code/app/services/job_claim.py`** — new, 117 lines. Exclusive claim on the right to compute a run.

- `compute_key(raw)` → namespaces the incoming `Idempotency-Key` as `compute:<id>`, so an inline compute job is distinguishable from the placeholder Job that `runs.py` creates when it hands off to Airflow.
- `find_prior(db, project, key)` → the previous Job for that key (replay lookup).
- `claim(db, project, key, job_type, request_id=…)` → `(job, outcome)` where outcome is `WON` / `REPLAY` / `DUPLICATE` / `ACTIVE`. The INSERT against the unique `(project_id, celery_task_id)` index is the arbiter; `IntegrityError` means someone else won.
- Rationale encoded in the module docstring: the project state machine *cannot* arbitrate, because the HTTP trigger has already moved the project into `ANALYZING`/`FINALIZING` before Airflow calls back, so a conditional UPDATE onto that same state succeeds for every caller.

**`code/app/services/activity_log.py`** — new, 69 lines. Per-user audit ledger.

- `append(email=…, user_id=…, action=…, method=…, path=…, project_id=…, status=…, request_id=…, client_ip=…)` writes one JSON line to `<storage_root>/activity/activity-YYYY-MM-DD.jsonl` (one file per UTC day).
- Best-effort — every failure is swallowed; it never raises into the request path.

**`code/scripts/run_retention.py`** — new, 199 lines. Consent-aware retention driver, run from system cron (not Celery beat).

- consent `0` → whole project folder + DB row deleted.
- consent `1` → retained (`TCP_RETAIN_CONSENT_ALL`).
- consent `2` → keep everything through Step 1, delete `step2/3/4` + labels, stamp `Project.pruned_at` as the idempotency marker.

**`code/scripts/prep_lowres.py`** — new, 108 lines. Input prep: strips the alpha channel from 4-band RGBA orthos and writes `nodata=0`. Nothing downstream handles a 4-band raster, so an RGBA ortho otherwise corrupts tiling silently.

---

### 1.2 Authentication — Google sign-in

**`code/app/api/deps.py`**

- `require_api_key()` — signature extended with `x_user_email: str | None = Header(alias="X-User-Email")`. Two independent optional checks now: `settings.api_key` set → `X-API-Key` must match (legacy); `settings.auth_enabled` True → `X-User-Email` must be present, else 401 `UNAUTHENTICATED`.
- Return value changed from the hard-coded `"default"` to `x_user_email or "default"` — so it doubles as `Project.user_id`.
- **New** `require_user(x_user_email, x_user_id)` — the human-identity dependency. `auth_enabled` False → returns the email if present else `"default"` (keeps dev/tests open). True → missing email is 401.
- `get_project()` switched from `Depends(require_api_key)` to `Depends(require_user)`, so the signed-in email is what `resolve_project` scopes projects by.
- The Google token is **not** verified server-side; the header is trusted for audit and ownership only.

**`code/app/core/settings.py`**

- New `auth_enabled: bool = False`.
- New `google_client_id: str | None = None` (FYI only — the real public client id lives in `frontend/config.js`).

**`code/app/main.py`** — audit middleware

- `audit_requests` HTTP middleware added: mints/reuses `X-Request-Id`, binds it via `with_context`, resolves the client IP from `X-Forwarded-For` (first hop) or `request.client.host`, times the request, and logs `METHOD path -> status (Nms)` at INFO (ERROR for ≥500).
- Writes the audit ledger entry via `activity_log.append` for `POST/PUT/PATCH/DELETE` on `/api/` paths only.
- `_COMPUTE_PATH_PREFIXES` + `_is_compute_path()` — the Airflow-facing callbacks (`/api/v1/compute/*`, `/api/v1/project/drone_api`, `/drone_status`, `/analyze`, `/finalize`, and `/api/v1/projects/{id}/analyze|finalize`) never get the `X-Request-Id` response header, keeping their bodies/headers byte-identical to the Airflow contract.
- `configure_logging()` called first thing in the `lifespan` startup, before `init_db()`.

**`frontend/index.html`** — the gate and the token flow

- New `#auth-gate` full-screen overlay (the "landing_I_grid" plate) with an animated canopy-detection canvas: `initGateCanvas()`, `setupCanopy()`, `setupCrowns()`, `draw()`, `loop()`, `resize()`, `inPoly()`.
- `initGis()` — polls until the GIS SDK is defined, then `google.accounts.oauth2.initTokenClient({client_id, scope})`.
- `requestLogin()` — opens the consent popup; the callback stores the access token.
- `fetchUserInfo()` — `GET https://www.googleapis.com/oauth2/v3/userinfo` to resolve the email.
- `onSignedIn()` — drops the gate, sets `#whoami`, calls `loadMyProjects()`.
- `signOut()` — `google.accounts.oauth2.revoke`.
- `authHeaders()` / `headers()` — every API call now carries `X-User-Email` and `X-User-Id`.
- `gateError(msg)` → `#gateErr`.
- **Working tree, uncommitted:** `guestLogin()` + a `#guestBtn` "Continue as guest" button that sets `AUTH.email = "guest@guest.local"`, `AUTH.userId = "guest"` and calls `onSignedIn()` directly. The per-session random-suffix identity is written but commented out, so **every guest shares one identity** and therefore sees and can act on every other guest's projects. Decide before tagging: uncomment the random suffix, or delete the button.

**`frontend/config.js.example`** — new, 18 lines. Committed template for the git-ignored `frontend/config.js` (§3.2).

---

### 1.3 Concurrency — the exclusive compute claim

**`code/app/db/models.py`**

- `Job.__table_args__` — new `UniqueConstraint("project_id", "celery_task_id", name="uq_jobs_project_task")`. This index is the arbiter (NULLs compare distinct, so pre-dispatch jobs are unaffected).
- `Job.request_id: str | None` — new column, the correlation id of the HTTP request that created the job.
- `Project.consent: int` (default 0), `Project.consent_at: datetime | None`, `Project.pruned_at: datetime | None` — new columns.
- `created_at` / `updated_at` defaults changed `datetime.utcnow` → `naive_now`.
- Module docstring documents the two transient claim states, `UPLOADING` and `DELETING`.

**`code/app/db/session.py`**

- `_connect_args` for SQLite now `{"check_same_thread": False, "timeout": 30}` — the default 5 s busy timeout is too short while a worker thread commits job progress.
- New `@event.listens_for(engine, "connect")` `_sqlite_pragmas()` setting `journal_mode=WAL`, `synchronous=NORMAL`, `busy_timeout=30000`, so status polls no longer fail with "database is locked" during a run.
- `_migrate_sqlite_add_columns()` extended: `projects.consent`, `projects.consent_at`, `projects.pruned_at`, and a new `jobs` table entry for `request_id`.
- New `_migrate_sqlite_unique_job_key()` — `create_all` only creates missing *tables*, so an existing `jobs` table would never get the constraint. Clears `celery_task_id` on historic duplicate rows (keeper = the SUCCEEDED one, else the most recent) then creates `uq_jobs_project_task`. Called from `init_db()`.

**`code/app/api/v1/compute.py`** (the Airflow callbacks)

- `_prior_job()` deleted; replaced by `job_claim.find_prior` + a new `_claim(db, project, key, job_type)` returning `(job, rejection_response)`.
- Key construction moved to `job_claim.compute_key(idempotency_key or req.execution_id)`.
- Losers now get **400 `INVALID_STATE`**, not 409 — the DAGs map 400 to `AirflowSkipException`, so a duplicate callback is a graceful skip rather than a task failure.
- Replay (`prior.state == "SUCCEEDED"`) returns the success payload without recomputing, now tagged `stage="analyze"` / `stage="finalize"`.
- Failures log `compute analyze|finalize failed project=… job=… stage=…` with `exc_info=True` before returning 500 `COMPUTE_FAILED`.
- New `_current_request_id()` helper reading `request_id_var` (returns `None` for the `"-"` sentinel).

**`code/app/api/v1/analyze.py`** and **`code/app/api/v1/finalize.py`** (the human trigger path)

- The inline `db.query(models.Job).filter_by(...)` idempotency block deleted from both; replaced by `job_claim.compute_key` + `find_prior` + `job_claim.claim(...)`.
- The claim is now taken **before** state is touched and before `_apply_run_config` runs, because `_apply_run_config` can archive the run and bump `current_run`.
- Lost claim → 409 with `CONFLICT_BUSY` and a `hint`, message differing for `DUPLICATE` vs `ACTIVE`.
- Failure roll-back changed from a blind `project.state = previous_state` to `transition_if(db, project, {"ANALYZING"}, previous_state)` (resp. `{"FINALIZING"}`) — it only undoes the state this call claimed.
- The failure message is now read *before* the roll-back, because `transition_if` clears `Project.error`.
- 500 bodies gained `"hint": "see the run's logs/ folder or errors.jsonl by request_id"`.
- `datetime.utcnow` imports removed.

**`code/app/api/v1/runs.py`**

- New `_current_request_id()` and `_classify_dispatch_error(exc)` (maps a dispatch `RuntimeError` to `AIRFLOW_HTTP_ERROR` / `AIRFLOW_UNREACHABLE` plus a remediation hint).
- `_new_job()` now stamps `started_at=naive_now()` and `request_id=_current_request_id()`.
- `_fail_trigger()` — the blind `project.state = "FAILED"` replaced with `transition_if(db, project, _BUSY, "FAILED")`, so it cannot stamp FAILED over a state something else legitimately moved on to.
- `_gate()` — codes taken from `ERROR_CODES`, messages now list the allowed states, `hint` added, and both 409 paths log a WARNING.
- `trigger_analyze` — the "no ortho" 400 changed from generic `BAD_REQUEST` to `NO_ORTHO` + hint.
- `trigger_finalize` — missing `project_id` → `MISSING_PARAM`; zero labels → `NO_LABELS`; both with hints.
- Dispatch failures now return the classified code instead of a flat `DISPATCH_FAILED`, and log an ERROR with `exc_info`.
- `_validate_trigger_body()` additionally calls the new `_validate_merged_params(project, body.params)`.

**`code/app/api/v1/projects.py`**

- New transient-state constants `_UPLOADING`, `_DELETING`, `_BUSY_STATES`, `_DELETABLE_STATES`.
- New `_dataset_edit_lock(db, project)` context manager — claims `UPLOADING` with a single conditional UPDATE, releasing to the pre-state (or `CREATED` if the project ended with no ortho). Wraps `upload_ortho`, `upload_ortho_from_url` and `upload_ground_truth`. Closes the check-then-act gap where an analyze trigger could start between `_assert_ortho_unlocked` and the actual write.
- `upload_ortho_from_url` body extracted into `_download_ortho_from_drive(project, db, file_id, gdown)` so it can run under the lock.
- `upload_ground_truth` gained a `db: Session = Depends(get_db)` parameter (it needs the lock).
- `update_project` — claims the state atomically via `transition_if(db, project, {pre_state}, "UPLOADED")` before archiving the run; the `project.state = "UPLOADED"` assignment removed.
- `delete_one` — claims `_DELETING` via `transition_if` before `delete_project_dir`, so a delete can no longer pull the tree out from under a running job.
- `_register_ortho` — final state publish changed to `transition_if(db, project, {_UPLOADING, "CREATED", "UPLOADED"}, "UPLOADED")`.
- `_assert_ortho_unlocked` and `update_project`'s busy check widened from `("ANALYZING","FINALIZING")` to `_BUSY_STATES`.

**`code/app/api/v1/labels.py`**

- `run = project.current_run or 1` pinned at the top of `submit_labels`, and passed to `write_species_map_csv(..., run=run)` — a concurrent re-analyze bumping the counter can no longer redirect the CSV into the new run's folder.
- The final `project.state = "LABELS_SUBMITTED"` assignment replaced with `transition_if(db, project, _LABEL_STATES, "LABELS_SUBMITTED")`; on loss, the just-written `ClusterLabel` rows are deleted and a 409 `CONFLICT_BUSY` is returned.

**`code/app/services/state.py`** — docstring expanded with the two calling rules: `allowed` must be the exact state you validated against, and a self-transition excludes nobody (which is why the compute endpoints claim a Job row instead).

**`code/app/workers/tasks.py`**

- `_Tee` deleted, replaced by `_ThreadRoutedStream` — a process-global stdout/stderr proxy that routes each write to the real stream plus the *calling thread's* registered sink. `contextlib.redirect_stdout` was unusable here: jobs run concurrently in-process (daemon thread for local dispatch, request threadpool for `/compute/*`), so it interleaved logs and, unwinding out of order, restored a closed file that silently swallowed every later write in the process.
- `_proxy(name)` installs the proxy lazily and once; `_redirect(logf)` is now a thread-scoped context manager built on `push`/`pop`.
- `os.environ.setdefault("TQDM_DISABLE", "1")` at module import, before the lazy pipeline imports — redirected to a file, tqdm's `\r` updates don't overwrite, bloating run logs ~10×.
- `_MODEL_LOCK` added: the predictor / DINOv2 cache **miss** path is serialised, so two concurrent jobs missing the same key can't both allocate GPU memory and OOM.
- `_get_predictor()` signature extended with `detections_per_image` and `min_size_test`, **and both added to the cache key** — they are baked into the predictor at construction, so omitting them would make two projects with different values silently share whichever predictor was built first.
- `job_a_analyze` / `job_b_finalize` bodies wrapped in `with_context(job_id=…, project_id=…, dag_run_id=job.celery_task_id, stage=…)`, bound *inside* the task body because ContextVars don't inherit across threads.
- `run_detectree2_pipeline` now called with `area_min`, `area_max`, `full_coverage` from the config.
- Every `datetime.utcnow()` → `naive_now()`.
- `_fail()` — logs a structured ERROR (stage, project, `duration_ms`, `exc_info`) and calls the new `_write_failure_to_log()`, which appends a marked timestamp + exception + traceback block to the run's own `.log` file so it is self-sufficient for RCA.
- STAC emission failure changed from `print(...)` to `log.warning("stac emission skipped", exc_info=True)`.

**`code/app/workers/celery_app.py`** — `configure_logging()` at import, plus a `worker_process_init` signal handler calling `configure_logging(force=True)` (prefork forks drop handlers).

**`code/app/workers/cleanup.py`** — `datetime.utcnow()` → `naive_now()`, `datetime.utcfromtimestamp()` → `naive_from_ts()`.

---

### 1.4 Pipeline parameters

**`code/app/schemas/project.py`** — `PipelineParams` now carries real bounds and new fields.

| Field | Change |
|---|---|
| `tile_size` | `Field(default=10, ge=1, le=1000)` |
| `buffer` | `Field(default=10, ge=0, le=500)` |
| `iou_threshold` | `Field(default=0.9, ge=0.0, le=1.0)` |
| `conf_threshold` | `Field(default=0.85, ge=0.0, le=1.0)` |
| `detections_per_image` | **new** — `Field(default=6, ge=1, le=500)`; cap on crowns per tile, silently discards the excess |
| `min_size_test` | **new** — `Field(default=512, ge=256, le=2048)`; `pixels_per_metre = min_size_test / (tile_size + 2*buffer)` |
| `area_min` | **new** — `Field(default=4.0, ge=0.0, le=100000.0)`, ortho-CRS units, exclusive |
| `area_max` | **new** — `Field(default=2000.0, ge=1.0, le=100000.0)` |
| `full_coverage` | **new** — `bool = False`; False skips the right/bottom remainder strip |
| `pca_components` | `Field(default=50, ge=2, le=768)` |
| `batch_size` | default **16 → 64**; deliberately not exposed in the UI (VRAM-bound) |

- New `@model_validator(mode="after") _check_cross_field()` enforcing `area_min < area_max`.

**`code/app/api/v1/projects.py` — validation**

- `_validate_param_overrides()` fixed: it validated `fields[key].annotation`, which is the *bare* type — pydantic keeps `ge/le/gt/lt` on the `FieldInfo`, so every declared bound was silently ignored. Now builds `Annotated[fields[key].annotation, fields[key]]` so the constraints actually enforce.
- New `_validate_merged_params(project, overrides)` — applies the cross-field rules to the params as they will be **stored**, not just to the incoming overrides. Sending only `area_min: 5000` passes every per-field bound while still producing an invalid pair against a stored `area_max` of 2000. Called from both write paths (`update_project` and `runs._validate_trigger_body`), and drops unknown keys first so legacy params can't break the check.

**`code/app/services/pipeline_adapter.py`**

- `build_config()` maps the five new params onto `cfg.DETECTIONS_PER_IMAGE`, `cfg.MIN_SIZE_TEST`, `cfg.AREA_MIN`, `cfg.AREA_MAX`, `cfg.FULL_COVERAGE`, with defaults mirroring `predict.py`'s `DEFAULT_*` constants so an older project without those keys behaves exactly as before.
- `cfg.BATCH_SIZE` default 16 → 64.
- `write_species_map_csv(project, chosen_k, mapping, run=None)` — new `run` parameter pinning the target run folder.

**`code/predict.py`**

- `matplotlib.use("Agg")` set before importing `pyplot` — headless, no GUI event loop, jobs run off-thread.
- `downsample_image(..., scale=0.3)` → `scale=1` default.
- **New** `get_ortho_gsd(ortho_path)` — reads native metres/pixel from the raster's own geotransform.
- **New** `TARGET_EFFECTIVE_GSD_M = 0.02 / 0.3` and `compute_downsample_scale(ortho_path, target_gsd_m)` — the downsample scale is now derived per-ortho from its native GSD instead of a flat 0.3, clamped to 1.0 so we never upsample. Calibrated off Sanjay Van's *true* rasterio-measured GSD (2.04 cm/px), not the nominal 2.5 cm — using the nominal figure regressed that site from 6.8 cm to 8.33 cm effective.
- New `DEFAULT_DETECTIONS_PER_IMAGE=6`, `DEFAULT_MIN_SIZE_TEST=512`, `DEFAULT_AREA_MIN=4`, `DEFAULT_AREA_MAX=2000`, `DEFAULT_FULL_COVERAGE=False` constants.
- `build_predictor(model_path, conf_threshold, detections_per_image, min_size_test)` — the two hardcoded values (`DETECTIONS_PER_IMAGE = 6`, `MIN_SIZE_TEST/MAX_SIZE_TEST = 512`) lifted to parameters.
- `run_detectree2_pipeline(...)` gained `target_gsd_m`, `area_min`, `area_max`, `full_coverage`, `detections_per_image`, `min_size_test`.
- Area filter changed from the hardcoded `(area > 4) & (area < 200)` to `(area > area_min) & (area < area_max)` — note the old upper bound was **200**, the new default is **2000**.
- Overlay save made figure-scoped: `ax.axis("off")`, `fig.savefig(...)`, `plt.close(fig)` — pyplot's "current figure" is process-global and concurrent jobs were saving each other's plots.

**`code/tree_crown_pipeline.py`**

- `matplotlib.use('Agg')` before the `pyplot` import.
- `step1_analyze_k` and `step1_tsne` switched from `plt.suptitle/tight_layout/savefig/close` to the figure-scoped `fig.*` equivalents and `plt.close(fig)`.
- `plt.cm.tab10(...)` → `matplotlib.colormaps['tab10'](...)` (deprecated accessor).

**`code/models.yaml`** — two detectors added:

```yaml
  flexi:
    file: 250312_flexi.pth
    description: Generalist — urban + closed canopy
  tropical_closed:
    file: 250711_tropical_closed_canopy.pth
    description: Dense tropical forest only.
```

Both `.pth` files must exist in `HOST_MODELS_DIR` (§2.2).

---

### 1.5 FileBrowser integration

**`code/app/services/filebrowser_client.py`**

- `filebrowser_enabled()` — the gate; true when `TCP_FILEBROWSER_BASE_URL` is non-blank.
- `_get_token()` — `POST /api/login` with username/password, returns the JWT.
- `create_project_share(project_id)` — `POST /api/share/<project_id>` with the `X-Auth` token header; returns the permanent share hash (e.g. `fNqIKDS3`). **The project id doubles as the path inside FileBrowser's `/srv`** — this is the path contract in §2.3.
- `share_url(hash)` — builds the user-facing URL from `filebrowser_public_url`, deliberately a separate setting from the internal one the backend calls.
- Error handling split by exception type; every failure path raises a `RuntimeError` carrying a `.code`: `HTTPError` on login → `FILEBROWSER_AUTH`, `URLError` → `FILEBROWSER_UNREACHABLE` (with `classify_conn_error` giving the reason), share-API `HTTPError` → `FILEBROWSER_SHARE_FAILED`. Each logs an ERROR with `exc_info`.

**`code/app/db/models.py`** — `Project.share_hash: str | None` persists the hash so the share is created once, not per request.

**`code/app/api/v1/projects.py`**

- On project creation: `project.share_hash = create_project_share(project.id)`.
- New `GET /projects/mine` — lists the signed-in user's projects with `project_id`, `name`, `state`, `run_name`, `updated_at` and `files_url` (`share_url(hash)` when FileBrowser is on). Powers the landing page's "your past runs" list.
- Project responses emit `files_url`, and the field rides along in every analyze / finalize response.

**`code/app/core/settings.py`** — four settings: `filebrowser_base_url`, `filebrowser_public_url`, `filebrowser_username`, `filebrowser_password`.

**`frontend/index.html`** — the share is consumed, not just linked. The cluster-review UI reads artifacts straight out of it:

```js
function fbFromShareUrl(shareUrl){          // {public}/share/{hash} -> {base, hash}
  const m = (shareUrl||"").match(/^(.*)\/share\/([^/?#]+)/);
  return m ? { base:m[1], hash:m[2] } : null;
}
function fbRaw(fb, path){  return `${fb.base}/api/public/dl/${fb.hash}/${path}`; }
function fbView(fb, path){ return `${fb.base}/share/${fb.hash}/${path}`; }
```

Cluster plots render as `<img>` off `fbRaw`; the per-k CSV is fetched and drawn as a table by `csvToTable()` / `parseCsvLine()`. **This is why `TCP_FILEBROWSER_PUBLIC_URL` must be reachable from the user's browser** — a container-internal hostname there makes the review step render empty even though the pipeline succeeded.

---

### 1.6 Airflow integration

**`code/app/services/airflow_client.py`**

- `trigger_dag()` and `get_dag_run_state()` — both `except` branches rewritten: `HTTPError` → `RuntimeError` with `.code = AIRFLOW_HTTP_ERROR`, `URLError` → `.code = AIRFLOW_UNREACHABLE` with the reason from `classify_conn_error(e, timeout=…)`. Each logs an ERROR with `exc_info`.
- Uses `urllib`, which **honours `http_proxy`** — every internal Airflow hostname must be in `no_proxy` (§3.1).

**`code/app/core/settings.py`** — Airflow fields in use: `airflow_base_url`, `airflow_username`, `airflow_password`, `airflow_auth_token`, `analyze_dag_id` (`drone_analyze`), `finalize_dag_id` (`drone_finalize`), `drone_dag_id` (`drone_pipeline`, the unified DAG the current frontend triggers via `/project/drone_api`).

**DAG-side environment** — read at import time by `airflow/dags/drone_analyze_dag.py:30-31` and `drone_finalize_dag.py:26-27`. These live on the **Airflow host**, not in this repo's `.env`:

| Variable | Purpose |
|---|---|
| `DRONE_API_BASE` | Airflow → our backend. Defaults to `http://host.docker.internal:8123` if unset. |
| `DRONE_SERVICE_TOKEN` | Sent as `X-Service-Token` on the compute callbacks; must equal `TCP_COMPUTE_TOKEN`. If empty the header is omitted entirely, which fails closed against a backend that has the token set — every DAG run then 401s at the callback. |

The DAGs send `Idempotency-Key: <dag_run_id>` on every callback, stable across Airflow retries. That is what makes a retry replay rather than recompute, and it is the key the `(project_id, celery_task_id)` unique index arbitrates on (§1.3).

**DAG id note.** This repo ships only the two split DAGs, `drone_analyze` and `drone_finalize`. The unified `drone_pipeline` DAG that `TCP_DRONE_DAG_ID` names — the one the current frontend actually triggers — is **not in this repo** and must already exist on the Airflow host. If it is missing, every analyze and finalize fails at the trigger with a 502 `AIRFLOW_TRIGGER_FAILED`.

---

### 1.7 Errors, results, STAC

**`code/app/core/errors.py`**

- `ApiError.__init__` gained a `hint=None` keyword; `_envelope()` gained a `hint` parameter added to the body only when supplied, so the shape is unchanged for callers that don't pass one.
- `_http_exception_handler` forwards `detail.get("hint")`.
- All three handlers now log: ERROR for ≥500, WARNING for 4xx, `log.exception(...)` for unhandled.

**`code/app/core/storage.py`** — new `prune_labelled_outputs(project_id, run=1)` deleting only `step2/3/4_output` for consent=2 retention; idempotent, returns the list of directories removed.

**`code/app/api/v1/results.py`**

- New `ConsentBody` model and `POST /projects/{id}/consent` (+ `/project/consent`) — accepts `0` (no) / `1` (all) / `2` (unlabelled only), requires the project to be COMPLETED, stamps `consent` + `consent_at = naive_now()`.
- `build_results_payload()` now emits `consent` and `consent_at`.

**`code/app/api/v1/clustering.py`** — `detection_overlay_url` changed from an API URL (`{base}/api/v1/project/detection/overlay.png`) to the **storage-relative file path** via the new `relative_artifact_path()` (e.g. `projects/<id>/work/run_<n>/detectree/S3C/overlay.png`), or `None` when absent — so the frontend resolves it through the FileBrowser share.

**`code/app/services/stac.py`**

- `build_stac_item(project, chosen_k=None, run=None, stage=None)` — new `stage` parameter appended to the item id (`..._run<n>_analyze` / `_finalize`) so the two compute phases emit distinct, non-colliding items. `None` keeps the legacy id.
- New `_footprint_from_geojson(geojson_path)` — fallback footprint (geometry + bbox) parsed out of the crown GeoJSON, which the pipeline already reprojects to EPSG:4326. Used when the ortho GeoTIFF has no embedded CRS; returns `(None, None)` on any failure so STAC emission never blocks finalize.
- `input_parameters` extended with `detections_per_image`, `min_size_test`, `area_min`, `area_max`, `full_coverage`; `batch_size` default 16 → 64.
- `write_stac_item()` now tags `stage="finalize"`.

**`code/app/services/assets.py`** — `asset_response_fields()`, `stac_response()` and `analyze_asset_fields()` all thread a `stage` argument through to `build_stac_item`.

---

### 1.8 Frontend (beyond auth)

`frontend/index.html`, +1,787 lines across the range.

- **Cluster review** — `renderClusterReview()`, `showKView()`, `csvToTable()`, `parseCsvLine()`, `clusteringPath()`; per-k plots + CSV pulled from the FileBrowser share (§1.5).
- **Parameter form** — `paramCard()`, `validateParams()`, `paramErrors()`, `renderHints()`, `backboneImgSize()`; new inputs `#p_det`, `#p_msize`, `#p_amin`, `#p_amax`, `#p_fullcov` for the five new params, plus `#p_tile`, `#p_buf`, `#p_iou`, `#p_conf`, `#p_pca`, `#klist`, `#modelKey`, `#backbone`, `#p_epsg`, `#epsgFallback`.
- **Zoom guide** — `updateGuideZoom()`, `updateZoomNote()`, `renderGuide()`, `#gz_tile` / `#gz_buf` / `#gz_size` / `#gz_out`, `#guide-page`, `#guideToc`, `#guideCards`, `#guideSymptoms`.
- **Past runs / routing** — `loadMyProjects()` (against `GET /projects/mine`), `#pastRuns`, `route()`, `newAnalysisFlow()`, `rerunFlow()`, `runDate()`.
- **Consent** — `consentBoxHtml()`, `submitConsent()`, `#consentBox`, `#consentBtn`, `#consentMsg`.
- **Progress / status** — `#astage`, `#aprog`, `#fstage`, `#fprog`, `#status-string`, `#lockNote`, `#runinfo`.
- Timezone: run timestamps rendered in IST to match the backend's `now_ist`.

---

## 2. Docker changes & variables

Everything here refers to **`docker-compose.hub.yml`** — the pull-and-run path. `docker-compose.yml` (local build) is not used in this deployment.

### 2.1 What changed in the compose file

| Change | Before | After |
|---|---|---|
| API image tag | `uavforaliens/treecrown-workstation:latest` | `uavforaliens/treecrown-workstation:cu128` |
| GPU | *(none)* | `deploy.resources.reservations.devices` — nvidia, `count: all`, `capabilities: [gpu]` |
| FileBrowser host port | `8097:80` | `8098:80` |
| FileBrowser service | *(added in this range)* | `filebrowser/filebrowser`, mounts `./data/storage/projects:/srv` + named volume `filebrowser_db:/database` |
| Top-of-file | — | the whole pre-GPU CPU stack is retained commented out above the live block; **out of scope**, do not uncomment |

The GPU block added to the `api` service:

```yaml
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]
```

**The `:cu128` tag is only a label.** The CUDA variant comes from the torch wheel index, not a CUDA base image — the Dockerfile still uses `FROM python:3.10-slim` with `ARG TORCH_INDEX` defaulting to the **CPU** index. A build that omits `TORCH_INDEX` produces a CPU image wearing a `:cu128` tag, which starts fine and then runs the whole pipeline on CPU. (`TORCH_INDEX` is a *build* arg — it matters only if you rebuild; the hub path just pulls.)

**Host prerequisites for the GPU reservation:**
- NVIDIA driver supporting CUDA 12.8
- `nvidia-container-toolkit` installed and the Docker daemon configured for it
- DINOv2 weights pre-cached under `./data/hf-cache`, because `HF_HUB_OFFLINE=1` means the first run **fails** rather than downloading

### 2.2 Variables Compose itself reads

These are interpolated by Docker Compose, so they must be in `.env` at the repo root — the same file `env_file: .env` injects into the `api` container.

| Variable | Value | Notes |
|---|---|---|
| `IMAGE_API` | `uavforaliens/treecrown-workstation:cu128` | must be the `cu128` tag, not `latest` |
| `IMAGE_FRONTEND` | `anunay12/treecrown-frontend:latest` | stays `:latest` — nginx serving static files, no CUDA dependency |
| `HOST_MODELS_DIR` | absolute host path to the detector `.pth` folder, e.g. `/home/susmit/development/drone_docker/models` | mounted read-only at `/models`. Compose uses `:?`, so an unset value **aborts the run** with the error message baked into the file. Must contain every `file:` named in `code/models.yaml`, including the two new ones: `250312_flexi.pth` and `250711_tropical_closed_canopy.pth`. |

Fixed in the compose file, **not** env-driven: the `8123:8000`, `8200:80` and `8098:80` port mappings; the `./code:/code`, `./data:/data` and `./data/storage/projects:/srv` bind mounts; the `filebrowser_db` named volume; `extra_hosts: host.docker.internal:host-gateway`; and the NVIDIA `deploy` block.

### 2.3 FileBrowser — path contract

FileBrowser runs as a production service, started outside this bundle. Nothing needs to be launched from our end; what must line up is **the paths**, and there is exactly one rule:

> **FileBrowser's `/srv` must be the same directory as the backend's `<TCP_STORAGE_ROOT>/projects`.**

The backend calls `POST /api/share/<project_id>` with no path prefix, so the project id **is** the path relative to FileBrowser's root. If `/srv` points anywhere else, share creation returns 404/500 and every project's `files_url` is dead.

| Side | Path | Must be |
|---|---|---|
| Host / mounted drive | `<repo>/data/storage/projects/` | the real directory holding `<project_id>/` folders — this is what the drive must be mounted at, or symlinked to |
| API container | `/data/storage/projects/` | `./data:/data` + `TCP_STORAGE_ROOT=/data/storage` |
| FileBrowser container | `/srv/` | bind the **same host directory** here |
| Resulting share path | `/srv/<project_id>/work/run_<n>/…` | what `fbRaw` / `fbView` fetch |

If FileBrowser is managed elsewhere, its mount must resolve to that same host directory (`<repo>/data/storage/projects`); if the storage lives on a mounted drive, mount the drive there so both containers see one path, rather than giving each its own copy. Then either delete the `filebrowser` service block from `docker-compose.hub.yml` or leave it — it is harmless, but if you keep it, do not point `TCP_FILEBROWSER_BASE_URL` at the production instance while a second local one is also running on `:8098`.

The `filebrowser_db` named volume holds the user database and, with it, every share hash ever issued. **Deleting that volume invalidates every `files_url` already handed out** — the `Project.share_hash` rows survive but no longer resolve.

### 2.4 Port map

| Service | Container | Host | Notes |
|---|---|---|---|
| `api` | 8000 | **8123** | the port Airflow calls back on |
| `frontend` | 80 | **8200** | nginx, static |
| `filebrowser` | 80 | **8098** | was 8097 |
| Airflow | — | 8080 | **external** — not in this compose bundle |

Both directions must be open: the API container reaches Airflow on `:8080` (via `host.docker.internal`, provided by `extra_hosts`), and the Airflow worker reaches the API on `:8123`.

### 2.5 Run and verify

```bash
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d

curl -f http://localhost:8123/livez                                  # API up
docker compose -f docker-compose.hub.yml exec api \
  python -c "import torch; print(torch.cuda.is_available())"         # must print True
docker compose -f docker-compose.hub.yml exec api \
  python -c "from app.services.airflow_client import airflow_enabled; print(airflow_enabled())"
docker compose -f docker-compose.hub.yml exec api \
  python -c "from app.services.filebrowser_client import filebrowser_enabled; print(filebrowser_enabled())"
```

Then create a project through the UI at `http://<workstation-ip>:8200` and confirm the response carries a non-null `files_url` — that single field proves the FileBrowser leg end to end (login, share creation, URL construction).

---

## 3. `.env` and `frontend/config.js`

### 3.1 `.env` — every value

Everything with the `TCP_` prefix maps 1:1 to a field on `Settings` in `code/app/core/settings.py`. `IMAGE_*`, `HOST_MODELS_DIR`, `TORCH_INDEX`, `HF_HUB_OFFLINE` and the proxy block are not `TCP_` settings — they are read by Compose or by third-party libraries.

**In this deployment none of these are optional.** The code does contain fallbacks — in-process compute when Airflow is unset, a FileBrowser no-op when its URL is blank, an open API when auth is off — but leaving a value empty silently degrades to a path this document does not cover.

#### Storage & database

| Variable | Value | What it means |
|---|---|---|
| `TCP_STORAGE_ROOT` | `/data/storage` | Container path for per-project artifacts. Its `projects/` subdirectory is what FileBrowser serves as `/srv` (§2.3). |
| `TCP_DATABASE_URL` | `sqlite:////data/treecrown.db` | Four slashes = absolute path. WAL is enabled automatically in code; point at Postgres if you outgrow single-writer SQLite. |

#### Model catalog

| Variable | Value | What it means |
|---|---|---|
| `HOST_MODELS_DIR` | absolute host path, e.g. `/home/susmit/development/drone_docker/models` | Host side of the `/models` mount. Compose aborts if unset. |
| `TCP_MODELS_DIR` | `/models` | Container side; must match the mount. |
| `TCP_MODELS_MANIFEST` | `/code/models.yaml` | The detector + backbone catalog. |
| `TCP_DEFAULT_MODEL_KEY` | `urban_cambridge` | Must be a key present in `models.yaml`. |

#### Run mode

| Variable | Value | What it means |
|---|---|---|
| `TCP_CELERY_EAGER` | `false` | Compute runs in-process on the Airflow callback; no Redis/Celery worker in this deployment. |
| `HF_HUB_OFFLINE` | `1` | Forces HuggingFace offline. DINOv2 must already be cached under `./data/hf-cache` or the first run fails instead of downloading. |
| `TCP_REDIS_URL` | leave at default | Unused while `celery_eager=false`. |

#### Airflow

| Variable | Value | What it means |
|---|---|---|
| `TCP_AIRFLOW_BASE_URL` | `http://host.docker.internal:8080` (Airflow on the same machine) or `http://<airflow-host>:8080` | Backend → Airflow REST, to start a DAG run. **Currently commented out in `.env` — uncomment it.** Blank means the trigger endpoints run compute in-process and Airflow is never involved. |
| `TCP_DRONE_DAG_ID` | `drone_pipeline` | The unified DAG the current frontend triggers via `/project/drone_api`. This is the one that actually runs — and it is **not in this repo** (§1.6). |
| `TCP_ANALYZE_DAG_ID` | `drone_analyze` | Split DAG, used by the older `/runs/*` trigger path. |
| `TCP_FINALIZE_DAG_ID` | `drone_finalize` | Split DAG, same. |
| `TCP_AIRFLOW_USERNAME` | `admin` | Basic-auth user from `airflow standalone`. |
| `TCP_AIRFLOW_PASSWORD` | your Airflow admin password | Blank calls the REST API unauthenticated, which a production Airflow rejects. |
| `TCP_AIRFLOW_AUTH_TOKEN` | leave unset | Bearer-token alternative to basic auth — use one or the other, not both. |

#### FileBrowser

| Variable | Value | What it means |
|---|---|---|
| `TCP_FILEBROWSER_BASE_URL` | `http://filebrowser:80` (compose service DNS) or `http://host.docker.internal:8098`, or the production host if FileBrowser runs elsewhere | **Internal** URL the backend calls for login + share creation. Never shown to a browser. |
| `TCP_FILEBROWSER_PUBLIC_URL` | the hostname users actually reach, e.g. `http://<workstation-ip>:8098` | **Browser-facing** URL embedded in `files_url`. `http://localhost:8098` works only when the browser is on the workstation. The cluster-review UI fetches artifacts from it (§1.5), so a container-internal value here breaks the review step, not just the download link. |
| `TCP_FILEBROWSER_USERNAME` | `admin` | |
| `TCP_FILEBROWSER_PASSWORD` | the FileBrowser admin password | FileBrowser generates a random one on first boot; read it from `docker compose -f docker-compose.hub.yml logs filebrowser`, or reset with `docker compose -f docker-compose.hub.yml exec filebrowser filebrowser users update admin --password <new>`. |

> **Fix required.** `.env` currently reads `TCP_FILEBROWSER_BASE_URL=http://host.docker.internal:80908`. Port 80908 is above the 65535 TCP maximum, so every share creation fails with `FILEBROWSER_UNREACHABLE`.

#### Google sign-in

| Variable | Value | What it means |
|---|---|---|
| `TCP_AUTH_ENABLED` | `true` | Human endpoints 401 without `X-User-Email`. |
| `TCP_GOOGLE_CLIENT_ID` | leave unset | FYI-only field. The real client id is public and lives in `frontend/config.js` (§3.2). |

The backend does **not** verify the Google token — it trusts the header for audit and ownership. Anyone who can reach `:8123` directly can set it to any value, so the API port must not be exposed to the open internet.

#### Service auth

| Variable | Value | What it means |
|---|---|---|
| `TCP_COMPUTE_TOKEN` | a long random secret (`openssl rand -hex 32`) | `/compute/*` then requires `X-Service-Token`. **Must equal `DRONE_SERVICE_TOKEN` on the Airflow worker.** Read the warning below before enabling. |
| `TCP_API_KEY` | leave unset | Legacy `X-API-Key` gate, superseded by Google sign-in. Setting it would require the frontend to send a second header it has no way to obtain. |

> **Warning — setting `TCP_COMPUTE_TOKEN` will 401 the frontend's polling.** `GET /project/drone_status/{dag_run_id}` (`code/app/api/v1/analyze.py:122-130`) declares `_svc: str = Depends(require_service_token)`, which a browser cannot satisfy. It is harmless today only because the token is unset, making `require_service_token` a no-op (`deps.py:68`). The moment production sets it, every analyze and finalize poll gets 401 `UNAUTHENTICATED` — the DAG still completes in Airflow, but the UI spins forever and then errors, reading as a broken deploy rather than a config change. `X-Service-Token` is a service credential and must not be shipped to the browser to work around this; the fix is to drop `_svc` from `drone_status`, which is frontend-facing, already gated by `require_api_key`, and scoped to the caller's own project by `resolve_project`. Pre-existing (`1f6a8d0`, Jun 22), not a regression from this release.

#### Logging

| Variable | Value | What it means |
|---|---|---|
| `TCP_LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR`. |
| `TCP_LOG_DIR` | `/data/logs` | Where `app.log` + `errors.jsonl` are written. Must stay **outside** `storage_root/projects` so retention can't delete it. |
| `TCP_LOG_JSON` | `true` | One JSON object per line in production; `false` gives dev-readable text. |
| `TCP_LOG_MAX_BYTES` | `10000000` | RotatingFileHandler cap per file. |
| `TCP_LOG_BACKUP_COUNT` | `5` | Rotated files kept per handler. |

#### Retention & consent

| Variable | Value | What it means |
|---|---|---|
| `TCP_RETENTION_DAYS` | `30` | Retention window. Default changed 7 → 30 in this release. |
| `TCP_RETAIN_CONSENT_ALL` | `true` | consent=1 projects are never pruned. |
| `TCP_CLEANUP_ENABLED` | `false` | **Keep false.** The legacy Celery beat job is consent-blind and would wipe consented data; `code/scripts/run_retention.py` drives retention from system cron instead. |

#### Uploads & misc

| Variable | Value | What it means |
|---|---|---|
| `TCP_MAX_UPLOAD_MB` | `8192` | 8 GB per upload. |
| `TCP_PUBLIC_BASE_URL` | the public API origin, e.g. `https://api.example.com` | Makes STAC Item asset/link hrefs absolute. Blank emits relative hrefs, which break for anyone consuming the STAC item outside the UI. |

#### Build-time

| Variable | Value | What it means |
|---|---|---|
| `TORCH_INDEX` | `https://download.pytorch.org/whl/cu128` | Only used when **building** (`docker-compose.yml`). Irrelevant to the hub pull path, but wrong here means the next rebuild silently produces a CPU image (§2.1). The `.env.example` comment still says `cu118` — stale. |

#### Proxy (site-specific)

Present for the IIT-D network. Drop the whole block on a network without a proxy.

```
http_proxy=http://10.10.78.21:3128
https_proxy=http://10.10.78.21:3128
HTTP_PROXY=http://10.10.78.21:3128
HTTPS_PROXY=http://10.10.78.21:3128
no_proxy=localhost,127.0.0.1,host.docker.internal,filebrowser
NO_PROXY=localhost,127.0.0.1,host.docker.internal,filebrowser
```

`no_proxy` matters more than it looks. Both `airflow_client.py` and `filebrowser_client.py` use `urllib`, which honours `http_proxy`. **Every internal hostname those two reach must be listed here** or the request goes to `10.10.78.21:3128` and fails. If you set `TCP_FILEBROWSER_BASE_URL=http://filebrowser:80`, `filebrowser` must be in both `no_proxy` and `NO_PROXY` — the current `.env` does not have it. Same for any Airflow hostname that isn't `host.docker.internal`.

#### `.env.example` is out of date

Copying it to `.env` produces a deployment with FileBrowser silently disabled and Airflow half-configured. Missing: `TCP_FILEBROWSER_*`, `TCP_DRONE_DAG_ID`, `HF_HUB_OFFLINE`, `IMAGE_API`, `IMAGE_FRONTEND`, `TCP_PUBLIC_BASE_URL`, `TCP_RETAIN_CONSENT_ALL`, `TCP_CLEANUP_ENABLED` and the proxy block; its `TORCH_INDEX` comment still says `cu118`. Update it to match §3.1 before tagging.

---

### 3.2 `frontend/config.js`

Not an env file — a git-ignored JS file, copied from the committed `config.js.example` and served as-is by nginx from the `./frontend:/usr/share/nginx/html` mount. The values in it are **public**: the browser downloads this file. Nothing secret belongs here.

```bash
cp frontend/config.js.example frontend/config.js
$EDITOR frontend/config.js
```

Sample:

```js
/*
 * Frontend runtime config — copied from config.js.example.
 * Both values are PUBLIC: the browser downloads this file verbatim.
 */

// Google OAuth Client ID, type "Web application", from Google Cloud Console.
// In the console, add every origin users load the UI from under
// "Authorized JavaScript origins" — e.g. http://10.x.x.x:8200,
// https://your-site.com — or sign-in fails with an origin mismatch.
window.GOOGLE_CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com";

// API base URL. "" = same origin (API + UI behind one reverse proxy).
// For the split-port layout, set the API origin explicitly:
//   window.API_BASE = "http://10.x.x.x:8123";
window.API_BASE = "";
```

| Value | What to put in it |
|---|---|
| `window.GOOGLE_CLIENT_ID` | Your OAuth **Web application** client id, `<id>.apps.googleusercontent.com`. Every origin the UI is served from must be registered under **Authorized JavaScript origins** in Google Cloud Console — `http://<workstation-ip>:8200`, `https://your-site.com`, and so on; the port is part of the origin. **Leaving the `PASTE_YOUR_CLIENT_ID…` placeholder disables the gate entirely** — `AUTH_CONFIGURED` goes false and the overlay auto-hides, so the app opens unauthenticated regardless of `TCP_AUTH_ENABLED`. |
| `window.API_BASE` | `""` — same origin, with API and UI behind one reverse proxy. Set an explicit origin (`"http://<host>:8123"`) only if you keep the split-port layout, in which case the API origin must also allow CORS for the frontend origin. |

**Two independent switches.** `TCP_AUTH_ENABLED=true` makes the *backend* reject requests without `X-User-Email`; a real `window.GOOGLE_CLIENT_ID` makes the *frontend* actually collect one. Setting only the first breaks the UI; setting only the second gates the UI while leaving the API open. Both are required. And with the guest button still present (§1.2), neither one gates anyone who uses the UI — that button is the decision to make before tagging 1.0.
