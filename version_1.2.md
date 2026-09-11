# Version 1.2 — Changelog

Covers everything since the 1.0 changelog was committed (`7a7fceb`, "Multi-ortho
build") up to `239cab6`.

| Section | Contents |
|---|---|
| [1. Code changes](#1-code-changes) | every code-level change, file by file |
| [2. Docker & configuration](#2-docker--configuration) | compose, nginx, `.env.example`, new settings |
| [3. Upgrading from 1.0](#3-upgrading-from-10) | what happens on the first start |

---

## 1. Code changes

### 1.1 Headline — every run is its own record

Before 1.2 a project could describe exactly one run. Starting a new analysis
overwrote the previous run's state, parameters and chosen k, and archiving a run
deleted its species labels, so an earlier run could never be picked up and
finished later.

In 1.2 each run keeps its own record. Several runs can sit side by side — the
same orthomosaic analysed with different settings — and any of them can be
reviewed, labelled and exported, including one that finished weeks ago while
another run in the project is computing now.

- `work/run_<n>` numbering is unchanged, so the pipeline, the Airflow DAGs and
  the `/compute/*` callbacks are unaffected.
- The project state still acts as the lock (one computing run per project) and
  mirrors the active run; a run's own state is the truth about that run.

### 1.2 Repository layout

- Removed the nested duplicate tree `code/app/app/`. Nothing imported it — the
  API is served from `code/app/main.py` — so the run-level work that lived there
  was never actually deployed. It is merged into `code/app/` and the copy is
  gone. A `code/app/app/` reappearing is a mistake.

### 1.3 New modules

**`code/app/services/run_registry.py`** — the bridge between a project and its runs.

- `get_run`, `ensure_run` (get-or-create), `mirror` (copy the project's
  run-scoped fields onto the active run's record), `set_run_state` (move one run,
  then re-derive the project state), `refresh_project_state`, `labels_for`,
  `can_label`, `can_finalize`.
- Project-only states (`UPLOADING`, `DELETING`) are never mirrored onto a run.
- Bookkeeping never raises into the request that is doing the real work.

**`code/app/services/run_backfill.py`** — `backfill_runs()`, run at start-up.

- Gives every project created before 1.2 one run record per run in its history,
  and links its existing labels to the live run.
- Idempotent (projects that already have run records are skipped) and never
  blocks start-up.
- A run whose orthomosaic cannot be determined is shown as "orthomosaic not
  recorded" rather than guessed.

**`code/app/services/startup_recovery.py`** — `recover_interrupted_runs()`, run at start-up.

- A run left in `ANALYZING` / `FINALIZING` by a restart is marked `FAILED` with a
  `SERVER_RESTARTED` error, so it can be re-run instead of blocking the project.
- In-process runs are released straight away. Airflow-dispatched runs are
  released only once Airflow reports the DAG run finished (or has no record of
  it); if Airflow cannot be reached the run is left alone.
- Off with `TCP_STARTUP_RECOVERY_ENABLED=false`.

**`code/crown_thumbs.py`** — `tif_to_png_bytes()` and `write_thumbnail()`.

- The one place a crown GeoTIFF becomes a PNG, shared by the pipeline and the
  API so a crown looks the same whichever side rendered it.
- Imports nothing from `app`, so the pipeline stays runnable on its own.
- Thumbnails are written to a temporary name and swapped in, so a reader never
  sees a half-written file.

---

### 1.4 Data model

**`code/app/db/models.py`**

- New `Run` model: one record per analysis run, holding its name, state,
  detector, parameters, recommended / available / chosen k, the orthomosaic it
  used, its failure record and its timestamps.
- Cluster labels now belong to a run, and are no longer deleted when a run is
  archived.

**`code/app/db/session.py`**

- The start-up migration is extended so an existing SQLite database gains the
  label → run link without being recreated.
- WAL is now attempted rather than required. On filesystems that refuse it (a
  Windows bind mount, a network share) the service falls back to the rollback
  journal and logs one warning, instead of every connection failing and the
  service not starting at all.

---

### 1.5 API

Human routes keep the `/projects/{id}/…` + `/project/…` pairing.

| Route | Change |
|---|---|
| `POST …/runs/{n}/labels` | **new** — label a specific run. The existing `…/labels` still means the active run. Labels are replaced for that run only. |
| `POST …/runs/{n}/finalize` | **new** — export a specific, already-labelled run. Still only one run per project computes at a time. |
| `GET …/runs` | new `ortho_id` and `run` filters. Each entry now carries `run_id`, `state`, `label_count`, `can_label`, `can_finalize`, `files_url`, timestamps and its orthomosaic. Built from the run records, falling back to the legacy history for a project not yet backfilled. |
| `GET …/runs/{n}/results` | an unknown run is 404 `RUN_NOT_FOUND`, with the valid range in `details`. |
| `GET …/clustering`, `…/clustering/k-selection.png`, `…/clustering/{k}/tsne.png`, `…/clustering/{k}/clusters`, `…/crowns/{name}`, `…/detection/overlay.png` | all take `?run=N` (default: the active run) and gate on **that run's** state, so an older run can be reviewed while a newer one is computing. |

**`code/app/api/v1/clustering.py`**

- The review payload names its `run`, `run_id` and `ortho_id`, and adds
  `overlay_url` and a per-run `files_url`. Every URL in it carries `?run=`, so
  following any link keeps you on the same run.
- `…/{k}/clusters` reads membership from `k{k}_assignments.csv` (so it works
  even when per-cluster folders were not written) and returns `crowns` — name,
  distance and thumbnail URL — ordered nearest the cluster centre first, plus an
  `order` field: `centroid`, or `filename` for runs made before distances were
  recorded. The old `sample_crowns` list is kept for compatibility.
- `…/crowns/{name}` with `k` serves the thumbnail rendered during analysis,
  renders and caches it on a miss, and sends a one-day `Cache-Control`.
- Messages and hints name the run they are about.

**`code/app/api/v1/labels.py`** — per-run labelling. For the active run the state
change is still an atomic claim; for an older run only that run's state moves, so
a run computing elsewhere in the project keeps its busy guard. The response adds
`run`, `run_id` and `project_state`.

**`code/app/api/v1/runs.py`**

- Finalize resolves the target run, checks that run's labels and state, takes the
  project-level lock, and dispatches with that run number. Finalizing an older
  run no longer disturbs the active run's state.
- Analyze records `based_on_run` when it was started from an older run, and
  clears it otherwise so later runs do not inherit it.

**`code/app/api/v1/results.py`** — run history built from the run records
(`_run_meta_from_rows`) and decorated with the per-run capability flags
(`_decorate`). `can_label` / `can_finalize` are decided on the server, not
re-derived in the browser.

**`code/app/schemas/project.py`** — `AnalyzeTrigger.based_on_run` (optional;
recorded for lineage only, changes no behaviour).

**`code/app/core/logging.py`** — new error codes `SERVER_RESTARTED` and
`RUN_NOT_FOUND`. The signed-in user is now part of the log context.

**`code/app/core/settings.py`** — `thumbs_per_cluster` (default 5) and
`startup_recovery_enabled` (default true).

**`code/app/services/filebrowser_client.py`** — `run_share_url(hash, run)` deep-links
into one run's folder inside the project's existing share; no extra share or API
call.

**`code/app/services/project_service.py`** — `archive_current_run` stamps the
outgoing run's record before the run counter moves, and keeps its labels.

**`code/app/services/state.py`** — `transition_if` mirrors every successful state
change onto the active run's record.

**`code/app/main.py`** — start-up runs the backfill, then recovery.

---

### 1.6 Orchestration (Airflow)

**`code/app/api/deps.py`** — new `service_caller` dependency. A request carrying
the configured orchestrator service token is treated as the system principal, so
Airflow callbacks resolve their project by id instead of failing the per-user
ownership check with 403 on deployments where users sign in. Threaded through
`drone_api`, `drone_status`, `/project/analyze`, `/project/finalize` and
`/project/runs/*`. This makes `TCP_COMPUTE_TOKEN` (with a matching
`DRONE_SERVICE_TOKEN` on the worker) required on any deployment with real users.

**`code/app/services/run_dispatch.py`** — `dag_id_for_job()`; the run number is
passed through to the local worker thread.

**`code/app/services/airflow_client.py`** — `get_dag_run_state()` takes the
`dag_id` the run was started under (default unchanged).

---

### 1.7 Worker & pipeline

**`code/app/workers/tasks.py`**

- `job_a_analyze` / `job_b_finalize` take the run number they were dispatched
  for. Outcomes are recorded on that run and the project state is re-derived from
  the run records.
- Finalize reads only that run's labels.
- Step 1 now crops from the **full-resolution** orthomosaic rather than the
  downsampled copy detection ran on — matching the CLI. The run gets a hard link
  to the original (`_place_run_ortho`), falling back to a copy across
  filesystems.
- Task logs carry the project owner in their context.

**`code/tree_crown_pipeline.py`**

- `k{k}_assignments.csv` gains `dist_to_centroid` (KMeans distance to the
  crown's own cluster centre).
- `_write_cluster_thumbs` renders the `THUMBS_PER_CLUSTER` most typical crowns of
  each cluster, for every k, into `clustering/k<k>/thumbs/`. A crown that will
  not render is skipped; the API renders it on demand.
- Crowns are hard-linked into cluster and species folders (`link_or_copy`)
  instead of copied.
- Emoji removed from console output (also in `predict.py` and
  `end_to_end_pipeline.py`).

**`code/config.py`** — CLI default `THUMBS_PER_CLUSTER = 5`.
**`code/app/services/pipeline_adapter.py`** passes the setting to the pipeline.

---

### 1.8 Frontend (`frontend/index.html`)

- **Cluster review rewritten** to read from the API rather than FileBrowser
  download URLs: k-selection and t-SNE plots, one row per cluster with its most
  typical crowns, a full-size crown lightbox (`#crownLb`), a marker on the
  recommended k, and a "Use k = … and name these groups" button that fills in
  step 4. A failed image leaves a labelled tile instead of a gap.
- **Run picker in step 4** (`#labelPicker`): when more than one run is ready for
  names, lists them all; `pickLabelRun()` switches without leaving the step.
- **Opening a run** restores its settings into step 3 (`applyRunParams`,
  `RUN_PARAM_FIELDS`), ticks its orthomosaic and loads its review. The banner
  names the run's orthomosaic and which run "Run analysis" will start next. Run
  lists show "from run N" lineage.
- **Asset URLs** — plots, crown images and download links are built through a new
  `assetUrl()` helper so they resolve against the right project and user.
- The "Connection check" diagnostics panel is removed. The transport-failure
  classification inside `api()` is unchanged.

---

## 2. Docker & configuration

- **nginx** — new `docker/nginx-frontend.conf`, copied in by
  `Dockerfile.frontend`. Access and error logs are written to a host-mounted
  volume as well as stdout, so they survive the container being replaced.
- **Container log caps** — both compose files use `json-file` logging capped at
  10 MB × 3 files per service.
- **Images** — the default frontend image is `uavforaliens/treecrown-frontend:latest`,
  and `docker-compose.yml` defaults to the published tags. The commented-out
  legacy stack is removed from `docker-compose.hub.yml`.
- **`.env.example`** — tidied: `TORCH_INDEX` comment now says `cu128`; the
  duplicate `HOST_MODELS_DIR` is removed; `IMAGE_*` are commented out so each
  compose file picks its own default; `TCP_COMPUTE_TOKEN` is documented as
  required once real users own projects.

New settings:

| Variable | Default | Meaning |
|---|---|---|
| `TCP_THUMBS_PER_CLUSTER` | `5` | Crowns per cluster pre-rendered during analysis. `0` renders them on demand instead. |
| `TCP_STARTUP_RECOVERY_ENABLED` | `true` | Release runs a restart interrupted. Leave on. |

---

## 3. Upgrading from 1.0

`docker compose pull` + `up -d`. On the first start:

1. Missing tables and columns are added to the SQLite database.
2. Existing projects get run records from their history, and their current labels
   are linked to the live run.
3. Any run left mid-flight is released as described in §1.3.

All three are safe to repeat and none of them can stop the service booting. On
Postgres the schema changes are not applied automatically — apply the equivalent
DDL once.

Runs analysed before 1.2 have no crown distances, so their review shows crowns in
detection order with a note saying so. Re-running the analysis gives
typical-crown ordering and pre-rendered thumbnails.
