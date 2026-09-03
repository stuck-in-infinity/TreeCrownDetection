"""Phase A: crown detection and clustering.

This is the compute endpoint the Airflow DAG calls. It runs the pipeline in the
request, so the call does not return until the run finishes or fails, and then
returns the review payload. A Job row records per-stage progress for the logs.

Callers should send an ``Idempotency-Key`` header, which for Airflow is its
dag_run_id. A repeat call with a key whose run already succeeded returns that
result again without recomputing, and a key whose run is still going gets 409
CONFLICT_BUSY.
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_project, require_api_key, require_service_token, resolve_project
from app.api.v1.clustering import build_clustering_payload
from app.api.v1.runs import (
    _apply_run_config,
    _classify_dispatch_error,
    _current_request_id,
    _pin_run_ortho,
    _validate_trigger_body,
    resolve_run_ortho,
)
from app.core.logging import ERROR_CODES, get_logger
from app.db.session import get_db
from app.schemas.project import AnalyzeTrigger
from app.services import job_claim
from app.services.airflow_client import airflow_enabled, get_dag_run_state, trigger_drone_dag
from app.services.assets import analyze_asset_fields
from app.services.state import transition_if
from app.workers.tasks import job_a_analyze

router = APIRouter()

log = get_logger("app.api")

# ANALYZING is in the list because the trigger endpoint has already moved the
# project into it before handing the work here.
_ANALYZE_OK = {"UPLOADED", "ANALYZING", "AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED", "FAILED"}


def _files_url(project) -> str | None:
    hash_ = getattr(project, "share_hash", None)
    if not hash_:
        return None
    try:
        from app.services.filebrowser_client import filebrowser_enabled, share_url
        if filebrowser_enabled():
            return share_url(hash_)
    except Exception:
        pass
    return None


def _analyze_payload(request: Request, project) -> dict:
    payload = build_clustering_payload(request, project)
    payload.update(analyze_asset_fields(project))
    payload["files_url"] = _files_url(project)
    return payload


@router.post("/project/drone_api")
def drone_api(
    request: Request,
    body: AnalyzeTrigger | None = None,
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    if not body or not body.project_id or not body.action:
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "project_id and action are required in the request body",
            "hint": "send both fields \u2014 this endpoint is the orchestrator callback, not the user-facing trigger",
            "project_id": getattr(body, "project_id", None),
        })
    if body.action not in ("analyze", "finalize"):
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "action must be 'analyze' or 'finalize'",
            "hint": "use action=analyze to detect and cluster, action=finalize to export",
            "project_id": body.project_id,
        })

    # Hand the work to Airflow when it is configured. An execution_id in the
    # body means Airflow is already calling us back to run the pipeline, so in
    # that case we run it here instead of triggering another DAG.
    if airflow_enabled() and not body.execution_id:
        conf = {
            "project_id": body.project_id,
            "action": body.action,
            "execution_type": "fullexec",
        }
        if body.params:
            conf["params"] = body.params
        if body.model_key:
            conf["model_key"] = body.model_key
        if body.source_epsg:
            conf["source_epsg"] = body.source_epsg
        if body.run_name:
            conf["run_name"] = body.run_name

        # Choose the orthomosaic, for the analyze action only. It is checked
        # here so a bad ortho_id fails this call with a 400 rather than starting
        # a DAG that then fails, and it is saved onto project.params before the
        # DAG starts, because the DAG's callback into run_analyze may not pass
        # every conf key through while a value on the project is always read.
        # It also goes into conf, so a DAG that ignores the key still works.
        if body.action == "analyze":
            project = resolve_project(db, user, body.project_id)
            selected = resolve_run_ortho(project, body)
            conf["ortho_id"] = selected.id
            _pin_run_ortho(project, selected)
            db.add(project)
            db.commit()

        try:
            dag_run_id = trigger_drone_dag(conf)
        except RuntimeError as exc:
            raise HTTPException(502, {
                "code": "AIRFLOW_TRIGGER_FAILED", "message": str(exc),
                "hint": ("the orchestrator refused the request or could not be "
                         "reached \u2014 ask an administrator to check Airflow is up"),
            })

        # Return now; the frontend polls /drone_status/{dag_run_id} from here.
        return {
            "status": "started",
            "dag_run_id": dag_run_id,
            "project_id": body.project_id,
            "action": body.action,
        }

    # With no Airflow configured, run the pipeline here instead.
    project = resolve_project(db, user, body.project_id)
    if body.action == "finalize":
        from app.api.v1.finalize import run_finalize
        return run_finalize(request, body, project, db, user, idempotency_key)
    return run_analyze(request, body, project, db, user, idempotency_key)


@router.get("/project/drone_status/{dag_run_id}")
def drone_status(
    request: Request,
    dag_run_id: str,
    action: str,
    project_id: str,
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
    _svc: str = Depends(require_service_token),
):
    try:
        state = get_dag_run_state(dag_run_id)
    except RuntimeError as exc:
        raise HTTPException(502, {
            "code": "AIRFLOW_POLL_FAILED", "message": str(exc),
            "hint": ("the run may still be going \u2014 reload in a moment; if it "
                     "persists, ask an administrator to check Airflow"),
        })

    if state == "success":
        db.expire_all()
        project = resolve_project(db, user, project_id)
        if action == "analyze":
            payload = _analyze_payload(request, project)
        else:
            from app.api.v1.results import build_results_payload
            payload = build_results_payload(project)
        payload["state"] = "success"
        return payload

    if state == "failed":
        raise HTTPException(500, {
            "code": "COMPUTE_FAILED",
            "message": "Airflow DAG failed",
            "hint": "the run failed inside the orchestrator \u2014 open the run log, or the DAG run in Airflow, for the stage that failed",
            "project_id": project_id,
        })

    return {"state": state, "dag_run_id": dag_run_id}


@router.post("/projects/{project_id}/analyze")
@router.post("/project/analyze")
def start_analyze(
    request: Request,
    body: AnalyzeTrigger | None = None,
    project=Depends(get_project),
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return run_analyze(request, body, project, db, user, idempotency_key)


def run_analyze(
    request: Request,
    body: AnalyzeTrigger | None,
    project,
    db: Session,
    user: str,
    idempotency_key: str | None = None,
):
    path_project_id = request.path_params.get("project_id")
    if not path_project_id and not (body and body.project_id):
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "project_id is required in the request body",
            "hint": "send project_id \u2014 this endpoint is the orchestrator callback, not the user-facing trigger",
            "project_id": None,
        })
    if body and body.project_id:
        project = resolve_project(db, user, body.project_id)

    _validate_trigger_body(project, body)
    # If this key's run already succeeded, return that result rather than
    # computing it again.
    key = job_claim.compute_key(idempotency_key)
    prior = job_claim.find_prior(db, project, key)
    if prior and prior.state == "SUCCEEDED":
        db.refresh(project)
        return _analyze_payload(request, project)

    if project.state not in _ANALYZE_OK:
        raise HTTPException(409, {
            "code": "INVALID_STATE",
            "message": f"Cannot analyze from state {project.state}",
            "hint": "analysis can start from UPLOADED, or after a finished run \u2014 wait for the current run to finish",
            "project_id": project.id,
        })
    # Work out which orthomosaic this run uses before claiming the job, for the
    # same reason the trigger does: a bad selection must not leave a claimed job
    # behind. _apply_run_config below resolves it again and saves it.
    resolve_run_ortho(project, body)

    # Claim the run before changing any state or touching the run folder. This
    # endpoint computes inline, and _apply_run_config can archive the run and
    # increment current_run, so two callers arriving together would corrupt each
    # other's run. The state check above cannot stop them, because ANALYZING is
    # a state analyze may start from when the trigger hands off. See
    # services/job_claim.py.
    previous_state = project.state
    job, outcome = job_claim.claim(
        db, project, key, "analyze", request_id=_current_request_id()
    )
    if outcome == job_claim.REPLAY:
        db.refresh(project)
        return _analyze_payload(request, project)
    if job is None:
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": (
                "A run with this Idempotency-Key is already in progress"
                if outcome == job_claim.DUPLICATE
                else "Another job is already in progress for this project"
            ),
            "project_id": project.id,
            "hint": "wait for the current run to finish",
        })

    project.state = "ANALYZING"
    project.error = None
    db.add(project)
    db.commit()
    db.refresh(project)
    _apply_run_config(db, project, body, previous_state)

    try:
        job_a_analyze.apply(args=[project.id, job.id]).get(propagate=True)
    except Exception as exc:
        # The task's _fail() has already stored the FAILED state and the error.
        db.refresh(project)
        db.refresh(job)
        stage = job.current_stage or "unknown"
        log.error("analyze failed project=%s job=%s stage=%s", project.id, job.id, stage, exc_info=True)
        # Read the failure text first: transition_if clears project.error.
        message = project.error or str(exc)
        # Only roll back if this call is what set ANALYZING.
        transition_if(db, project, {"ANALYZING"}, previous_state)
        raise HTTPException(500, {
            "code": "COMPUTE_FAILED",
            "message": message,
            "project_id": project.id,
            "stage": stage,
            "hint": "see the run's logs/ folder or errors.jsonl by request_id",
        }) from exc

    db.refresh(project)
    return _analyze_payload(request, project)
