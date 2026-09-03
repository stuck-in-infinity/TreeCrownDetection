"""Celery tasks wrapping the pipeline.

Two jobs, with the user's labelling step in between:
  * ``job_a_analyze``  — Step 0 detect (once per ortho) -> Step 1 crop, features,
    cluster, k-analysis, t-SNE. Ends with the project in ``AWAITING_LABELS``.
  * ``job_b_finalize`` — Step 2 assign -> Step 3 validate (if ground truth) ->
    Step 4 KMZ export. Ends with the project ``COMPLETED``.

Heavy pipeline modules (torch, detectron2, detectree2, rasterio, ...) are
imported lazily *inside* the tasks so the FastAPI process and this module can be
imported without the ML stack installed.
"""
import csv
import json
import os
import shutil
import sys
import threading
import traceback
from datetime import datetime

# Silence tqdm progress bars in the worker. Redirected to a file, tqdm's \r
# updates don't overwrite — every tick is saved, bloating run logs ~10x and
# drowning the real signal. tqdm reads TQDM_DISABLE at import, so this must run
# BEFORE the lazy pipeline imports (predict / tree_crown_pipeline / detectree2)
# inside the tasks. setdefault so an explicit env override still wins.
os.environ.setdefault("TQDM_DISABLE", "1")

from app.core.failures import classify as classify_failure
from app.core.logging import get_logger, naive_now, with_context
from app.core.storage import ensure_project_dirs, project_paths, reset_dirs
from app.db import models
from app.db.session import SessionLocal
from app.services.pipeline_adapter import build_config
from app.workers.celery_app import celery_app

log = get_logger("app.pipeline")

# ── warm model caches (one per worker process) ─────────────────────────
_PREDICTORS: dict = {}
_DINOV2: dict = {}
# Building a predictor / DINOv2 allocates GPU memory, so the cache MISS path has
# to be serialised: without this, two concurrent jobs that miss the same key both
# build the model and the second allocation can OOM the device.
_MODEL_LOCK = threading.Lock()


class _ThreadRoutedStream:
    """Process-global stdout/stderr proxy that routes writes per thread.

    Jobs run concurrently *in this process* — local dispatch uses a daemon
    thread, and ``/compute/*`` calls ``.apply()`` inside the request threadpool.
    ``contextlib.redirect_stdout`` cannot be used for per-job log capture there:
    it swaps the process-global ``sys.stdout``, so overlapping jobs interleave
    into each other's log, and unwinding out of order restores a *closed* file
    that silently swallows every later write in the process.

    This proxy is installed once and never removed. Each write goes to the real
    stream plus whatever sink the *calling thread* has registered, so two jobs
    capture cleanly side by side and a finished job's closed file is simply
    dropped from its own thread's state.
    """

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    # -- per-thread sink registration --
    def push(self, sink):
        prev = getattr(self._local, "sink", None)
        self._local.sink = sink
        return prev

    def pop(self, prev):
        self._local.sink = prev

    # -- file-like surface --
    def write(self, data):
        try:
            self._real.write(data)
        except Exception:
            pass
        sink = getattr(self._local, "sink", None)
        if sink is not None:
            try:
                sink.write(data)
            except Exception:
                pass

    def flush(self):
        for s in (self._real, getattr(self._local, "sink", None)):
            if s is None:
                continue
            try:
                s.flush()
            except Exception:
                pass

    def isatty(self):
        return False

    def fileno(self):
        return self._real.fileno()


_STREAM_LOCK = threading.Lock()
_PROXIES: dict = {}


def _proxy(name: str) -> _ThreadRoutedStream:
    """Install (once) and return the routed proxy for 'stdout' / 'stderr'.

    Installed lazily so importing this module never touches the process
    streams. The logging handlers hold a direct reference to the original
    ``sys.stdout`` object (bound in ``configure_logging``), so container log
    collection is unaffected by the swap.
    """
    with _STREAM_LOCK:
        proxy = _PROXIES.get(name)
        if proxy is None:
            proxy = _ThreadRoutedStream(getattr(sys, name))
            _PROXIES[name] = proxy
            setattr(sys, name, proxy)
        return proxy


