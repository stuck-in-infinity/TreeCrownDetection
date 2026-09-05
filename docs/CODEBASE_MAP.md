# Codebase Map — Tree-Crown Species Pipeline

Navigation index for this repo. **Read this before grepping.** Line numbers are
anchors that drift; the file→responsibility mapping is stable. If a lookup here
turns out wrong, fix the entry rather than leaving it.

Human-facing docs live elsewhere and are *not* duplicated here:
`README.md` (deploy/run), `project_outline.md` (architecture + mermaid),
`docs/INTEGRATION_GUIDE.md`, `docs/FRONTEND_BACKEND_FLOW.md`,
`docs/filebrowser_*.md`.

---

## 1. One-paragraph system

Drone orthomosaic → Detectree2 detects crowns → DINOv2 embeds each crown crop →
KMeans clusters them → **human labels clusters with species** → export
KMZ/GeoJSON/CSV + STAC item. Three containers: static frontend (`:8200`),
FastAPI backend (`:8123`, `:8000` inside), optional external Airflow (`:8080`)
that does zero ML and only calls back into the backend. SQLite + on-disk
artifacts under `data/`.

---

## 2. Where to look for X

| Need | Go to |
|---|---|
| Add/change an HTTP endpoint | `code/app/api/v1/<area>.py` + register in `router.py` |
| Project state machine / status gating | `api/v1/runs.py` (`_gate`), `services/state.py` |
| Concurrency / "who may compute" | `services/state.py` (state claims), `services/job_claim.py` (job claims) |
| What states exist | `db/models.py` docstring (top) |
| Per-run state / an old run's results | `db/models.py` `Run`, `services/run_registry.py` |
| DB schema / a new column | `code/app/db/models.py` |
| Env var / config knob | `code/app/core/settings.py` (all `TCP_`-prefixed) |
| On-disk artifact paths | `code/app/core/storage.py:36` `project_paths()` |
| The actual ML algorithm | `code/tree_crown_pipeline.py` (steps 1–4) |
| Crown GeoTIFF → PNG | `code/crown_thumbs.py` (shared by pipeline and API) |
| Crown detection / downsampling | `code/predict.py` |
| Which model weights exist | `code/models.yaml` + `core/models_registry.py` |
| Background job bodies | `code/app/workers/tasks.py` |
| How a run gets started | `services/run_dispatch.py` (Airflow vs local thread) |
| Airflow → backend contract | `api/v1/compute.py` docstring + `airflow/dags/*.py` |
| Auth / identity headers | `code/app/api/deps.py` |
| Error envelope + codes | `core/errors.py`, `core/logging.py:298` `ERROR_CODES` |
| Logging, request ids, IST time | `code/app/core/logging.py` |
| STAC output | `code/app/services/stac.py` |
| FileBrowser share links | `services/filebrowser_client.py` |
| Anything UI | `frontend/index.html` (single file, §6 below) |
| Retention / deletion | `code/scripts/run_retention.py`, `workers/cleanup.py` |

---

## 3. Backend — `code/app/`

### Entry + wiring
- `main.py` — FastAPI app, CORS, request-id middleware (`:82`), `/livez` (`:136`),
  mounts `api_router`. `_is_compute_path()` (`:73`) exempts Airflow callbacks
  from the human error envelope.
- `api/v1/router.py` — prefix `/api/v1`, includes the 8 area routers.
- `db/session.py` — engine, `get_db`, `init_db`. `db/base.py` — `Base` only.

### Route areas (`api/v1/`)
Every human route is registered **twice**: `/projects/{project_id}/…` and a
legacy singular `/project/…` (project resolved from header/query by
`deps.resolve_project`). Expect paired decorators.

