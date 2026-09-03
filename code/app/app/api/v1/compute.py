"""STACD/Airflow compute callbacks (body-style) — the endpoints the Airflow
framework calls per algorithm node.

Response contract (HTTP-status driven — the framework branches on the code):
  200  success, asset produced   -> Airflow task SUCCESS, dataset registered
       body: {"asset_id": "<path>", "version": "<n>", "hosting_platform": "..."}
  400  invalid input parameters  -> task SKIPPED (graceful), no dataset
  404  no data for these params  -> task SKIPPED (graceful), no dataset
  500  pipeline/computation fail -> task FAILED, DAG run fails
       error body (400/404/500): {"error": "<CODE>", "message": "<text>"}

These bodies are returned DIRECTLY (JSONResponse), bypassing the app's nested
``{"error":{...}}`` envelope, because the framework expects the flat shape above.

Idempotency: pass a stable ``Idempotency-Key`` header (the DAG's dag_run_id). A
repeat whose key already SUCCEEDED replays a 200 without recomputing. The key is
namespaced (``compute:<id>``) so it never collides with the trigger's placeholder
job (which stores the raw dag_run_id on its celery_task_id).

Concurrency: two callbacks must never compute the same project at once — they
share one ``work/run_<n>`` directory and each starts by wiping it. The claim is
made on the Job row (``_claim``), not on the project state, because the trigger
has already moved the project into ANALYZING/FINALIZING before the DAG fires and
a conditional UPDATE onto the state it is already in cannot exclude anyone. A
callback that loses the claim gets **400**, which the DAG turns into a graceful
skip; no 409 is emitted from this module.
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
    """Server-side correlation id from the audit middleware ContextVar; never
    crosses the Airflow boundary (stored side-channel on the Job row only)."""
    try:
        rid = request_id_var.get()
    except LookupError:
        return None
    return rid if rid and rid != "-" else None


# Identifies which workstation produced the asset (returned on 200; env-overridable).
HOSTING_PLATFORM = os.getenv("TCP_HOSTING_PLATFORM", "act4dws4")

# Includes the in-progress state so the trigger (which already moved the project
# into ANALYZING/FINALIZING) can hand off to this compute callback.
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

    Exactly one of the two is non-None, except for the replay case — a same-key
    callback that finished while we were inserting — which returns
    ``(None, None)`` and leaves the caller to return its success payload.

    See services/job_claim.py for why the exclusion lives on the Job row rather
    than on the project state. Losers get 400, which the DAGs map to
    AirflowSkipException: a graceful skip is the honest outcome, because the
    winner is producing the asset.
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
    """Path to the analyze output used as the STAC-D asset_id: the crown-polygon
    GeoJSON if present, else the Step-1 clustering output dir."""
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
    """Path to the finalize output used as the asset_id: the species map KMZ."""
    p = project_paths(project.id, project.current_run or 1)
    return os.path.join(p["step4_output"], "species_map.kmz")


@router.post("/compute/analyze")
def compute_analyze(
    req: ComputeRequest,
    db: Session = Depends(get_db),
    _svc: str = Depends(require_service_token),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Single-node DAG A callback: Detectree2 detection + DINOv2/KMeans/t-SNE."""
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
        # Same-key callback that finished while we were inserting — replay it.
        return _ok(project, _analyze_asset_id(project), project.current_run or 1, stage="analyze")
    # Recording the state, not guarding with it — _claim already won the race.
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
    """Single-node DAG B callback: assign species + validate + reproject + KMZ."""
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
    # Recording the state, not guarding with it — _claim already won the race.
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