def _get_predictor(
    model_path: str,
    conf_threshold: float,
    detections_per_image: int = 6,
    min_size_test: int = 512,
):
    """Return a cached DefaultPredictor.

    Every argument here is baked into the predictor at construction time, so
    every argument must appear in the cache key. Leaving one out would make
    two projects with different values silently share whichever predictor was
    built first — wrong results, no error, nothing in the logs.
    """
    import predict  # lazy

    key = (
        model_path,
        round(float(conf_threshold), 4),
        int(detections_per_image),
        int(min_size_test),
    )
    hit = _PREDICTORS.get(key)
    if hit is not None:
        return hit
    with _MODEL_LOCK:
        # Re-check: another thread may have built it while we waited.
        if key not in _PREDICTORS:
            _PREDICTORS[key] = predict.build_predictor(
                model_path,
                conf_threshold=conf_threshold,
                detections_per_image=detections_per_image,
                min_size_test=min_size_test,
            )
        return _PREDICTORS[key]


def _get_dinov2(model_name: str, img_size: int):
    import tree_crown_pipeline as tcp  # lazy

    key = (model_name, img_size)
    hit = _DINOV2.get(key)
    if hit is not None:
        return hit
    with _MODEL_LOCK:
        if key not in _DINOV2:
            _DINOV2[key] = tcp.build_dinov2(model_name, img_size)
        return _DINOV2[key]


def _set_job(db, job, **fields):
    for k, v in fields.items():
        setattr(job, k, v)
    db.add(job)
    db.commit()


def _set_state(db, project, state, error=None, run=None):
    """Record an outcome for ONE run, then re-derive the project's state.

    This used to write ``project.state`` directly, which could only ever
    describe the active run: finalize an older run and the project ended up
    reporting that run's outcome instead of its own. ``run_registry`` owns both
    halves now — the run row is the truth, the project's state is derived from
    the rows.
    """
    from app.services import run_registry

    if error is not None:
        project.error = error
        db.add(project)
        db.commit()
    n = run if run is not None else (project.current_run or 1)
    run_registry.set_run_state(db, project, n, state, error=error)
    db.refresh(project)


def _job_tracking_id(job, request_id: str | None) -> str | None:
    """Preserve an orchestrator idempotency key once the API has recorded it."""
    return job.celery_task_id or request_id


def _read_recommended_k(dir_cluster: str):
    path = os.path.join(dir_cluster, "k_recommendation_table.csv")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
        # table is sorted with rank 1 first
        return int(float(rows[0]["k"])) if rows else None
    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════