| File | Owns |
|---|---|
| `projects.py` (601 L) | CRUD, `/detectors`, `/feature-extractors`, ortho upload (`:243`) + from-URL/Drive (`:276`), ground-truth zip upload (`:348`), param validation (`:453`), raster metadata (`:494`), zip-slip guards (`:515`,`:538`) |
| `runs.py` | `runs/analyze`, `runs/finalize`, `runs/status`, and `runs/{n}/finalize`. State gating `_gate`, Job creation, run-config apply |
| `analyze.py` | `drone_api` / `drone_status` (unified-DAG entry) + `POST …/analyze` |
| `clustering.py` | Cluster review payload, `k-selection.png`, `tsne.png`, per-crown PNGs, detection overlay. Every route takes `?run=N` (defaults to the active run) and gates on `Run.state`; `?k=K` on a crown serves its precomputed thumbnail |
| `labels.py` | `POST …/labels` and `POST …/runs/{n}/labels` — user's cluster→species mapping, per run |
| `finalize.py` | `POST …/finalize` |
| `results.py` (320 L) | Results payload, KMZ/CSV/confusion-matrix/STAC downloads, consent, per-run history (`/runs`, `/runs/{n}/results`) |
| `compute.py` | **Airflow-only** `POST /compute/analyze`, `/compute/finalize`. Flat response bodies, HTTP-status-as-control-flow (200 ok / 400·404 skip / 500 fail), `Idempotency-Key` replay |

### Core (`core/`)
- `settings.py` — pydantic settings, `TCP_` env prefix. Groups: storage/models,
  DB+Redis, Airflow, model defaults, upload cap, `api_key`, `compute_token`,
  `auth_enabled`+`google_client_id`, FileBrowser, `public_base_url`,
  `celery_eager`, `thumbs_per_cluster`, retention, logging.
- `storage.py` — path authority. `project_root`, `run_dir`, `project_paths`,
  `ensure_project_dirs`, `reset_dirs`, `delete_project_dir`,
  `prune_labelled_outputs`.
- `logging.py` — IST-aware timestamps (`naive_now`, `now_ist`), `ContextFilter`,
  `JsonFormatter`, `configure_logging`, `with_context`, `new_request_id`,
  `classify_conn_error`, `ERROR_CODES`.
- `errors.py` — `ApiError`, 4 handlers, `install_error_handlers`. Envelope is
  `{"error": {code, message, project_id, stage, details, hint}}`.
- `models_registry.py` — reads `models.yaml`; `list_models`, `resolve_model_path`,
  `list_backbones`, `resolve_backbone`, `DEFAULT_MODEL_KEY`.

### Services (`services/`)
- `run_dispatch.py` — **the fork**: `airflow_enabled()` → `trigger_dag`;
  else run the Celery task body in a daemon thread (`_run_local`).
- `airflow_client.py` — `trigger_dag`, `trigger_drone_dag`, `get_dag_run_state`.
- `pipeline_adapter.py` — DB `Project` → `Config` SimpleNamespace for the ML code
  (`build_config`, `:29`); writes `species_map.csv`.
- `project_service.py` — `serialize_project`, `archive_current_run` (run
  versioning), `_last_error`.
- `state.py` — `transition_if()`: single atomic conditional UPDATE, the only
  correct way to change `Project.state`. Loser gets `409 CONFLICT_BUSY`. Also
  mirrors the new state onto the active `Run` row.
- `run_registry.py` — the `Run` table's owner: `get_run`, `ensure_run`,
  `set_run_state`, `mirror` (Project state → active Run row), `can_label`,
  `can_finalize`, `labels_for`. `Project.state` stays the compute mutex;
  `Run.state` is the truth about one run.
- `run_backfill.py` — `backfill_runs()` at start-up: gives projects that
  predate the `runs` table one row per run, from `project.runs` JSON.
  Safe to run twice; never raises.
- `startup_recovery.py` — `recover_interrupted_runs()` at start-up: releases
  runs a restart killed (`SERVER_RESTARTED`). Airflow-dispatched runs are
  left alone. Off via `TCP_STARTUP_RECOVERY_ENABLED=false`.
- `job_claim.py` — the *other* mutex, for the three endpoints that compute
  inline (`/compute/*`, `/project/analyze`, `/project/finalize`). State cannot
  exclude them (the trigger already moved the project into the in-progress
  state, so it is a legal source state and a conditional UPDATE onto it always
  succeeds), so the claim is the Job INSERT against the unique
  `(project_id, celery_task_id)` index, plus an active-job check. Keys are
  namespaced `compute:` so the trigger's never-finished placeholder Job doesn't
  count as active. `/compute/*` renders a loss as 400 (DAG skips); the
  human-facing routes as 409.
