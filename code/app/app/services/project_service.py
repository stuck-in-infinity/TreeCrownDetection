import json
from app.schemas.project import OrthoOut, ProjectOut

# States whose run has already produced (or attempted) results - re-configuring
# from here archives the run and opens a fresh work/run_<n+1> folder.
USED_RUN_STATES = {"AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED", "FAILED"}


def archive_current_run(db, project, archived_state: str | None = None) -> None:
    """Append the current run's summary to project.runs and bump current_run so
    the next analyze computes into a fresh folder. Clears the per-run review
    fields off the project and stamps the outgoing run's row. Cluster labels are
    KEPT — they belong to the archived run and are what lets it be finished
    later. Does NOT commit - the caller owns the transaction."""
    from app.db import models

    params = dict(project.params or {})
    # Which ortho this archived run actually used. ``ortho_id`` / ``ortho_stem``
    # are pinned by _apply_run_config (runs.py); they are what makes the
    # ``in_use`` check on the library exact. ``ortho`` keeps its original
    # meaning — a display filename — so nothing that already reads the history
    # changes shape. On a project that predates the pin there is exactly one
    # ortho, so the old expression is still the right answer.
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
    # Stamp the outgoing run's row BEFORE current_run moves, or the mirror
    # would write this run's outcome onto the next run's row.
    from app.services.run_registry import mirror
    outgoing = mirror(db, project, number=project.current_run or 1, commit=False)
    if outgoing is not None:
        outgoing.state = archived_state or project.state
        if outgoing.ortho_id is None:
            outgoing.ortho_id = pinned_id or (used.id if used else None)
        db.add(outgoing)

    project.runs = history
    project.current_run = (project.current_run or 1) + 1
    project.run_name = None
    project.recommended_k = None
    project.available_k = None

    # The cluster labels are NOT deleted any more. This used to be
    #
    #     db.query(models.ClusterLabel).filter_by(project_id=project.id).delete()
    #
    # which threw away the user's species judgement — the most expensive thing
    # they produce — every time a new run opened. It is the single reason an
    # earlier run could never be picked up and finished later. Labels now carry
    # ``run_id`` and stay with the run they describe.


def _last_error(project) -> dict | None:
    """Structured failure info when the project is FAILED (v4 section 8.1).

    ``Project.error`` now holds the JSON written by ``app.core.failures.classify``
    — a specific code, a plain-language message, a hint, and the raw exception
    under ``details``. Rows written before that change hold bare text, so both
    shapes are read here and both come out the same way. The stage is filled in
    from the most recent job when the stored record did not carry one.
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

    # Legacy row (or a failure recorded before classification): keep the old
    # shape exactly, so nothing that reads last_error breaks.
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
    """ORM Project -> API response model."""
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