# JOB A — detect + cluster  (ends at AWAITING_LABELS)
# ═══════════════════════════════════════════════════════════════════════
@celery_app.task(name="app.workers.tasks.job_a_analyze", bind=True)
def job_a_analyze(self, project_id: str, job_id: str, run: int | None = None):
    db = SessionLocal()
    logf = None
    project = db.get(models.Project, project_id)
    job = db.get(models.Job, job_id)
    # Bind correlation context INSIDE the task body: local-dispatch runs this in
    # a daemon thread and ContextVars don't inherit across threads.
    with with_context(
        job_id=job_id,
        project_id=project_id,
        dag_run_id=(getattr(job, "celery_task_id", None) or ""),
        stage="analyze",
    ):
      try:
        # The run this task was dispatched for. Falls back to the active run,
        # which is right for analyze (it always computes the newest) and is the
        # old behaviour for every existing caller.
        run = run or (project.current_run or 1)
        paths = ensure_project_dirs(project_id, run)
        # fresh analysis: drop any derived artifacts from a previous attempt of
        # THIS run so a stale feature/cluster cache cannot leak in. Sibling runs
        # (run_1, run_2, ...) are untouched.
        #
        # SAFETY — every key below is run-scoped (work/run_<n>/...). In
        # particular "ortho" is work/run_<n>/ortho, the per-run WORKING COPY the
        # pipeline reads, NOT "input_ortho" (input/ortho), the shared upload
        # library that now persists across runs and holds files no upload path
        # could reconstruct. The two are different keys in project_paths, they
        # resolve to different trees, and reset_dirs now refuses the input keys
        # outright (storage._PROTECTED_KEYS), so this list cannot delete a user's
        # orthomosaics however it is later edited.
        reset_dirs(
            project_id,
            ["detectree", "ortho", "polygons", "step1_output",
             "step2_output", "step3_output", "step4_output"],
            run,
        )

        logf = open(os.path.join(paths["logs"], "analyze.log"), "a", buffering=1)
        _set_job(db, job, state="RUNNING", started_at=naive_now(),
                 celery_task_id=_job_tracking_id(job, self.request.id),
                 log_path=logf.name)
        _set_state(db, project, "ANALYZING", run=run)

        cfg = build_config(project)

        with _redirect(logf):
            import predict
            import tree_crown_pipeline as tcp

            # ── Step 0: detection, on THIS RUN'S ortho ──────────────────
            _set_job(db, job, current_stage="detecting", progress=0.05)
            ortho_dir = paths["input_ortho"]
            stems = _run_ortho_stems(project, ortho_dir)
            if not stems:
                raise RuntimeError("No orthomosaics uploaded.")

            predictor = _get_predictor(
                cfg.DETECTREE_MODEL,
                cfg.CONF_THRESHOLD,
                detections_per_image=cfg.DETECTIONS_PER_IMAGE,
                min_size_test=cfg.MIN_SIZE_TEST,
            )
            for stem in stems:
                src_ortho = _find_ortho(ortho_dir, stem)
                det_out = os.path.join(paths["detectree"], stem)
                gj, _overlay, used = predict.run_detectree2_pipeline(
                    ortho_path=src_ortho,
                    predictor=predictor,
                    output_dir=det_out,
                    tile_size=cfg.TILE_SIZE,
                    buffer=cfg.BUFFER,
                    iou_threshold=cfg.IOU_THRESHOLD,
                    conf_threshold=cfg.CONF_THRESHOLD,
                    area_min=cfg.AREA_MIN,
                    area_max=cfg.AREA_MAX,
                    full_coverage=cfg.FULL_COVERAGE,
                )
                # feed Step 1: same-resolution ortho + per-ortho-prefixed polygons
                shutil.copy(used, os.path.join(paths["ortho"], f"{stem}.tif"))
                shutil.copy(gj, os.path.join(paths["polygons"], f"{stem}.geojson"))

            # ── Step 1: crop -> features -> cluster -> analyse -> t-SNE ─
            _set_job(db, job, current_stage="cropping", progress=0.35)
            crowns_dir = tcp.step1_crop_crowns(cfg)

            _set_job(db, job, current_stage="extracting_features", progress=0.50)
            model = _get_dinov2(cfg.MODEL_NAME, cfg.IMG_SIZE)
            X, names_df, _ = tcp.step1_extract_features(cfg, crowns_dir, model=model)

            _set_job(db, job, current_stage="clustering", progress=0.70)
            all_labels, inertia, sil, db_vals, dir_cluster = tcp.step1_cluster(
                cfg, X, names_df, crowns_dir
            )

            _set_job(db, job, current_stage="analyzing_k", progress=0.85)
            tcp.step1_analyze_k(cfg, inertia, sil, db_vals, dir_cluster)

            _set_job(db, job, current_stage="tsne", progress=0.92)
            tcp.step1_tsne(cfg, X, names_df, all_labels, dir_cluster)

        # surface k recommendation for the review step
        project.available_k = list(cfg.K_LIST)
        project.recommended_k = _read_recommended_k(dir_cluster)
        db.add(project)
        db.commit()
        from app.services.run_registry import mirror
        mirror(db, project)

        _set_job(db, job, state="SUCCEEDED", current_stage="done",
                 progress=1.0, finished_at=naive_now())
        _set_state(db, project, "AWAITING_LABELS", run=run)

      except Exception as e:
        _fail(db, project_id, job_id, e, run=run)
        raise
      finally:
        if logf:
            logf.close()
        db.close()


