"""Celery tasks that run the pipeline.

The work is split into two jobs, with the human labelling step between them:
  * ``job_a_analyze`` runs Step 0 detection, once per orthomosaic, then Step 1:
    crop, features, cluster, k analysis and t-SNE. It leaves the project in
    ``AWAITING_LABELS``.
  * ``job_b_finalize`` runs Step 2 species assignment, Step 3 validation when
    there is ground truth, and Step 4 KMZ export. It leaves the project
    ``COMPLETED``.

The heavy pipeline modules (torch, detectron2, detectree2, rasterio and so on)
are imported inside the tasks rather than at the top, so the FastAPI process and
this module can be imported on a machine without the ML stack installed.
"""
import csv
import json
import os
import shutil
import sys
import threading
import traceback
from datetime import datetime

# Turn off tqdm progress bars in the worker. When output goes to a file, tqdm's
# carriage returns do not overwrite anything, so every tick is kept, which makes
# a run log about ten times larger and buries the useful lines. tqdm reads
# TQDM_DISABLE when it is imported, so this has to run before the pipeline
# imports inside the tasks. setdefault leaves an explicit override in place.
os.environ.setdefault("TQDM_DISABLE", "1")

from app.core.failures import classify as classify_failure
from app.core.logging import get_logger, naive_now, with_context
from app.core.storage import ensure_project_dirs, project_paths, reset_dirs
from app.db import models
from app.db.session import SessionLocal
from app.services.pipeline_adapter import build_config
from app.workers.celery_app import celery_app

log = get_logger("app.pipeline")

# Loaded models, cached once per worker process.
_PREDICTORS: dict = {}
_DINOV2: dict = {}
# Building a predictor or DINOv2 model allocates GPU memory, so only one thread
# may do it at a time. Without the lock, two jobs that miss the same cache key
# both build the model and the second allocation can exhaust the GPU.
_MODEL_LOCK = threading.Lock()


class _ThreadRoutedStream:
    """A stdout/stderr replacement that sends each write to the writing thread's
    own log file as well as the real stream.

    Jobs can run at the same time in this process: local dispatch uses a daemon
    thread, and ``/compute/*`` calls ``.apply()`` on a request thread.
    ``contextlib.redirect_stdout`` does not work for capturing per-job logs
    there, because it replaces the process-wide ``sys.stdout``. Overlapping jobs
    would write into each other's log, and unwinding in a different order than
    they started restores a file that is already closed, after which every write
    in the process is silently lost.

    This proxy is installed once and never removed. A write goes to the real
    stream and to whatever file the calling thread registered, so two jobs
    capture side by side and a finished job's closed file is simply forgotten by
    that thread.
    """

    def __init__(self, real):
        self._real = real
        self._local = threading.local()

    # Registering the calling thread's log file.
    def push(self, sink):
        prev = getattr(self._local, "sink", None)
        self._local.sink = sink
        return prev

    def pop(self, prev):
        self._local.sink = prev

    # The parts that make this look like a file.
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
    """Return the proxy for 'stdout' or 'stderr', installing it the first time.

    It is installed on first use rather than at import, so importing this module
    never changes the process streams. The logging handlers hold a reference to
    the original ``sys.stdout`` object from ``configure_logging``, so swapping
    it here does not affect container log collection.
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
    """Return a DefaultPredictor, building and caching it if needed.

    Each argument is fixed into the predictor when it is built, so each one has
    to be part of the cache key. Leaving one out would let two projects with
    different values share whichever predictor was built first, producing wrong
    results with no error and nothing in the logs.
    """
    import predict  # imported here to keep the ML stack out of the API process

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
        # Check again: another thread may have built it while we waited.
        if key not in _PREDICTORS:
            _PREDICTORS[key] = predict.build_predictor(
                model_path,
                conf_threshold=conf_threshold,
                detections_per_image=detections_per_image,
                min_size_test=min_size_test,
            )
        return _PREDICTORS[key]


def _get_dinov2(model_name: str, img_size: int):
    import tree_crown_pipeline as tcp  # imported here for the same reason

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


def _set_state(db, project, state, error=None):
    project.state = state
    if error is not None:
        project.error = error
    db.add(project)
    db.commit()


def _job_tracking_id(job, request_id: str | None) -> str | None:
    """Keep the orchestrator's idempotency key if the API already stored one."""
    return job.celery_task_id or request_id