- `stac.py` (392 L) — `build_stac_item`, `write_stac_item`, WGS84 footprint from
  GeoJSON or ortho, column docs.
- `assets.py` — STACD asset ids/versions, hosting platform.
- `filebrowser_client.py` — token, `create_project_share`, `share_url`,
  `run_share_url` (share subpath `work/run_<n>`).
- `activity_log.py` — `append()`, daily file under `data/storage/activity`.

### Workers (`workers/`)
- `tasks.py` (369 L) — `job_a_analyze` (`:119`), `job_b_finalize` (`:227`).
  Cached model loaders (`_get_predictor`, `_get_dinov2`), `_Tee` stdout capture
  into the run log, `_fail` writes traceback to DB + log.
- `cleanup.py` — legacy Celery-beat retention sweep, Redis lock. Off by default.
- `celery_app.py` — Celery config (`celery_eager` honoured).

### Schemas (`schemas/`)
`project.py` — `PipelineParams`, `ProjectCreate/Update/Out`, `AnalyzeTrigger`,
`FinalizeTrigger`, `OrthoFromUrl`, `OrthoOut`. `labels.py`, `compute.py`
(`ComputeRequest`, `STACDResponse`), `job.py`.

### Auth (`api/deps.py`)
`require_api_key` (X-API-Key + optional X-User-Email), `require_user`
(Google GIS headers, **unverified** — audit only, must sit behind a gateway,
with a `?user=` query fallback for browser-issued GETs — invariant 16),
`require_service_token` (X-Service-Token, Airflow), `resolve_project`,
`get_project`.

---

## 4. ML pipeline — `code/` (framework-free, importable)

- `tree_crown_pipeline.py` (972 L) — the algorithm.
  `Config` (`:60`), `step1_crop_crowns` (`:151`), `build_dinov2` (`:218`),
  `step1_extract_features` (`:230`), `step1_cluster` (`:314`),
  `step1_analyze_k` (`:377`, elbow/silhouette/DB → recommended k),
  `step1_tsne` (`:447`), `step2_assign_species` (`:499`),
  `step3_validate` (`:633`, confusion matrix vs ground truth),
  `step4_export_kmz` (`:721`), `main()` for CLI use.
- `predict.py` — Detectree2/detectron2 wrapper. `get_ortho_gsd`,
  `compute_downsample_scale` (GSD-aware, target `0.025/0.3` m),
  `build_predictor`, `run_detectree2_pipeline`.
- `crown_thumbs.py` — `tif_to_png_bytes`, `write_thumbnail`. The one place a
  crown GeoTIFF becomes a PNG, so the thumbnails the pipeline renders during
  a run and the ones `api/v1/clustering.py` renders on demand look the same.
  Imports nothing from `app` — the pipeline must stay runnable on its own.
- `end_to_end_pipeline.py` — thin CLI driver `step0…step4`.
- `config.py` — standalone/CLI defaults, **not** used by the API (the API builds
  config via `pipeline_adapter.build_config`). Don't confuse the two.

Step outputs land in `work/run_<n>/step{1,2,3,4}_output/`.

---

## 5. Storage layout (`core/storage.py`)

```
data/
  treecrown.db                      # SQLite
  logs/                             # app logs — OUTSIDE storage_root
  storage/
    activity/                       # activity-log JSONL
    projects/<project_id>/
      input/ortho/                  # run-independent
      input/ground_truth/           # run-independent
      work/run_<n>/
        detectree/ ortho/ polygons/
        step1_output/               # crowns, features, clustering/
          clustering/k<k>/thumbs/   # PNGs of the crowns nearest each centroid
        step2_output/               # species assignment
        step3_output/               # validation, confusion matrix
        step4_output/               # KMZ / GeoJSON / CSV
        logs/
```

`current_run` on `Project` points at the live run; `runs` (JSON) keeps history.
Consent `2` prunes `step2/3/4` only (`_LABELLED_OUTPUT_KEYS`).

---

## 6. Frontend — `frontend/index.html` (single file, ~4500 L)

`config.js` (gitignored, from `config.js.example`) supplies `window.API_BASE`
and Google client id. `index.legacy.html` is the old UI — ignore unless asked.