# ═══════════════════════════════════════════════════════════════════════
# JOB B — assign + validate + export  (ends at COMPLETED)
# ═══════════════════════════════════════════════════════════════════════
@celery_app.task(name="app.workers.tasks.job_b_finalize", bind=True)
def job_b_finalize(self, project_id: str, job_id: str, run: int | None = None):
    db = SessionLocal()
    logf = None
    project = db.get(models.Project, project_id)
    job = db.get(models.Job, job_id)
    # Bind correlation context INSIDE the task body (daemon-thread safe; see
    # job_a_analyze note).
    with with_context(
        job_id=job_id,
        project_id=project_id,
        dag_run_id=(getattr(job, "celery_task_id", None) or ""),
        stage="finalize",
    ):
      try:
        # The run this task was dispatched for. Falls back to the active run,
        # which is right for analyze (it always computes the newest) and is the
        # old behaviour for every existing caller.
        run = run or (project.current_run or 1)
        paths = ensure_project_dirs(project_id, run)
        # clean outputs from any previous finalize (e.g. after re-labeling) so
        # results never mix old and new species assignments.
        reset_dirs(project_id, ["step2_output", "step3_output", "step4_output"], run)

        logf = open(os.path.join(paths["logs"], "finalize.log"), "a", buffering=1)
        _set_job(db, job, state="RUNNING", started_at=naive_now(),
                 celery_task_id=_job_tracking_id(job, self.request.id),
                 log_path=logf.name)
        _set_state(db, project, "FINALIZING", run=run)

        cfg = build_config(project)
        # Scoped to THIS run. A project now keeps every run's labels, so an
        # unscoped query would export run 4's species map into run 2's folder.
        from app.services import run_registry
        run_row = run_registry.get_run(db, project, run)
        labels = (
            db.query(models.ClusterLabel)
            .filter_by(project_id=project_id,
                       run_id=(run_row.id if run_row else None))
            .all()
        )
        if not labels:
            raise RuntimeError("No labels submitted.")
        cfg.CHOSEN_K = labels[0].chosen_k

        with _redirect(logf):
            import tree_crown_pipeline as tcp

            _set_job(db, job, current_stage="assigning", progress=0.20)
            tcp.step2_assign_species(cfg)

            if _has_ground_truth(cfg.GROUND_TRUTH_CSV):
                _set_job(db, job, current_stage="validating", progress=0.55)
                tcp.step3_validate(cfg)

            _set_job(db, job, current_stage="exporting", progress=0.85)
            tcp.step4_export_kmz(cfg)

            # Emit a STAC Item describing this run (footprint + params + assets).
            # Non-fatal: a STAC failure must not fail an otherwise-good run.
            _set_job(db, job, current_stage="stac", progress=0.95)
            try:
                from app.services.stac import write_stac_item

                write_stac_item(project, chosen_k=cfg.CHOSEN_K)
            except Exception:  # pragma: no cover - best effort
                log.warning("STAC item not written", exc_info=True)

        _set_job(db, job, state="SUCCEEDED", current_stage="done",
                 progress=1.0, finished_at=naive_now())
        _set_state(db, project, "COMPLETED", run=run)

      except Exception as e:
        _fail(db, project_id, job_id, e, run=run)
        raise
      finally:
        if logf:
            logf.close()
        db.close()


# ── helpers ────────────────────────────────────────────────────────────
def _redirect(logf):
    """Capture this thread's stdout/stderr into ``logf`` for the duration.

    Thread-scoped, not process-scoped — see ``_ThreadRoutedStream``. Restores
    whatever sink the thread had before, so nesting is safe and a concurrent
    job's capture is never disturbed.
    """
    from contextlib import contextmanager

    @contextmanager
    def _capture():
        out, err = _proxy("stdout"), _proxy("stderr")
        prev_out, prev_err = out.push(logf), err.push(logf)
        try:
            yield
        finally:
            out.pop(prev_out)
            err.pop(prev_err)

    return _capture()


