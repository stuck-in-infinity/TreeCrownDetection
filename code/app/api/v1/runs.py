"""Asynchronous run triggers (frontend-facing).

These are the endpoints the frontend calls. They return immediately:
atomically move the project into the in-progress state, dispatch the work
(Airflow DAG run, or a local background thread), and return the run id. The
frontend then polls GET /projects/{id} until AWAITING_LABELS / COMPLETED /
FAILED.

Only one run of a project computes at a time. The in-progress states are
deliberately not valid launch states, and the transition into them is atomic, so
a second trigger arriving while a run is active is turned away with
409 CONFLICT_BUSY instead of racing the first one into reset_dirs, which deletes
the run folder.
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_project, require_api_key, resolve_project, service_caller
from app.core.logging import ERROR_CODES, get_logger, naive_now, request_id_var
from app.core.models_registry import resolve_backbone, resolve_model_path
from app.core.storage import ensure_project_dirs
from app.db import models
from app.db.session import get_db
from app.schemas.project import AnalyzeTrigger, FinalizeTrigger
from app.services.airflow_client import airflow_enabled
from app.services import run_registry
from app.services.project_service import USED_RUN_STATES, archive_current_run
from app.services.run_dispatch import dispatch_analyze, dispatch_finalize
from app.services.state import transition_if

router = APIRouter()

log = get_logger("app.api")


def _current_request_id():
    """Read the request_id ContextVar (minted in the audit middleware); the
    "-" default means no request scope, so return None for the nullable column."""
    try:
        rid = request_id_var.get()
    except LookupError:
        return None
    return rid if rid and rid != "-" else None


def _classify_dispatch_error(exc: Exception) -> tuple[str, str]:
    """Map a dispatch RuntimeError (which carries the classified reason from
    airflow_client) to a §10 code + remediation hint for the human 502 body."""
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


# A NEW run may be launched only from these states (in-progress excluded on
# purpose). Launching from a used-run state archives that run and opens a fresh
# work/run_<n+1> folder (one-call re-analyze).
_ANALYZE_FROM = {"UPLOADED", "AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED", "FAILED"}
_FINALIZE_FROM = {"LABELS_SUBMITTED", "COMPLETED", "FAILED"}
_BUSY = {"ANALYZING", "FINALIZING"}

#: Every project state that is not a run in flight. Used when the action's real
#: precondition has already been checked against the RUN, and all the project
#: gate still has to decide is "is something else computing right now".
_NOT_BUSY = {"CREATED", "UPLOADED", "AWAITING_LABELS", "LABELS_SUBMITTED",
             "COMPLETED", "FAILED"}


def _gate(db: Session, project, allowed: set[str], in_progress_state: str, action: str):
    """Atomically enter the in-progress state, or raise the right 409."""
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
    # Conditional: only roll back the in-progress state this trigger claimed.
    # A blind assignment could stamp FAILED over a state something else has
    # legitimately moved on to.
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
    """Fail fast with 400 before any state transition happens."""
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
    """Decide which orthomosaic this run uses, or raise the right 4xx.

    Three cases, in the order the spec states them:

    * ``ortho_id`` given -> it must belong to THIS project and its file must be
      on disk. A foreign or unknown id is 404 ORTHO_NOT_FOUND, not 403: the
      caller already owns the project, so the id is simply not one of its own.
    * omitted, exactly one ortho -> that one. This is what keeps every existing
      project, every existing frontend build and every script working unchanged.
    * omitted, several orthos -> 400 ORTHO_SELECTION_REQUIRED, whose ``details``
      carries the candidate list so a client can render the choice without a
      second call.

    Pure: it reads, validates and returns; persisting the decision is
    ``_apply_run_config``'s job, so this can also be used as a pre-flight check
    before any state transition happens.
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
        # Orchestrator callback (Airflow re-entering /project/drone_api or
        # /analyze with execution_id set). The DAG does not necessarily forward
        # every conf key, so fall back to the pin the trigger already wrote onto
        # project.params. Deliberately NOT done for a plain user request: there,
        # silently reusing the previous run's ortho is exactly the ambiguity the
        # 400 below exists to prevent.
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
        # A pin whose ortho has since been deleted: fall through to the normal
        # rules rather than 404-ing a callback the user cannot influence.

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
    """The selected ortho must still have its file. A row whose file vanished
    (manual cleanup, a half-finished upload) would otherwise fail deep in the
    worker with a FileNotFoundError and a FAILED project."""
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
    """Record the run's ortho on ``project.params``.

    Chosen over a new ``Job`` column on purpose — see CHANGES.md. In short:
    params is the one carrier every dispatch path already shares (the local
    thread, the Airflow DAG's callback into ``run_analyze``, and a direct
    ``POST /analyze``), it is what ``build_config`` already reads in the worker,
    it is copied verbatim into ``project.runs`` history by
    ``archive_current_run``, and it needs no schema change.

    Both keys are stored: ``ortho_id`` is the durable identity used for
    ``in_use`` checks, ``ortho_stem`` is what the worker resolves files with and
    what stays readable in the run history.
    """
    params = dict(project.params or {})
    params["ortho_id"] = ortho.id
    params["ortho_stem"] = ortho.stem
    project.params = params


