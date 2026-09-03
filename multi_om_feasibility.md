# Multi-OM Support — Feasibility Notes

Scope: decouple orthomosaic (OM) upload from project; per-OM crown detection with per-OM
params; project = joint clustering over N processed OMs; explicit save/publish; cancellable runs.

Verdict up front: **the recommended two-stage design (OM+detection → clustering project) is
feasible and is the right shape.** The heavy compute already supports multiple OMs. Almost all
the work is API/DB/UI plumbing plus two genuinely tricky items: **run cancellation** and
**shared-OM lifecycle/retention**.

---

## 1. What already works today (free wins)

- `Project → Ortho` is already **1:N** in the ORM (`app/db/models.py`), not 1:1. Only the upload
  endpoint forces one (`_clear_existing_orthos`, set-semantics in `projects.py`).
- `job_a_analyze` already **loops over every ortho stem** in `input/ortho`, detects per-stem, and
  writes `polygons/<stem>.geojson` + `ortho/<stem>.tif`.
- `step1_crop_crowns` already reads **all** geojsons in `POLY_FOLDER` against **all** orthos in
  `ORTHO_FOLDER`, and names crowns `<ortho_stem>_<crown_id>.tif` → per-OM provenance is already
  carried through features, clustering CSVs and `image_stem` in step 2.
- Run versioning (`work/run_<n>`, `project.runs` history, `archive_current_run`) already gives us
  "re-run with different params without losing the old run".
- Atomic state gating (`transition_if`, 409 `CONFLICT_BUSY`) is reusable for the new entities.

So: **joint clustering of multiple OMs is already computable.** The blockers are structural, not
algorithmic.

## 2. What blocks it today

| Blocker | Where | Fix size |
|---|---|---|
| One ortho per project (set-semantics) | `projects.py::_clear_existing_orthos` | trivial |
| One param set per project, shared by detection + clustering | `Project.params`, `pipeline_adapter.build_config` | medium |
| Dataset frozen after first analyze (423 `ORTHO_LOCKED`) | `projects.py::_assert_ortho_unlocked` | small, but semantics must be redefined |
| Single project-level state machine — no per-OM detection state | `Project.state` | medium |
| OM files live *inside* the project folder | `core/storage.py::project_paths` | medium |
| No cancel path | `run_dispatch.py`, `workers/tasks.py` | **hard** (see §6) |

---

## 3. Recommended design (Option B — the "even better" one)

**Stage 1 — OM Library (upload + detection).**

- New table `Orthomosaic`: `id, user_id, name (user-given), filename, crs/epsg, width/height, bands, size, created_at`.
  Files at `storage/orthos/<om_id>/input/<name>.tif`.
- New table `DetectionRun`: `id, om_id, model_key, params (JSON), state, crown_count, geojson_path,
  overlay_path, job_id, created_at`. Files at `storage/orthos/<om_id>/det/<det_run_id>/`.
- **Detection runs are immutable and append-only.** Re-tuning params creates a new `DetectionRun`;
  nothing is ever overwritten. This removes a whole class of locking problems (see §7).
- Each OM exposes "latest / chosen" detection run for the UI, plus crown count + overlay preview so
  the user can judge parameter quality per OM.

**Stage 2 — Project (clustering + labelling + export).**

- `Project` no longer owns files. It owns: `name`, clustering params, `chosen_k`, labels, state,
  and a join table `ProjectDetection(project_id, detection_run_id)`.
- Project pins **detection_run ids**, not OM ids → results stay reproducible even if the user keeps
  tuning that OM afterwards.
- Analyze (project) = Step 1 only: crop → DINOv2 → cluster → k-analysis → t-SNE, over the union of
  the selected detection runs' polygons. Then labels → finalize → save/publish, as today.
- Per-run isolation stays: `work/run_<n>` unchanged, so re-running clustering with different
  `k_list` / backbone still archives the previous run.

Param split (clean, because the pipeline functions already read disjoint subsets):

- **Detection (per-OM):** `model_key`, `tile_size`, `buffer`, `iou_threshold`, `conf_threshold`,
  `detections_per_image`, `min_size_test`, `area_min`, `area_max`, `full_coverage`, `source_epsg`.
- **Clustering (per-project):** `model_name` (DINOv2), `img_size`, `batch_size`, `pca_components`,
  `k_list`, `chosen_k`.

`build_config` splits into `build_detect_config(om, det_run)` and `build_cluster_config(project, run)`.
Low risk — mechanical.

Option A (upload section + project that re-detects every time) is strictly worse: it re-runs the
expensive GPU detection on every clustering experiment, and gives no place to iterate on per-OM
params. Not recommended.

