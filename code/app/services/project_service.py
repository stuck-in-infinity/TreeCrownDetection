import json
from app.schemas.project import OrthoOut, ProjectOut

# States in which the current run has already produced results, or tried to.
# Changing the configuration from one of these archives the run and opens a new
# work/run_<n+1> folder.
USED_RUN_STATES = {"AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED", "FAILED"}


def archive_current_run(db, project, archived_state: str | None = None) -> None:
    """Add a summary of the current run to project.runs and increment
    current_run, so the next analyze computes into a new folder.

    Also clears the review fields and the cluster labels, which belong to the
    clustering of the run being archived. Does not commit; the caller owns the
    transaction.
    """
    from app.db import models

    params = dict(project.params or {})
    # Record which orthomosaic this run used. ``_apply_run_config`` in runs.py
    # stores ``ortho_id`` and ``ortho_stem`` when the run starts, and they are
    # what makes the library's ``in_use`` check exact. ``ortho`` still holds a
    # display filename, so anything already reading the history keeps working.
    # A project created before those keys existed has only one orthomosaic, so
    # falling back to it gives the same answer.
    pinned_id = params.get("ortho_id")
    pinned_stem = params.get("ortho_stem")
    used = next((o for o in (project.orthos or []) if o.id == pinned_id), None)
    if used is None and project.orthos:
        used = next((o for o in project.orthos if o.stem == pinned_stem), None)
    if used is None and len(project.orthos or []) == 1:
        used = project.orthos[0]

    history = list(project.runs or [])
    history.append(
        {
            "run": project.current_run or 1,
            "run_name": getattr(project, "run_name", None),
            "params": params,
            "model_key": project.model_key,
            "state": archived_state or project.state,
            "recommended_k": project.recommended_k,
            "available_k": project.available_k,
            "ortho": used.filename if used else None,
            "ortho_id": pinned_id or (used.id if used else None),
            "ortho_stem": pinned_stem or (used.stem if used else None),
        }
    )
    project.runs = history
    project.current_run = (project.current_run or 1) + 1
    project.run_name = None
    project.recommended_k = None
    project.available_k = None
    db.query(models.ClusterLabel).filter_by(project_id=project.id).delete()


def _last_error(project) -> dict | None:
    """Return the failure details when the project is FAILED, else None.

    ``Project.error`` normally holds the JSON that ``app.core.failures.classify``
    wrote: a specific code, a plain message, a hint, and the raw exception under
    ``details``. Older rows hold plain text instead, so both shapes are read here
    and returned in the same form. The stage comes from the most recent job when
    the stored record does not name one.
    """
    if project.state != "FAILED":
        return None

    stage = None
    try:
        jobs = [j for j in (project.jobs or []) if j.started_at]
        if jobs:
            stage = max(jobs, key=lambda j: j.started_at).current_stage
    except Exception:
        stage = None

    raw = project.error
    if isinstance(raw, str) and raw.lstrip().startswith("{"):
        try:
            rec = json.loads(raw)
        except (ValueError, TypeError):
            rec = None
        if isinstance(rec, dict) and rec.get("code"):
            rec.setdefault("stage", None)
            if not rec.get("stage"):
                rec["stage"] = stage
            return rec

    # An older row, or a failure recorded before classification existed. Return
    # the same shape so callers reading last_error do not have to special-case it.
    return {"code": "COMPUTE_FAILED", "stage": stage, "message": raw,
            "hint": None, "details": None}


def _project_files_url(project) -> str | None:
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


def serialize_project(project) -> ProjectOut:
    """Convert a Project row into the API response model."""
    return ProjectOut(
        project_id=project.id,
        name=project.name,
        model_key=project.model_key,
        state=project.state,
        source_epsg=project.source_epsg,
        params=project.params or {},
        recommended_k=project.recommended_k,
        available_k=project.available_k,
        current_run=project.current_run or 1,
        run_name=getattr(project, "run_name", None),
        runs=project.runs or [],
        orthos=[OrthoOut.model_validate(o) for o in project.orthos],
        error=project.error,
        last_error=_last_error(project),
        files_url=_project_files_url(project),
        created_at=project.created_at,
        updated_at=project.updated_at,
    )