def _run_ortho_stems(project, ortho_dir: str) -> list[str]:
    """The ortho stem(s) this run must process — normally exactly one.

    A project's ``input/ortho`` directory is now a LIBRARY that persists across
    runs, so listing the directory (what this used to do) would make every run
    detect over every ortho ever uploaded. The run's ortho is pinned on
    ``project.params["ortho_stem"]`` by ``_apply_run_config`` at trigger time.

    The fallbacks exist for runs that predate the pin, and only for those:

    * pin present and its file exists -> that one file. This is the only path a
      run triggered through the current API can take.
    * no pin, exactly one registered ortho -> that one. Every project created
      before this change, including one mid-run when the code was deployed.
    * no pin and several -> every registered ortho, the pre-library behaviour.
      Unreachable through the API (the trigger rejects an ambiguous request with
      400 ORTHO_SELECTION_REQUIRED); kept so an in-flight legacy job cannot
      crash on a rule that did not exist when it was queued.

    Only stems whose file is actually present are returned, so a row left behind
    by a half-finished upload cannot fail the whole run.
    """
    def _present(stem: str) -> bool:
        return any(
            os.path.exists(os.path.join(ortho_dir, stem + ext))
            for ext in (".tif", ".tiff")
        )

    params = dict(getattr(project, "params", None) or {})
    pinned = params.get("ortho_stem")
    if pinned and _present(pinned):
        return [pinned]

    registered = [o.stem for o in (getattr(project, "orthos", None) or [])]
    if pinned:
        log.warning(
            "pinned ortho '%s' missing on disk for project=%s; falling back",
            pinned, getattr(project, "id", None),
        )
    if len(registered) == 1 and _present(registered[0]):
        return registered

    usable = sorted(s for s in registered if _present(s))
    if usable:
        return usable
    # No DB rows at all (or none on disk) — fall back to the directory, which is
    # exactly what this function replaced.
    return sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(ortho_dir)
        if f.lower().endswith((".tif", ".tiff"))
    )


def _find_ortho(ortho_dir: str, stem: str) -> str:
    for ext in (".tif", ".tiff"):
        cand = os.path.join(ortho_dir, stem + ext)
        if os.path.exists(cand):
            return cand
    raise FileNotFoundError(f"Ortho for stem '{stem}' not found in {ortho_dir}")


def _has_ground_truth(gt_dir: str) -> bool:
    if not os.path.isdir(gt_dir):
        return False
    return any(
        os.path.isdir(os.path.join(gt_dir, d)) for d in os.listdir(gt_dir)
    )


def _fail(db, project_id: str, job_id: str, exc: Exception, run: int | None = None):
    tb = traceback.format_exc()
    job = db.get(models.Job, job_id)
    project = db.get(models.Project, project_id)
    stage = getattr(job, "current_stage", None) if job else None
    # Duration (ms) since the job started, if we have a start timestamp.
    duration_ms = None
    started_at = getattr(job, "started_at", None) if job else None
    if started_at is not None:
        try:
            duration_ms = int(
                (naive_now() - started_at).total_seconds() * 1000
            )
        except Exception:
            duration_ms = None
    # Durable, structured ERROR line (also lands in errors.jsonl) keyed by the
    # correlation ids already bound in the task body's with_context block.
    log.error(
        "job %s failed during stage=%s project=%s duration_ms=%s",
        job_id, stage, project_id, duration_ms,
        exc_info=exc,
    )
    if job:
        # Also append the traceback to the run's own .log file so the log is
        # self-sufficient for RCA — otherwise the file just stops mid-step and
        # the error lives only in the DB Job.error column.
        _write_failure_to_log(getattr(job, "log_path", None), job_id, exc, tb)
        _set_job(db, job, state="FAILED", error=tb, finished_at=naive_now())
    if project:
        # Store the CLASSIFIED failure, not str(exc). Before this, every run
        # failure — GPU out of memory, disk full, a truncated GeoTIFF — came
        # back as the same COMPUTE_FAILED code with whatever text the library
        # that raised happened to use. The raw text is still kept, inside
        # details, so nothing is lost by interpreting it.
        # `run` is threaded in so a failure lands on the run that failed. The
        # active run is the right default and the only possibility for analyze.
        _set_state(db, project, "FAILED", run=run,
                   error=json.dumps(classify_failure(exc, stage=stage)))


def _write_failure_to_log(log_path, job_id: str, exc: Exception, tb: str) -> None:
    """Append a clearly-marked failure block (timestamp + exception + traceback)
    to the run log. Best-effort: never raise from the failure path."""
    if not log_path:
        return
    try:
        from app.core.logging import now_ist
        ts = now_ist().strftime("%Y-%m-%d %H:%M:%S IST")
        with open(log_path, "a", buffering=1) as f:
            f.write(
                f"\n{'='*70}\n"
                f"ERROR  {ts}  job={job_id}\n"
                f"{type(exc).__name__}: {exc}\n"
                f"{'-'*70}\n{tb}\n{'='*70}\n"
            )
    except Exception:
        pass