Line numbers drift; the function names are the stable anchors.

| Lines | Region |
|---|---|
| 7–224, 232–924 | CSS (two `<style>` blocks). Review panel + crown lightbox styles are at the end of the second. |
| 925–2040 | Markup: auth gate, `#pastRuns`, step sections (upload → configure → review → finalize), `#clusterReview`, `#crownLb`, FAQ |
| 2042–4220 | Main JS |
| — `api()` (`:2585`) | fetch wrapper. Classifies transport failures (`transportError`, `diagnoseFetchFailure`, `foreignResponse`) — this is what tells a user *why* a request never arrived |
| — ortho library | `renderOrthos`, `toggleOrtho`, `toggleOrthoRuns`, `renderOrthoRuns` (shows each run's state, label count and "from run N") |
| — run selection | `applyRunParams` (`:3152`) puts a run's settings back into step 3; `runEntry`; `openRun` (`:3189`) ticks its ortho, repopulates step 3 and loads that run's review; `renderRunBanner`; `clearActiveRun` |
| — `analyzeProject()` | reads the parameter form, sends `based_on_run` when started from an older run |
| — cluster review (`:3638`) | `loadClusterReview`, `renderClusterReview`, `showKView`, `useK`, `reviewImg`, `thumbFailed`, `openCrown`. Every image is an `<img>` against **our own API** (same-origin under nginx) — never FileBrowser |
| — `submitLabels` (`:3797`), `finalize`, `rerunFlow`, `newAnalysisFlow`, consent |
| — `loadMyProjects` (`:3940`), `openProject`, `restoreOpenProject` |
| — `initGateCanvas` (`:4105`) | decorative landing canvas. Ignore for logic changes. |
| 4222–4515 | Walkthrough add-on (`#wt-page`), self-contained, owns its own `#wts-lb` lightbox |

**Step 3 parameter form** is grouped by pipeline stage, each group a `<details>`
holding its own model selector: Detection/Detectree2 (`modelKey`, `p_tile`,
`p_buf`, `p_iou`, `p_conf`) → Crown embedding/DINOv2 (`backbone`) → Clustering
(`p_pca`, `klist`). `batch_size` and `img_size` are intentionally absent (see §9
invariants 9–10); `#epsgFallback`/`p_epsg` is hidden unless the ortho had no CRS.
`RUN_PARAM_FIELDS` maps these field ids to the param names a run records.

**Gone:** the connection-check panel (`#connBox`, `runConnectionCheck`,
`offerConnectionCheck`) and the FileBrowser URL helpers (`fbRaw`, `fbView`).
Plots and crowns come from the API now, so there is no cross-origin step left to
diagnose. The transport classification inside `api()` stayed — that is the part
that actually explained failures.

---

## 7. Orchestration & deploy

- `airflow/dags/drone_analyze_dag.py` / `drone_finalize_dag.py` — one
  `PythonOperator` each; POST to `COMPUTE_URL` with `Idempotency-Key = dag_run_id`
  and optional `X-Service-Token`. `200`→success, `400/404`→`AirflowSkipException`,
  else fail. 7200 s timeout.
- `docker-compose.yml` — local build (`api` + `frontend`).
  `docker-compose.hub.yml` — Docker Hub images (`uavforaliens/treecrown-*`).
  Code, `data/`, `models/` are **bind-mounted**, not baked.
- `Dockerfile` (backend, `TORCH_INDEX` build arg), `Dockerfile.frontend` (nginx).
- `publish.sh` — push images.
- `.env.example` / `code/.env.example` / `frontend/config.js.example` — the three
  gitignored files a deployment must create.

---

## 8. Scripts

- `code/scripts/run_retention.py` — the real retention driver (cron, not Celery).
- `code/scripts/consensus_gt.py` *(untracked)* — turns the BBMP tree census into
  usable ground truth via ExG greenness + DBSCAN proximity consensus; detects
  merged multi-tree crowns.
- `code/scripts/index_census.py` *(untracked)* — index/reshape census GeoJSON.
- `scripts/drone_imagery_download/download_drone_imagery.py` *(untracked)* —
  tiles a Web-Mercator bbox and stitches a georeferenced ortho.

---

## 9. Invariants worth not re-deriving

1. **Never** set `Project.state` by read-then-write — use `state.transition_if`.
   Constrain `allowed` to the state you actually validated against (often
   `{pre_state}`), or the "atomic" update silently permits the race it was
   meant to stop. A self-transition (`X` allowed → `X`) excludes nobody: that is
   why the inline-compute endpoints claim a Job row instead
   (`services/job_claim.py`). `transition_if` also clears `Project.error`, so
   read any failure text you still need *before* calling it.
2. Human routes come in `/projects/{id}/…` + `/project/…` pairs; adding one means
   adding both.
3. `/compute/*` returns flat bodies and is exempt from the error envelope
   (`main._is_compute_path`). Don't "fix" it to match the others.
4. Airflow performs no computation; it only calls back.
5. `code/config.py` is CLI-only. API config comes from
   `pipeline_adapter.build_config`.
6. Timestamps are IST-aware naive datetimes via `logging.naive_now` — not
   `datetime.utcnow()`.
7. Detector weights are never in the image or git; mounted at `/models`.
8. Paths always via `core/storage.py`, never hand-joined.
9. `batch_size` is VRAM-bound and deliberately not in the UI. Default `64` lives
   in **two** places that must agree: `PipelineParams.batch_size` and
   `build_config`'s `params.get("batch_size", 64)`. (`code/config.py` and
   `tree_crown_pipeline.Config` hold separate CLI-only values.)