---

## 4. Feasibility by piece

### Easy (hours, low risk)
- Allow N orthos per project / N detection runs per project (delete set-semantics, add join table).
- Crown-name prefix collision guard: two OMs can be uploaded with the same filename. Prefix crowns
  with a short OM slug/id instead of the raw stem.
- Cluster × OM contingency table in the review UI (see §8 — cheap, high value).
- Reject mixed-EPSG selections with a clear 400 at project-analyze time.

### Medium (days, contained)
- **Storage layout.** OMs move out of the project tree. Do **not** copy GB-sized rasters into each
  run — **hardlink or symlink** the detection run's ortho + geojson into
  `work/run_<n>/{ortho,polygons}/`. rasterio/geopandas only read them.
- **Crop performance fix (do this regardless).** `step1_crop_crowns` currently tries *every* open
  ortho for *every* crown until one succeeds → O(crowns × OMs) rasterio masks. With 5–10 OMs this
  gets slow. We already know which ortho each polygon file came from — map `<stem>.geojson →
  <stem>.tif` directly. Big speedup, small patch.
- **State machines.** New `DetectionRun` state (QUEUED/RUNNING/SUCCEEDED/FAILED/CANCELLED);
  `Project.state` loses the detection phase and starts at `READY_TO_CLUSTER`.
- **STAC.** `build_stac_item` assumes one input footprint. Multi-OM → geometry = union of OM
  footprints, and `properties` should carry a per-OM params array instead of a flat param dict.
- **Migration.** There is no Alembic here — `db/session.py::_migrate_sqlite_add_columns` only adds
  columns. New tables + re-pointed FKs need either Alembic introduced now, or a one-shot backfill
  script (each existing project → 1 OM + 1 detection run + 1 project). Backfill is straightforward
  because today's projects have exactly one ortho. **Recommend introducing Alembic at this point** —
  the schema churn here is the largest so far.
- **Frontend.** `frontend/index.html` is a single ~112 KB file built around one linear project flow.
  Two new sections (OM library w/ per-OM param tuning + preview; project builder w/ multi-select)
  plus a rewritten progress/state model. Realistically **the single largest chunk of work** in this
  change, larger than the backend.

### Tricky (needs a decision, not just code)
- **Cancellation** — §6.
- **Shared-OM lifecycle / retention** — §7.
- **Cross-OM clustering validity** — §8.

---

## 5. Save / "save and publish"

Feasible and small, mostly a re-timing of things that already exist.

- Today: a FileBrowser public share is created at **project creation** (`create_project_share`), and
  a STAC item is written at the end of finalize. So output is effectively "published" by default.
- Change to: finalize produces a **draft** result. `POST /projects/{id}/publish` then (a) creates the
  FileBrowser share, (b) writes the STAC item, (c) snapshots the run into an immutable
  `published/<run>/` folder (or just freezes the run and records `published_at`), (d) records the
  existing consent value.
- Add `Project.published_at`, `published_run`, `visibility`. `save` = keep run + params + labels
  server-side, no share, no STAC.
- Gotcha: existing projects already have `share_hash` issued at creation. Migration must either
  revoke those shares or grandfather them. Decide explicitly — this is a privacy-visible change.
- Consent capture (`POST /project/consent`) belongs on the publish step now; it currently fires after
  finalize.

## 6. Terminating a running process — the hard one

Current dispatch has two modes and **neither can be cancelled**:

- **Local mode** (`run_dispatch._run_local`): the task body runs in a `threading.Thread` inside the
  API process. Python threads **cannot be killed**. No handle, no signal, nothing.
- **Airflow mode**: killing the DAG run does *not* stop the compute — the DAG only calls back into
  `POST /compute/analyze|finalize`, which runs `job_x.apply(...)` **synchronously inside our FastAPI
  process**. The work outlives the DAG run.

Options, cheapest → best:

1. **Cooperative cancel flag (works everywhere, ship first).** Add `Job.cancel_requested` +
   `CANCELLED` state, and a `POST /jobs/{id}/cancel`. The worker already has ~6 natural checkpoints
   (the `_set_job(current_stage=...)` calls); check the flag at each and raise `RunCancelled`.
   *Limitation:* only cancels at a stage boundary. Detection over one large ortho is a single long
   stage — cancel could take minutes. Push the check inside the per-ortho `for stem in stems:` loop
   (and ideally into the per-tile loop in `predict.run_detectree2_pipeline`) to tighten it.
