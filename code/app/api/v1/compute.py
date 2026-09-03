"""Compute callbacks for the STACD Airflow framework, one per algorithm node.

The framework decides what to do from the HTTP status:
  200  the asset was produced   -> task succeeds, dataset registered
       body: {"asset_id": "<path>", "version": "<n>", "hosting_platform": "..."}
  400  bad input parameters     -> task is skipped, no dataset
  404  no data for these params -> task is skipped, no dataset
  500  the pipeline failed      -> task fails, and so does the DAG run
       error body for 400, 404 and 500: {"error": "<CODE>", "message": "<text>"}

These bodies are returned as-is with JSONResponse, skipping the app's usual
nested ``{"error":{...}}`` envelope, because the framework expects the flat
shape above.

Callers should send a stable ``Idempotency-Key`` header, which for Airflow is
the dag_run_id. A repeat whose key already succeeded gets the same 200 back
without recomputing. The key is stored with a ``compute:`` prefix, so it cannot
collide with the placeholder job the trigger creates, which stores the raw
dag_run_id.

Two callbacks must never compute the same project at once, because they share
one ``work/run_<n>`` directory and each wipes it on entry. The claim is taken on
the Job row in ``_claim`` rather than on the project state, because the trigger
has already moved the project into ANALYZING or FINALIZING before the DAG runs,
and a conditional update onto the state it is already in excludes nobody. A
callback that loses the claim gets a 400, which the DAG treats as a skip. This
module never returns 409.
"""
import os

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import require_service_token
from app.core.logging import get_logger, request_id_var
from app.core.storage import project_paths
from app.db import models
from app.db.session import get_db
from app.schemas.compute import ComputeRequest
from app.services import job_claim
from app.services.assets import asset_response_fields
from app.workers.tasks import job_a_analyze, job_b_finalize

router = APIRouter()

log = get_logger("app.api")


def _current_request_id():
    """The correlation id the audit middleware set for this request.

    It is only stored on the Job row and is never sent on to Airflow.
    """
    try:
        rid = request_id_var.get()
    except LookupError:
        return None
    return rid if rid and rid != "-" else None


# Names the machine that produced the asset. Returned on a 200, and can be
# overridden through the environment.
HOSTING_PLATFORM = os.getenv("TCP_HOSTING_PLATFORM", "act4dws4")

# The in-progress states are in these lists because the trigger has already
# moved the project into them before handing the work here.
_ANALYZE_OK = {"UPLOADED", "ANALYZING", "AWAITING_LABELS", "FAILED"}
_FINALIZE_OK = {"LABELS_SUBMITTED", "FINALIZING", "COMPLETED", "FAILED"}


def _ok(project, asset_id: str, version, stage: str | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content=asset_response_fields(project, asset_id, version, stage=stage),
    )


def _err(
    http_code: int, error: str, message: str, project_id: str | None = None
) -> JSONResponse:
    content = {"error": error, "message": message}
    if project_id:
        content["project_id"] = project_id
    return JSONResponse(status_code=http_code, content=content)


def _claim(db: Session, project, key: str, job_type: str):
    """Claim the run for this callback. Returns ``(job, rejection_response)``.

    One of the two is set, except in the replay case, where a callback with the
    same key finished while this one was inserting. That returns
    ``(None, None)`` and leaves the caller to return the success payload.

    See services/job_claim.py for why the claim is on the Job row rather than
    the project state. A caller that loses gets a 400, which the DAGs turn into
    an AirflowSkipException. Skipping is the right outcome, because the caller
    that won is producing the asset.
    """
    job, outcome = job_claim.claim(
        db, project, key, job_type, request_id=_current_request_id()
    )
    if outcome == job_claim.WON:
        return job, None
    if outcome == job_claim.REPLAY:
        return None, None
    if outcome == job_claim.ACTIVE:
        return None, _err(
            400, "INVALID_STATE",
            "Another job is already in progress for this project", project.id,
        )
    return None, _err(
        400, "INVALID_STATE",
        "A run with this Idempotency-Key is already in progress", project.id,
    )


def _get_compute_project(db: Session, req: ComputeRequest):
    return db.get(models.Project, req.project_id)