def _read_recommended_k(dir_cluster: str):
    path = os.path.join(dir_cluster, "k_recommendation_table.csv")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            rows = list(csv.DictReader(f))
        # the table is already sorted, best k first
        return int(float(rows[0]["k"])) if rows else None
    except Exception:
        return None


# Job A: detect crowns and cluster them. Ends at AWAITING_LABELS.
@celery_app.task(name="app.workers.tasks.job_a_analyze", bind=True)
def job_a_analyze(self, project_id: str, job_id: str):
    db = SessionLocal()
    logf = None
    project = db.get(models.Project, project_id)
    job = db.get(models.Job, job_id)
    # Set the correlation ids here, inside the task body. Local dispatch runs
    # this in a daemon thread, and ContextVars are not inherited across threads.
    with with_context(
        job_id=job_id,
        project_id=project_id,
        dag_run_id=(getattr(job, "celery_task_id", None) or ""),
        stage="analyze",
    ):
      try:
        run = project.current_run or 1
        paths = ensure_project_dirs(project_id, run)
        # Start clean: remove anything an earlier attempt at this run produced,
        # so an old feature or cluster file cannot be mistaken for new output.
        # Other runs (run_1, run_2 and so on) are left alone.
        #
        # Every key below is inside work/run_<n>/. Note in particular that
        # "ortho" means work/run_<n>/ortho, the per-run working copy the pipeline
        # reads, and not "input_ortho", the upload library that persists across
        # runs and holds files the server cannot rebuild. They are separate keys
        # pointing at separate trees, and reset_dirs rejects the input keys
        # outright, so no later edit to this list can delete a user's uploads.
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
        _set_state(db, project, "ANALYZING")

        cfg = build_config(project)

        with _redirect(logf):
            import predict
            import tree_crown_pipeline as tcp

            # Step 0: detect crowns in this run's orthomosaics.
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
                # Hand Step 1 the orthomosaic at the resolution detection used,
                # and the polygons named after it.
                shutil.copy(used, os.path.join(paths["ortho"], f"{stem}.tif"))
                shutil.copy(gj, os.path.join(paths["polygons"], f"{stem}.geojson"))

            # Step 1: crop, extract features, cluster, analyse k, then t-SNE.
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

        # Pass the recommended k on to the review step.
        project.available_k = list(cfg.K_LIST)
        project.recommended_k = _read_recommended_k(dir_cluster)
        db.add(project)
        db.commit()

        _set_job(db, job, state="SUCCEEDED", current_stage="done",
                 progress=1.0, finished_at=naive_now())
        _set_state(db, project, "AWAITING_LABELS")

      except Exception as e:
        _fail(db, project_id, job_id, e)
        raise
      finally:
        if logf:
            logf.close()
        db.close()


# Job B: assign species, validate, and export. Ends at COMPLETED.
@celery_app.task(name="app.workers.tasks.job_b_finalize", bind=True)
def job_b_finalize(self, project_id: str, job_id: str):
    db = SessionLocal()
    logf = None
    project = db.get(models.Project, project_id)
    job = db.get(models.Job, job_id)
    # Set the correlation ids inside the task body, for the reason given in
    # job_a_analyze.
    with with_context(
        job_id=job_id,
        project_id=project_id,
        dag_run_id=(getattr(job, "celery_task_id", None) or ""),
        stage="finalize",
    ):
      try:
        run = project.current_run or 1
        paths = ensure_project_dirs(project_id, run)
        # Clear the output of any earlier finalize, for instance after
        # relabelling, so old and new species assignments cannot be mixed.
        reset_dirs(project_id, ["step2_output", "step3_output", "step4_output"], run)

        logf = open(os.path.join(paths["logs"], "finalize.log"), "a", buffering=1)
        _set_job(db, job, state="RUNNING", started_at=naive_now(),
                 celery_task_id=_job_tracking_id(job, self.request.id),
                 log_path=logf.name)
        _set_state(db, project, "FINALIZING")

        cfg = build_config(project)
        labels = db.query(models.ClusterLabel).filter_by(project_id=project_id).all()
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

            # Write a STAC Item for this run: its footprint, parameters and
            # files. A failure here must not fail a run that otherwise worked.
            _set_job(db, job, current_stage="stac", progress=0.95)
            try:
                from app.services.stac import write_stac_item

                write_stac_item(project, chosen_k=cfg.CHOSEN_K)
            except Exception:  # pragma: no cover - best effort
                log.warning("stac emission skipped", exc_info=True)

        _set_job(db, job, state="SUCCEEDED", current_stage="done",
                 progress=1.0, finished_at=naive_now())
        _set_state(db, project, "COMPLETED")

      except Exception as e:
        _fail(db, project_id, job_id, e)
        raise
      finally:
        if logf:
            logf.close()
        db.close()