10. `img_size` is dictated by the DINOv2 backbone's `img_size` in `models.yaml`,
    not user-chosen — the frontend sends the selected option's `data-img`.
11. `source_epsg` is auto-detected at upload. When the GeoTIFF has no CRS,
    `build_config` falls back to a hardcoded `32643` and `set_crs()` *assigns*
    it, so a wrong guess silently misplaces the export. The frontend therefore
    forces the user to supply an EPSG in that case — don't remove that gate.
12. Requests that rewrite a project's input files or delete it hold a transient
    state as a lock — `UPLOADING` / `DELETING` (`api/v1/projects.py`). Neither is
    a valid analyze/finalize source state; both are released before the response
    returns. Clients treat unknown states as busy.
    Releasing the `UPLOADING` claim must restore the state the upload *started*
    from whenever that state is in `USED_RUN_STATES` — `_register_ortho` takes
    `pre_state` for exactly this. Stamping `UPLOADED` unconditionally (what it
    did before the library) is silent data loss: that state is the only marker
    that `work/run_<n>` is used, so `_apply_run_config` would skip
    `archive_current_run`, the next analyze would reuse the same run folder, and
    the worker's `reset_dirs` would wipe the finished run's step1–4 outputs.
13. The worker's stdout capture is **thread-scoped** (`workers/tasks.py`
    `_ThreadRoutedStream`). Never reintroduce `contextlib.redirect_stdout`
    there: jobs run concurrently in this process, and an out-of-order unwind
    leaves `sys.stdout` pointing at a closed file, silently dropping every later
    write process-wide.
14. Pipeline plotting is figure-scoped (`fig.savefig`, `plt.close(fig)`), never
    `plt.savefig` / `plt.close()`. pyplot's "current figure" is process-global
    and concurrent jobs would save each other's plots. Backend is forced to Agg.
15. SQLite runs in WAL with a 30 s busy timeout (`db/session.py`). Job progress
    is written throughout a run while requests read; the default rollback
    journal turns that into "database is locked".
16. Anything the **browser** fetches by itself — the review plots, the crown
    thumbnails, the result downloads — must carry `project_id` *and* `user` in
    the URL (`frontend/index.html` `assetUrl()`, the `<img>`/`<a>` counterpart
    of `withPid()`). `<img src>` and `<a href>` send no custom headers, so
    without them `require_user` reads no identity, `resolve_project` falls back
    to "newest project owned by `default`", and every image 404s
    `PROJECT_NOT_FOUND` — projects are owned by the signed-in email (or
    `guest@guest.local`), never by `default`. Adding a new asset route means
    routing its URL through `assetUrl()`, not through `withPid()`. The header
    still wins when both are present; `?user=` is a fallback, and it does not
    loosen the ownership check — a wrong `user` for a real `project_id` is
    still 403.

