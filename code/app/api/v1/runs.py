"""The run triggers the frontend calls.

These endpoints return straight away. Each one moves the project into its
in-progress state, starts the work as an Airflow DAG run or a local background
thread, and returns the run id. The frontend then polls GET /projects/{id} until
the project reaches AWAITING_LABELS, COMPLETED or FAILED.

The in-progress states are deliberately not states a run can start from, and the
transition into them is a single conditional update. A second trigger arriving
while a run is going therefore gets 409 CONFLICT_BUSY, instead of racing the
first one into the destructive reset_dirs.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_project, require_api_key, resolve_project
from app.core.logging import ERROR_CODES, get_logger, naive_now, request_id_var
from app.core.models_registry import resolve_backbone, resolve_model_path
from app.core.storage import ensure_project_dirs
from app.db import models
from app.db.session import get_db
from app.schemas.project import AnalyzeTrigger, FinalizeTrigger
from app.services.airflow_client import airflow_enabled
from app.services.project_service import USED_RUN_STATES, archive_current_run
from app.services.run_dispatch import dispatch_analyze, dispatch_finalize
from app.services.state import transition_if

router = APIRouter()

log = get_logger("app.api")


def _current_request_id():
    """Read the request id the audit middleware set for this request.

    The default is "-", meaning there is no request, in which case return None
    for the nullable column.
    """
    try:
        rid = request_id_var.get()
    except LookupError:
        return None
    return rid if rid and rid != "-" else None


def _classify_dispatch_error(exc: Exception) -> tuple[str, str]:
    """Turn a dispatch RuntimeError, which carries the reason from
    airflow_client, into an error code and a hint for the 502 response."""
    text = str(exc).lower()
    if "http" in text and ("returned" in text or "status" in text):
        return ERROR_CODES["AIRFLOW_HTTP_ERROR"], (
            "the run scheduler rejected the request — nothing was started, so "
            "you can safely try again; if it keeps happening, ask an "
            "administrator to check the Airflow DAG and its logs"
        )
    return ERROR_CODES["AIRFLOW_UNREACHABLE"], (
        "the run scheduler could not be reached — nothing was started, so you "
        "can safely try again in a minute; if it persists, ask an administrator "
        "to check that Airflow is running"
    )


# A new run can only start from these states; the in-progress ones are left out
# on purpose. Starting from a state whose run has already produced results
# archives that run and opens a new work/run_<n+1> folder.
_ANALYZE_FROM = {"UPLOADED", "AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED", "FAILED"}
_FINALIZE_FROM = {"LABELS_SUBMITTED", "COMPLETED", "FAILED"}
_BUSY = {"ANALYZING", "FINALIZING"}


def _gate(db: Session, project, allowed: set[str], in_progress_state: str, action: str):
    """Take the in-progress state for this run, or raise the matching 409."""
    if not transition_if(db, project, allowed, in_progress_state):
        db.refresh(project)
        if project.state in _BUSY:
            log.warning("409 CONFLICT_BUSY project=%s action=%s state=%s",
                        project.id, action, project.state)
            raise HTTPException(409, {
                "code": ERROR_CODES["CONFLICT_BUSY"],
                "message": f"A run is already in progress (state {project.state})",
                "project_id": project.id,
                "hint": ("one run at a time per project — wait for this one to "
                         "finish, then start the next"),
            })
        log.warning("409 INVALID_STATE project=%s action=%s state=%s allowed=%s",
                    project.id, action, project.state, sorted(allowed))
        raise HTTPException(409, {
            "code": ERROR_CODES["INVALID_STATE"],
            "message": f"Cannot {action} from state {project.state} (allowed: {sorted(allowed)})",
            "project_id": project.id,
            "hint": "reach an allowed state before triggering this run",
        })


def _new_job(db: Session, project, job_type: str):
    job = models.Job(
        project_id=project.id, type=job_type, state="QUEUED",
        started_at=naive_now(), request_id=_current_request_id(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def _fail_trigger(db: Session, project, job, exc: Exception) -> None:
    job.state = "FAILED"
    job.error = str(exc)
    job.finished_at = naive_now()
    db.add(job)
    db.commit()
    # Only roll back the in-progress state this trigger took. Assigning FAILED
    # directly could overwrite a state something else has properly moved on to.
    if transition_if(db, project, _BUSY, "FAILED"):
        project.error = str(exc)
        db.add(project)
        db.commit()


def _mark_dispatched(db: Session, job, run_id: str) -> None:
    job.celery_task_id = run_id
    if airflow_enabled():
        job.state = "RUNNING"
    db.add(job)
    db.commit()


def _validate_trigger_body(project, body) -> None:
    """Reject a bad request body with 400 before any state is changed."""
    if not body:
        return
    try:
        if body.model_key is not None:
            resolve_model_path(body.model_key)
        if body.params and "model_name" in body.params:
            resolve_backbone(body.params.get("model_name"))
    except ValueError as e:
        raise HTTPException(400, {
            "code": "BAD_REQUEST", "message": str(e), "project_id": project.id,
            "hint": "correct the setting named in the message, then try again",
        })
    if body.params:
        from app.api.v1.projects import (
            _validate_merged_params,
            _validate_param_overrides,
        )
        _validate_param_overrides(body.params)
        _validate_merged_params(project, body.params)


def resolve_run_ortho(project, body):
    """Decide which orthomosaic this run uses, or raise the matching 4xx.

    There are three cases:

    * ``ortho_id`` was given, so it must belong to this project and its file
      must be on disk. An id from another project, or an unknown one, is 404
      ORTHO_NOT_FOUND rather than 403: the caller already owns this project, the
      id just is not one of its orthomosaics.
    * it was left out and the project holds exactly one, so use that one. This
      is what keeps older projects, frontend builds and scripts working.
    * it was left out and the project holds several, so 400
      ORTHO_SELECTION_REQUIRED, with the candidates in ``details`` so a client
      can show the choice without another call.

    This function only reads and validates; ``_apply_run_config`` is what saves
    the decision. That means it can also be called as a check before any state
    is changed.
    """
    orthos = list(project.orthos or [])
    if not orthos:
        log.warning("400 NO_ORTHO project=%s action=analyze", project.id)
        raise HTTPException(400, {
            "code": ERROR_CODES["NO_ORTHO"],
            "message": f"No orthomosaic uploaded for project {project.id}",
            "project_id": project.id,
            "hint": "upload a GeoTIFF in step 2 before starting a run",
        })

    requested = getattr(body, "ortho_id", None) if body else None
    from_pin = False
    if not requested and body is not None and getattr(body, "execution_id", None):
        # This is Airflow calling back into /project/drone_api or /analyze with
        # execution_id set. The DAG does not necessarily pass every conf key
        # through, so fall back to what the trigger already saved onto
        # project.params. This is not done for an ordinary user request, where
        # quietly reusing the last run's orthomosaic is exactly the ambiguity
        # the 400 below exists to catch.
        requested = (dict(project.params or {})).get("ortho_id")
        from_pin = bool(requested)
    if requested:
        match = next((o for o in orthos if o.id == requested), None)
        if match is not None:
            _assert_ortho_usable(project, match)
            return match
        if not from_pin:
            log.warning("404 ORTHO_NOT_FOUND project=%s ortho=%s", project.id, requested)
            raise HTTPException(404, {
                "code": ERROR_CODES["ORTHO_NOT_FOUND"],
                "message": "The selected orthomosaic does not belong to this project.",
                "project_id": project.id,
                "hint": ("reload the orthomosaic list in step 2 and tick one of its "
                     "entries — this one belongs to a different project"),
            })
        # The saved orthomosaic has since been deleted. Fall through to the
        # normal rules rather than 404 a callback the user cannot change.

    if len(orthos) == 1:
        only = orthos[0]
        _assert_ortho_usable(project, only)
        return only

    log.warning("400 ORTHO_SELECTION_REQUIRED project=%s n=%s", project.id, len(orthos))
    raise HTTPException(400, {
        "code": ERROR_CODES["ORTHO_SELECTION_REQUIRED"],
        "message": (
            f"This project holds {len(orthos)} orthomosaics — say which one this "
            f"run should use."
        ),
        "project_id": project.id,
        "hint": ("tick which orthomosaic this run should use in step 2 — the "
                     "choices are listed under details.orthos below"),
        "details": {"orthos": [
            {"id": o.id, "stem": o.stem, "filename": o.filename} for o in orthos
        ]},
    })


def _assert_ortho_usable(project, ortho) -> None:
    """Check the selected orthomosaic still has its file.

    A row whose file has gone, after a manual cleanup or an upload that did not
    finish, would otherwise fail inside the worker with a FileNotFoundError and
    leave the project FAILED.
    """
    from app.api.v1.projects import ortho_file_path
    from app.core.storage import project_paths

    paths = project_paths(project.id, project.current_run or 1)
    if ortho_file_path(paths, ortho) is None:
        log.warning("400 NO_ORTHO project=%s ortho=%s missing-file", project.id, ortho.id)
        raise HTTPException(400, {
            "code": ERROR_CODES["NO_ORTHO"],
            "message": (
                f"The file for '{ortho.filename}' is missing on the server, so it "
                f"cannot be analyzed."
            ),
            "project_id": project.id,
            "hint": ("upload the GeoTIFF again — it will be added alongside this "
                     "entry, and you can then pick the new one for the run"),
        })


def _pin_run_ortho(project, ortho) -> None:
    """Record the run's orthomosaic on ``project.params``.

    params is used rather than a new Job column because it is the one place
    every dispatch path already shares: the local thread, the Airflow DAG's
    callback into ``run_analyze``, and a direct ``POST /analyze``. The worker
    already reads it through ``build_config``, ``archive_current_run`` copies it
    into the run history as it stands, and it needs no schema change.

    Two keys are stored. ``ortho_id`` is the lasting identity, used by the
    ``in_use`` check, and ``ortho_stem`` is what the worker finds files with and
    what stays readable in the run history.
    """
    params = dict(project.params or {})
    params["ortho_id"] = ortho.id
    params["ortho_stem"] = ortho.stem
    project.params = params


def _apply_run_config(db: Session, project, body, pre_state: str) -> None:
    """Apply the settings a run was triggered with: its name, model and params.

    This is called once the gate has moved the project into ANALYZING, so no
    other run can be interleaved with it. If the previous state means the
    current run has already been used, that run is archived first and
    current_run is incremented, so this run computes into a new folder.

    It also records the run's orthomosaic. That happens after
    ``archive_current_run``, so the archived entry keeps the previous run's
    orthomosaic and the new run gets the one just chosen.
    """
    if pre_state in USED_RUN_STATES:
        archive_current_run(db, project, archived_state=pre_state)

    if body:
        if body.params:
            new_params = dict(project.params or {})
            new_params.update(body.params)
            if "model_name" in new_params:
                new_params["model_name"] = resolve_backbone(new_params.get("model_name"))
            project.params = new_params
        if body.model_key is not None:
            project.model_key = body.model_key
        if body.source_epsg is not None:
            project.source_epsg = body.source_epsg
        if body.run_name is not None:
            project.run_name = body.run_name.strip() or None

    # Recorded last, so the checked ``ortho_id`` field wins over a raw
    # ``params.ortho_id`` in a hand-written body. params is merged rather than
    # replaced, so an old value would otherwise survive. It is resolved here
    # rather than passed in, because archive_current_run above may have changed
    # current_run, which the file check depends on.
    _pin_run_ortho(project, resolve_run_ortho(project, body))

    db.add(project)
    db.commit()
    db.refresh(project)
    ensure_project_dirs(project.id, project.current_run)


@router.post("/projects/{project_id}/runs/analyze")
@router.post("/project/runs/analyze")
def trigger_analyze(
    body: AnalyzeTrigger | None = None,
    project=Depends(get_project),
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
):
    """Start, or restart, the analysis.

    The optional JSON body names the run, picks the orthomosaic with
    ``ortho_id``, and sets the detector, feature extractor and pipeline
    parameters in the same call, so the frontend's Analyze tab is one button.

    ``ortho_id`` can be left out when the project holds exactly one
    orthomosaic, which is the case for every project created before the library
    existed, so older clients keep working.
    """
    if body and body.project_id:
        project = resolve_project(db, user, body.project_id)

    # Resolve the orthomosaic before taking the state, so a bad or missing
    # choice returns a 400 or 404 and leaves the project as it was, rather than
    # stuck in ANALYZING. _apply_run_config resolves it again and saves the same
    # choice once the state is held.
    resolve_run_ortho(project, body)
    _validate_trigger_body(project, body)
    pre_state = project.state
    _gate(db, project, _ANALYZE_FROM, "ANALYZING", "analyze")
    _apply_run_config(db, project, body, pre_state)
    job = _new_job(db, project, "analyze")
    try:
        run_id = dispatch_analyze(project.id, job.id, project.current_run)
    except RuntimeError as exc:
        _fail_trigger(db, project, job, exc)
        code, hint = _classify_dispatch_error(exc)
        log.error("502 %s project=%s action=analyze job=%s: %s",
                  code, project.id, job.id, exc, exc_info=True)
        raise HTTPException(502, {
            "code": code,
            "message": f"Failed to trigger analyze: {exc}",
            "project_id": project.id,
            "hint": hint,
        }) from exc
    _mark_dispatched(db, job, run_id)
    return {
        "project_id": project.id,
        "state": project.state, "job_id": job.id, "run_id": run_id,
        "run": project.current_run, "run_name": project.run_name,
        "mode": "airflow" if airflow_enabled() else "local",
    }


@router.post("/projects/{project_id}/runs/finalize")
@router.post("/project/runs/finalize")
def trigger_finalize(
    request: Request,
    body: FinalizeTrigger | None = None,
    project=Depends(get_project),
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
):
    path_project_id = request.path_params.get("project_id")
    if not path_project_id and not (body and body.project_id):
        log.warning("400 MISSING_PARAM field=project_id action=finalize")
        raise HTTPException(400, {
            "code": ERROR_CODES["MISSING_PARAM"],
            "message": "Required field 'project_id' is missing",
            "project_id": None,
            "hint": "provide: project_id",
        })
    if body and body.project_id:
        project = resolve_project(db, user, body.project_id)

    n_labels = db.query(models.ClusterLabel).filter_by(project_id=project.id).count()
    if n_labels == 0:
        log.warning("400 NO_LABELS project=%s action=finalize", project.id)
        raise HTTPException(400, {
            "code": ERROR_CODES["NO_LABELS"],
            "message": f"No labels submitted for project {project.id}",
            "project_id": project.id,
            "hint": "submit the cluster table",
        })
    _gate(db, project, _FINALIZE_FROM, "FINALIZING", "finalize")
    job = _new_job(db, project, "finalize")
    try:
        run_id = dispatch_finalize(project.id, job.id, project.current_run)
    except RuntimeError as exc:
        _fail_trigger(db, project, job, exc)
        code, hint = _classify_dispatch_error(exc)
        log.error("502 %s project=%s action=finalize job=%s: %s",
                  code, project.id, job.id, exc, exc_info=True)
        raise HTTPException(502, {
            "code": code,
            "message": f"Failed to trigger finalize: {exc}",
            "project_id": project.id,
            "hint": hint,
        }) from exc
    _mark_dispatched(db, job, run_id)
    return {
        "project_id": project.id,
        "state": project.state, "job_id": job.id, "run_id": run_id,
        "mode": "airflow" if airflow_enabled() else "local",
    }


@router.get("/projects/{project_id}/runs/status")
@router.get("/project/runs/status")
def run_status(project=Depends(get_project), db: Session = Depends(get_db)):
    """The latest job's progress for the active run: its stage and percentage.

    This is what drives the frontend's progress bar, and it is cheap enough to
    poll every few seconds.
    """
    from datetime import datetime as _dt
    jobs = db.query(models.Job).filter_by(project_id=project.id).all()
    job = max(jobs, key=lambda j: (j.started_at or _dt.min), default=None) if jobs else None
    return {
        "project_id": project.id,
        "state": project.state,
        "current_run": project.current_run,
        "run_name": project.run_name,
        "job": None if not job else {
            "id": job.id,
            "type": job.type,
            "state": job.state,
            "current_stage": job.current_stage,
            "progress": job.progress,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            "dag_run_id": job.celery_task_id,
            "error": job.error,
        },
    }