2. **Real Celery (recommended target).** The Celery app, queues (`gpu`/`cpu`), prefork pool and
   Redis are already configured — the local-thread path just bypasses them. Dispatch via
   `apply_async` instead, then `revoke(task_id, terminate=True, signal="SIGTERM")` genuinely kills
   the prefork child → instant stop, VRAM released. Cost: Redis + a worker container must actually
   run in the deployment (currently optional). This is the clean answer.
3. **Subprocess for local mode.** If we don't want to require Redis: run the task body in a
   `multiprocessing.Process` and `terminate()` it. Contained change to `_run_local` only.
4. **Auto-timeout ("15 min → stop").** With Celery this is one line: `task_soft_time_limit` /
   `task_time_limit` (globally, or per-dispatch via `apply_async(soft_time_limit=...)`). Without
   Celery, a watchdog thread that sets `cancel_requested` when `now - job.started_at > limit`.
   Make the limit configurable per stage — detection and clustering have very different runtimes.

Whichever path: on cancel, the job must land in `CANCELLED` (not `FAILED`), the project/detection-run
must roll back to its pre-run state, and the partial `run_<n>` artifacts should be marked or wiped
(`reset_dirs` already exists for this).

**Recommendation:** ship (1) immediately — it's small and covers every mode — then move dispatch to
(2) and layer (4) on top. Don't build cancel on Airflow's DAG-run kill; it doesn't stop our compute.

## 7. Shared OMs — the subtle data-loss risk

Once an OM is shared across projects, `DELETE /projects/{id}` must **not** delete the OM files.
Today `delete_project_dir` nukes the whole project tree, and `scripts/run_retention.py` /
`prune_labelled_outputs` assume project-scoped inputs.

Needed:

- OM deletion refuses (409) while any project references one of its detection runs, **or** we
  reference-count. Refuse-with-list is simpler and more explainable to the user.
- Retention becomes two-tier: OM-level TTL (last-referenced-at) + project-level TTL. Consent
  semantics (`consent=2` → keep Step-1 only) still applies at project level; the OM/detection layer
  is unlabelled crown data by definition, so it maps naturally onto consent tier 2.
- Ownership: OMs are per-`user_id`, same as projects. Cross-user sharing is a separate feature —
  explicitly out of scope for v1.

## 8. Cross-OM clustering — a real ML caveat, not just plumbing

Joint clustering assumes crown embeddings are comparable across OMs. If OMs differ in GSD,
sensor, season or lighting, DINOv2 features will separate **by OM** rather than by species, and the
user gets clean-looking but meaningless clusters.

Mitigations, cheap → expensive:

- **Show a cluster × OM contingency table + the t-SNE coloured by OM** on the review screen. Costs
  almost nothing (we already have `image_stem` per crown) and makes the failure mode visible instead
  of silent. **Do this in v1.**
- Warn at selection time when selected OMs have very different GSD.
- Later: per-OM feature standardisation, or resampling all OMs to a common GSD before cropping
  (`scripts/prep_lowres.py` already does something in this direction).

Also: keep the current EPSG rule strict for v1 — `cfg.SOURCE_EPSG` is a single value used by step 2
and the KMZ export, so **require all selected OMs to share an EPSG** and reject otherwise.
Reprojection is a later feature.

## 9. Suggested phasing

1. **P0 — no schema change.** Cooperative cancel + `CANCELLED` state + timeout watchdog. Fix the
   `step1_crop_crowns` O(crowns × OMs) loop. Allow N orthos per project (drop set-semantics) — this
   alone gives "multiple OMs in one project" with shared params, as a stopgap.
2. **P1 — the split.** Alembic + `Orthomosaic` / `DetectionRun` / `ProjectDetection` tables, storage
   re-layout with symlinks, param split, backfill script, new endpoints. Frontend: OM library screen.
3. **P2 — polish.** Save vs. save-and-publish, multi-OM STAC footprint union, OM refcount/retention,
   cluster × OM diagnostics, per-OM overlay previews.
4. **P3 — infra.** Move dispatch onto real Celery + Redis and swap cooperative cancel for hard
   `revoke(terminate=True)`; per-stage time limits.

## 10. Open decisions needed before P1

- Alembic now, or one-shot backfill script? (Recommend Alembic.)
- Redis + Celery worker mandatory in deployment, or keep the no-broker local mode? (Determines
  whether hard-kill cancel is available at all.)
- Existing projects' already-issued FileBrowser shares under the new publish gate: revoke or
  grandfather?
- Mixed-EPSG OMs: hard-reject in v1 (recommended) or reproject?
- Are OMs private per user, or shareable across a team? (Affects the ownership model now, painful to
  retrofit later.)