---

## 10. Snapshot of in-flight work

Dated **2026-08-15** (branch `newchanges`, last commit `4e42cf7`). Confirm with
`git status` before trusting; delete entries once merged.

**Per-run state — the `runs` table** (branch `reconcile-backend-trees`). Every
per-run field that used to live only on `Project` now has a `Run` row:
`number` (the `n` in `work/run_<n>`, unchanged and still what the pipeline,
Airflow and `/compute/*` use) plus a uuid `id` the API addresses. `Project.state`
is unchanged in meaning — the one-computing-run-per-project mutex and a mirror of
the active run; `Run.state` is the truth about a run. `cluster_labels.run_id`
ties labels to their run, and `archive_current_run` **no longer deletes them** —
that deletion was the sole reason an earlier run could not be finished later.
New routes `POST …/runs/{n}/labels` and `…/runs/{n}/finalize`; `GET …/runs`
takes `ortho_id` and returns `run_id` + `files_url`. Start-up runs
`run_backfill.backfill_runs` then `startup_recovery.recover_interrupted_runs`,
both idempotent and non-raising.

> **This work previously lived in a nested `code/app/app/` copy that nothing
> imported** (`working_dir: /code` + `uvicorn app.main:app` resolves
> `code/app/main.py`), so the deployed API served none of it while the frontend
> already called the run-scoped routes. The trees are now reconciled and the
> nested copy deleted. If you find a `code/app/app/` again, it is a mistake.

**Multi-orthomosaic library** (uncommitted). `input/ortho/` is now an
append-only library instead of a single file: upload has append semantics with
per-project limits (`TCP_PROJECT_QUOTA_GB`, `TCP_MAX_ORTHOS_PER_PROJECT`), stems
are de-duplicated (`_unique_stem`), and `GET …/orthomosaics` lists the library
with usage and `in_use`. A run is pinned to ONE ortho: `AnalyzeTrigger.ortho_id`
→ `runs.resolve_run_ortho` / `_pin_run_ortho` → `project.params["ortho_id"|
"ortho_stem"]` → `workers/tasks._run_ortho_stems` (which replaced the old
"detect over every file in the directory" listing). Omitting `ortho_id` is legal
only with exactly one ortho; several give 400 `ORTHO_SELECTION_REQUIRED`. The
old 423 `ORTHO_LOCKED` freeze now applies to ground truth only. See invariant 12
for the release rule the upload path depends on. Also new: a wall-clock transfer
budget (`TCP_ORTHO_TRANSFER_TIMEOUT_MIN`, enforced out-of-process for Drive via
`services/drive_download.py`), `core/failures.py` run-failure classification,
`api/v1/health.py`, and `TCP_CORS_ORIGINS`.

**Race-condition pass** (uncommitted). New `services/job_claim.py`; claims added
to `api/v1/{compute,analyze,finalize,labels,projects,runs}.py`; unique
`(project_id, celery_task_id)` index on `jobs` + WAL in `db/session.py`
(migration `_migrate_sqlite_unique_job_key` dedupes legacy rows first);
thread-scoped stdout capture and locked model caches in `workers/tasks.py`;
figure-scoped plotting in `tree_crown_pipeline.py` / `predict.py`. See
invariants 1, 12–15. Airflow's wire contract is unchanged — the DAG files were
not touched.

Modified (pre-existing):
- `code/predict.py` — GSD-aware downsampling: `get_ortho_gsd`,
  `TARGET_EFFECTIVE_GSD_M`, `compute_downsample_scale`; default `scale` 0.3→1.
- `docker-compose.yml` / `.hub.yml` — CPU service definitions commented out,
  replaced by an NVIDIA-GPU service (`treecrown-workstation:cu128`).
- `frontend/index.html` — "Continue as guest" bypass (`guestLogin`, fixed
  `guest@guest.local` identity).

Untracked of interest: the three scripts in §8, `docs/progress_reports/`,
`bbmp_tree_census_july_2026.geojson`, `drone_imagery1.{tif,png,pgw}`,
`gt_out/`, `run_2/`, `frontend/index1.html`.