def _analyze_asset_id(project) -> str:
    """The analyze output reported as the asset_id: the crown-polygon GeoJSON,
    or the Step 1 clustering output directory if there is no GeoJSON."""
    p = project_paths(project.id, project.current_run or 1)
    poly = p["polygons"]
    try:
        gj = sorted(f for f in os.listdir(poly) if f.lower().endswith(".geojson"))
        if gj:
            return os.path.join(poly, gj[0])
    except OSError:
        pass
    return p["step1_output"]


def _finalize_asset_id(project) -> str:
    """The finalize output reported as the asset_id: the species map KMZ."""
    p = project_paths(project.id, project.current_run or 1)
    return os.path.join(p["step4_output"], "species_map.kmz")


@router.post("/compute/analyze")
def compute_analyze(
    req: ComputeRequest,
    db: Session = Depends(get_db),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """DAG A: Detectree2 detection, then DINOv2 features, KMeans and t-SNE."""
    if not req.project_id:
        return _err(400, "BAD_REQUEST", "project_id is required in the request body")
    project = _get_compute_project(db, req)
    if not project:
        return _err(404, "NOT_FOUND", f"Project {req.project_id} not found", req.project_id)

    key = job_claim.compute_key(idempotency_key or req.execution_id)
    prior = job_claim.find_prior(db, project, key)
    if prior and prior.state == "SUCCEEDED":
        return _ok(project, _analyze_asset_id(project), project.current_run or 1, stage="analyze")

    if project.state not in _ANALYZE_OK:
        return _err(400, "INVALID_STATE", f"Cannot analyze from state {project.state}", project.id)
    if not project.orthos:
        return _err(400, "NO_INPUT", "No orthomosaic uploaded for this project", project.id)

    job, rejection = _claim(db, project, key, "analyze")
    if job is None:
        if rejection is not None:
            return rejection
        # A callback with the same key finished while we were inserting, so
        # return its result.
        return _ok(project, _analyze_asset_id(project), project.current_run or 1, stage="analyze")
    # Just recording the state. _claim is what settled who computes.
    project.state = "ANALYZING"; project.error = None
    db.add(project); db.commit()

    try:
        job_a_analyze.apply(args=[project.id, job.id]).get(propagate=True)
    except Exception as exc:
        db.refresh(project)
        log.error("compute analyze failed project=%s job=%s stage=%s",
                  project.id, job.id, job.current_stage, exc_info=True)
        return _err(500, "COMPUTE_FAILED", project.error or str(exc), project.id)

    db.refresh(project)
    return _ok(project, _analyze_asset_id(project), project.current_run or 1, stage="analyze")


@router.post("/compute/finalize")
def compute_finalize(
    req: ComputeRequest,
    db: Session = Depends(get_db),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """DAG B: assign species, validate, reproject and write the KMZ."""
    if not req.project_id:
        return _err(400, "BAD_REQUEST", "project_id is required in the request body")
    project = _get_compute_project(db, req)
    if not project:
        return _err(404, "NOT_FOUND", f"Project {req.project_id} not found", req.project_id)

    key = job_claim.compute_key(idempotency_key or req.execution_id)
    prior = job_claim.find_prior(db, project, key)
    if prior and prior.state == "SUCCEEDED":
        return _ok(project, _finalize_asset_id(project), project.current_run or 1, stage="finalize")

    if project.state not in _FINALIZE_OK:
        return _err(400, "INVALID_STATE", f"Cannot finalize from state {project.state}", project.id)
    n_labels = db.query(models.ClusterLabel).filter_by(project_id=project.id).count()
    if n_labels == 0:
        return _err(400, "NO_LABELS", "No labels submitted for this project", project.id)

    job, rejection = _claim(db, project, key, "finalize")
    if job is None:
        if rejection is not None:
            return rejection
        return _ok(project, _finalize_asset_id(project), project.current_run or 1, stage="finalize")
    # Just recording the state. _claim is what settled who computes.
    project.state = "FINALIZING"; project.error = None
    db.add(project); db.commit()

    try:
        job_b_finalize.apply(args=[project.id, job.id]).get(propagate=True)
    except Exception as exc:
        db.refresh(project)
        log.error("compute finalize failed project=%s job=%s stage=%s",
                  project.id, job.id, job.current_stage, exc_info=True)
        return _err(500, "COMPUTE_FAILED", project.error or str(exc), project.id)

    db.refresh(project)
    return _ok(project, _finalize_asset_id(project), project.current_run or 1, stage="finalize")