# Helpers.
def _redirect(logf):
    """Capture this thread's stdout and stderr into ``logf`` for the block.

    This affects the calling thread only, not the whole process; see
    ``_ThreadRoutedStream``. It restores whatever the thread was writing to
    before, so these can be nested and a concurrent job's capture is untouched.
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
    """Which orthomosaic stems this run should process, normally just one.

    A project's ``input/ortho`` directory is a library that persists across runs,
    so simply listing it would make every run detect over every orthomosaic ever
    uploaded. ``_apply_run_config`` records the run's choice in
    ``project.params["ortho_stem"]`` when the run is triggered.

    The fallbacks below are only for runs made before that key existed:

    * the key is set and its file exists, so return that one. This is the only
      case a run triggered through the current API reaches.
    * no key and exactly one registered orthomosaic, so return it. This covers
      projects created before the change, including one that was mid-run when
      the new code was deployed.
    * no key and several, so return them all, which is what happened before the
      library existed. The API cannot reach this, because the trigger rejects an
      ambiguous request with 400 ORTHO_SELECTION_REQUIRED. It stays so an older
      job already in the queue does not fail on a rule added after it started.

    Only stems whose file is present are returned, so a row left behind by an
    upload that did not finish cannot fail the whole run.
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
    # No rows in the database, or none of their files are on disk. Fall back to
    # listing the directory, which is what this function used to do.
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


def _fail(db, project_id: str, job_id: str, exc: Exception):
    tb = traceback.format_exc()
    job = db.get(models.Job, job_id)
    project = db.get(models.Project, project_id)
    stage = getattr(job, "current_stage", None) if job else None
    # How long the job ran, in milliseconds, if it recorded a start time.
    duration_ms = None
    started_at = getattr(job, "started_at", None) if job else None
    if started_at is not None:
        try:
            duration_ms = int(
                (naive_now() - started_at).total_seconds() * 1000
            )
        except Exception:
            duration_ms = None
    # One error line, which also goes to errors.jsonl, carrying the correlation
    # ids the task body already set through with_context.
    log.error(
        "job %s failed during stage=%s project=%s duration_ms=%s",
        job_id, stage, project_id, duration_ms,
        exc_info=exc,
    )
    if job:
        # Also write the traceback into the run's own log file, so that file
        # explains the failure on its own. Without this it just stops part-way
        # through a step and the error is only in the Job.error column.
        _write_failure_to_log(getattr(job, "log_path", None), job_id, exc, tb)
        _set_job(db, job, state="FAILED", error=tb, finished_at=naive_now())
    if project:
        # Store the classified failure rather than str(exc), so a GPU running
        # out of memory, a full disk and a truncated GeoTIFF each get their own
        # code and message instead of a single COMPUTE_FAILED with whatever text
        # the library used. The raw text is still kept under details.
        _set_state(db, project, "FAILED",
                   error=json.dumps(classify_failure(exc, stage=stage)))


def _write_failure_to_log(log_path, job_id: str, exc: Exception, tb: str) -> None:
    """Append a marked-out failure block to the run log, holding the timestamp,
    the exception and the traceback. Best effort, and never raises, because this
    is already the failure path."""
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