def _apply_run_config(db: Session, project, body, pre_state: str) -> None:
    """Apply analyze-time configuration (run name + model + param overrides).

    Called after the atomic gate has moved the project into ANALYZING, so no
    concurrent run can interleave. If the previous state had already used the
    current run, that run is archived first and current_run is bumped so this
    run computes into a fresh folder (run versioning, v5).

    Also pins the run's orthomosaic. This runs AFTER ``archive_current_run``, so
    the archived entry keeps the PREVIOUS run's ortho and the new run gets the
    newly-selected one."""
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

    # Which run this one was configured from, when the user opened an older run
    # and pressed Run analysis. Set last and cleared when absent, for the same
    # reason the ortho is pinned last: params are MERGED, so a value left over
    # from the previous run would otherwise be inherited and every later run
    # would keep claiming it came from run 3.
    _params = dict(project.params or {})
    if body is not None and body.based_on_run is not None:
        _params["based_on_run"] = int(body.based_on_run)
    else:
        _params.pop("based_on_run", None)
    project.params = _params

    # Pinned LAST, so the validated ``ortho_id`` field always wins over a raw
    # ``params.ortho_id`` in a hand-written body (params is merged, not
    # replaced, so a stale pin would otherwise survive). Resolved here rather
    # than passed in because archive_current_run above may have bumped
    # current_run, which the file-existence check depends on.
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
    service: bool = Depends(service_caller),
):
    """Fire (or re-fire) the analysis. Optional JSON body names the run, picks
    the orthomosaic (``ortho_id``) and sets the detector / feature extractor /
    pipeline params in the same call, so the frontend's Analyze tab is a single
    button.

    ``ortho_id`` may be omitted when the project holds exactly one orthomosaic —
    which is every project that existed before the library — so no client
    change is required to keep working."""
    if body and body.project_id:
        project = resolve_project(db, user, body.project_id, service=service)

    # Pre-flight: resolve the ortho BEFORE the state gate, so a bad or missing
    # selection is a clean 400/404 that leaves the project exactly as it was,
    # rather than a project stuck in ANALYZING. _apply_run_config re-resolves
    # and persists the same choice once the gate is held.
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


@router.post("/projects/{project_id}/runs/{run}/finalize")
@router.post("/project/runs/{run}/finalize")
@router.post("/projects/{project_id}/runs/finalize")
@router.post("/project/runs/finalize")
def trigger_finalize(
    request: Request,
    run: int | None = None,
    body: FinalizeTrigger | None = None,
    project=Depends(get_project),
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
    service: bool = Depends(service_caller),
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
        project = resolve_project(db, user, body.project_id, service=service)

    # Which run is being exported. Omitted means the active one — what every
    # existing caller means. Named, it may be an earlier run that was labelled
    # and then left.
    target_run = run if run is not None else (project.current_run or 1)
    run_row = run_registry.ensure_run(db, project, target_run)
    db.commit()

    n_labels = db.query(models.ClusterLabel).filter_by(
        project_id=project.id, run_id=run_row.id
    ).count()
    if n_labels == 0:
        log.warning("400 NO_LABELS project=%s run=%s action=finalize",
                    project.id, target_run)
        raise HTTPException(400, {
            "code": ERROR_CODES["NO_LABELS"],
            "message": (
                f"Run {target_run} has no labels, so there is nothing to export."
            ),
            "project_id": project.id,
            "hint": "assign a species to each cluster of that run and submit them first",
            "details": {"run": target_run, "state": run_row.state},
        })
    if run_row.state not in _FINALIZE_FROM:
        log.warning("409 INVALID_STATE project=%s run=%s state=%s allowed=%s",
                    project.id, target_run, run_row.state, sorted(_FINALIZE_FROM))
        raise HTTPException(409, {
            "code": ERROR_CODES["INVALID_STATE"],
            "message": (
                f"Run {target_run} cannot be exported from state {run_row.state}."
            ),
            "project_id": project.id,
            "hint": "submit that run's labels first",
            "details": {"run": target_run, "state": run_row.state,
                        "accepted_states": sorted(_FINALIZE_FROM)},
        })

    # The project-level gate is the lock: only one run of a project computes at
    # a time, whichever run that is. It stays exactly as it was, which is what
    # keeps a finalize of run 2 from starting while run 4 is still analysing.
    #
    # Finalizing an OLDER run needs two repairs around it, because the gate is
    # written in terms of the project:
    #
    #  * the gate reads `project.state`, which describes the ACTIVE run. If run
    #    4 is sitting in AWAITING_LABELS, that is not a finalize-from state,
    #    and run 2 would be refused for something run 4 is doing. So when the
    #    target is not the active run, the gate is asked only for the busy
    #    check — the per-run check above has already decided the real question.
    #  * `transition_if` mirrors the new state onto the ACTIVE run's row, which
    #    would mark run 4 as FINALIZING. The active row is put back afterwards.
    active_number = project.current_run or 1
    active_row = run_registry.get_run(db, project, active_number)
    active_state_before = active_row.state if active_row else None

    if target_run == active_number:
        _gate(db, project, _FINALIZE_FROM, "FINALIZING", "finalize")
    else:
        _gate(db, project, _NOT_BUSY, "FINALIZING", "finalize")
        if active_row is not None and active_state_before:
            active_row.state = active_state_before
            db.add(active_row)
            db.commit()
    run_registry.set_run_state(db, project, target_run, "FINALIZING")

    job = _new_job(db, project, "finalize")
    try:
        run_id = dispatch_finalize(project.id, job.id, target_run)
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
    """Latest job's live progress for the active run - drives the frontend
    progress UI (stage + percentage). Cheap to poll every few seconds."""
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
