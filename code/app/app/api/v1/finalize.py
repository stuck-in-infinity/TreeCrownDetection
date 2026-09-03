"""Phase B — Species assignment + validation + export (synchronous compute callback).

Compute-service endpoint the orchestrator calls. Blocks until the KMZ is built
and returns the final results payload. Supports the same ``Idempotency-Key``
replay/in-flight semantics as ``/analyze`` (v4 §9.4).
"""
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.deps import get_project, require_api_key, require_service_token, resolve_project
from app.api.v1.results import build_results_payload
from app.api.v1.runs import _current_request_id
from app.core.logging import get_logger
from app.db import models
from app.db.session import get_db
from app.schemas.project import AnalyzeTrigger, FinalizeTrigger
from app.services import job_claim
from app.services.state import transition_if
from app.workers.tasks import job_b_finalize

router = APIRouter()
log = get_logger("app.api")

# Includes FINALIZING so the trigger can hand off to this compute callback.
_FINALIZE_OK = {"LABELS_SUBMITTED", "FINALIZING", "COMPLETED", "FAILED"}


@router.post("/projects/{project_id}/finalize")
@router.post("/project/finalize")
def start_finalize(
    request: Request,
    body: FinalizeTrigger | None = None,
    project=Depends(get_project),
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    return run_finalize(request, body, project, db, user, idempotency_key)


def run_finalize(
    request: Request,
    body: AnalyzeTrigger | FinalizeTrigger | None,
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

    key = job_claim.compute_key(idempotency_key)
    prior = job_claim.find_prior(db, project, key)
    if prior and prior.state == "SUCCEEDED":
        db.refresh(project)
        return build_results_payload(project)

    if project.state not in _FINALIZE_OK:
        raise HTTPException(409, {
            "code": "INVALID_STATE",
            "message": f"Cannot finalize from state {project.state}",
            "hint": "finalize runs after labels have been submitted \u2014 upload the labels CSV first",
            "project_id": project.id,
        })

    n_labels = db.query(models.ClusterLabel).filter_by(project_id=project.id).count()
    if n_labels == 0:
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "Submit labels before finalizing",
            "hint": "download the cluster samples, name each cluster\u2019s species in the labels CSV, and upload it in step 4",
            "project_id": project.id,
        })

    # Claim before computing: this endpoint runs the pipeline inline and wipes
    # step2/3/4 on entry, so a second concurrent caller would delete this run's
    # outputs mid-export. The state check above cannot exclude it (FINALIZING is
    # a valid source state, for hand-off from the trigger).
    previous_state = project.state
    job, outcome = job_claim.claim(
        db, project, key, "finalize", request_id=_current_request_id()
    )
    if outcome == job_claim.REPLAY:
        db.refresh(project)
        return build_results_payload(project)
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

    project.state = "FINALIZING"
    project.error = None
    db.add(project)
    db.commit()

    try:
        job_b_finalize.apply(args=[project.id, job.id]).get(propagate=True)
    except Exception as exc:
        db.refresh(project)
        db.refresh(job)
        stage = job.current_stage or "unknown"
        log.error("finalize failed project=%s job=%s stage=%s", project.id, job.id, stage, exc_info=True)
        # Read the failure text before rolling back — transition_if clears it.
        message = project.error or str(exc)
        # Conditional roll-back: only undo the state this call claimed.
        transition_if(db, project, {"FINALIZING"}, previous_state)
        raise HTTPException(500, {
            "code": "COMPUTE_FAILED",
            "message": message,
            "project_id": project.id,
            "stage": stage,
            "hint": "see the run's logs/ folder or errors.jsonl by request_id",
        }) from exc

    db.refresh(project)
    return build_results_payload(project)
