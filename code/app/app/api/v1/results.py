"""Final results summary + downloads (current run) + per-run history/comparison."""
import csv
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.deps import get_project
from app.core.logging import naive_now
from app.core.storage import project_paths
from app.db import models
from app.db.session import get_db
from app.services.assets import analyze_asset_fields

router = APIRouter()


class ConsentBody(BaseModel):
    # 0 = no / not given, 1 = yes all data, 2 = yes unlabelled (Step-1) only.
    consent: int


def _run(project) -> int:
    return getattr(project, "current_run", 1) or 1


def _require_completed(project) -> None:
    """425 NOT_READY until the run has produced results (v4 section 8.3)."""
    if project.state != "COMPLETED":
        raise HTTPException(425, {"code": "NOT_READY",
            "message": f"Results not ready (state {project.state})",
            "project_id": project.id,
            "hint": "results appear only after Finalize completes — upload the orthomosaic, "
                    "run analysis, submit the labels CSV, then run Finalize"})


def build_results_payload(project) -> dict:
    """Build the final summary + download links for the current run.

    Extracted so the synchronous POST /finalize can return it directly.
    """
    p = project_paths(project.id, _run(project))
    master = os.path.join(p["step2_output"], "crown_master.csv")
    polyspecies = os.path.join(p["step2_output"], "polygon_species.csv")
    kmz = os.path.join(p["step4_output"], "species_map.kmz")
    cm = os.path.join(p["step3_output"], "confusion_matrix.png")
    stac = os.path.join(p["step4_output"], "stac_item.json")

    distribution: dict[str, int] = {}
    if os.path.exists(master):
        with open(master) as f:
            for row in csv.DictReader(f):
                sp = row.get("species", "unlabelled") or "unlabelled"
                distribution[sp] = distribution.get(sp, 0) + 1

    base = "/api/v1/project/results"
    files_url = None
    try:
        from app.services.filebrowser_client import filebrowser_enabled, share_url
        hash_ = getattr(project, "share_hash", None)
        if filebrowser_enabled() and hash_:
            files_url = share_url(hash_)
    except Exception:
        pass

    payload = {
        "project_id": project.id,
        "state": project.state,
        "run": _run(project),
        "species_distribution": distribution,
        "validation": _read_validation(p),
        "files_url": files_url,
        "consent": getattr(project, "consent", 0),
        "consent_at": (
            project.consent_at.isoformat()
            if getattr(project, "consent_at", None)
            else None
        ),
        "downloads": {
            "kmz": f"{base}/kmz" if os.path.exists(kmz) else None,
            "crown_master_csv": f"{base}/crown-master.csv" if os.path.exists(master) else None,
            "polygon_species_csv": f"{base}/polygon-species.csv" if os.path.exists(polyspecies) else None,
            "confusion_matrix_png": f"{base}/confusion-matrix.png" if os.path.exists(cm) else None,
            "stac_item_json": f"{base}/stac-item.json" if os.path.exists(stac) else None,
        },
    }
    payload.update(analyze_asset_fields(project))
    return payload


@router.get("/projects/{project_id}/results")
@router.get("/project/results")
def results(project=Depends(get_project)):
    _require_completed(project)
    return build_results_payload(project)


@router.post("/projects/{project_id}/consent")
@router.post("/project/consent")
def submit_consent(
    body: ConsentBody,
    project=Depends(get_project),
    db: Session = Depends(get_db),
):
    """Capture the user's data-sharing consent after finalize.

    0 = no / not given, 1 = yes to all data, 2 = yes to unlabelled (Step-1) only.
    """
    if body.consent not in (0, 1, 2):
        raise HTTPException(400, {"code": "BAD_REQUEST",
            "message": "consent must be one of 0 (no), 1 (all), 2 (unlabelled only)",
            "project_id": project.id,
            "hint": "pick one of the three consent choices and resubmit — 0 to decline, "
                    "1 to share all data, 2 to share unlabelled data only"})
    _require_completed(project)
    project.consent = body.consent
    project.consent_at = naive_now()
    db.add(project)
    db.commit()
    return {"project_id": project.id, "consent": project.consent,
            "consent_at": project.consent_at.isoformat()}


@router.get("/projects/{project_id}/results/kmz")
@router.get("/project/results/kmz")
def download_kmz(project=Depends(get_project)):
    f = os.path.join(project_paths(project.id, _run(project))["step4_output"], "species_map.kmz")
    _require_completed(project)
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "KMZ not found",
            "project_id": project.id,
            "hint": "this run finished without writing a KMZ — re-run the analysis, "
                    "resubmit the labels CSV and run Finalize again"})
    return FileResponse(
        f, media_type="application/vnd.google-earth.kmz", filename="species_map.kmz"
    )


@router.get("/projects/{project_id}/results/crown-master.csv")
@router.get("/project/results/crown-master.csv")
def download_master(project=Depends(get_project)):
    f = os.path.join(project_paths(project.id, _run(project))["step2_output"], "crown_master.csv")
    _require_completed(project)
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "crown_master.csv not found",
            "project_id": project.id,
            "hint": "this run finished without writing crown_master.csv — re-run the analysis "
                    "and finalize again to rebuild it"})
    return FileResponse(f, media_type="text/csv", filename="crown_master.csv")


@router.get("/projects/{project_id}/results/polygon-species.csv")
@router.get("/project/results/polygon-species.csv")
def download_polyspecies(project=Depends(get_project)):
    f = os.path.join(project_paths(project.id, _run(project))["step2_output"], "polygon_species.csv")
    _require_completed(project)
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "polygon_species.csv not found",
            "project_id": project.id,
            "hint": "this run finished without writing polygon_species.csv — check that the "
                    "labels CSV named every cluster, then finalize again"})
    return FileResponse(f, media_type="text/csv", filename="polygon_species.csv")


@router.get("/projects/{project_id}/results/confusion-matrix.png")
@router.get("/project/results/confusion-matrix.png")
def download_cm(project=Depends(get_project)):
    f = os.path.join(project_paths(project.id, _run(project))["step3_output"], "confusion_matrix.png")
    _require_completed(project)
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "confusion_matrix.png not found",
            "project_id": project.id,
            "hint": "this file is optional — it is only produced when ground truth was "
                    "uploaded before finalizing; the rest of the results are unaffected"})
    return FileResponse(f, media_type="image/png")


@router.get("/projects/{project_id}/results/stac-item.json")
@router.get("/project/results/stac-item.json")
def download_stac_item(project=Depends(get_project)):
    f = os.path.join(project_paths(project.id, _run(project))["step4_output"], "stac_item.json")
    _require_completed(project)
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "stac_item.json not found",
            "project_id": project.id,
            "hint": "this run finished without writing stac_item.json — re-run the analysis "
                    "and finalize again to rebuild it"})
    return FileResponse(f, media_type="application/json", filename="stac_item.json")


# -- run history: list + per-run results for comparison (v5) ----------------
_ASSETS = {
    "kmz": ("step4_output", "species_map.kmz",
            "application/vnd.google-earth.kmz", "species_map.kmz"),
    "crown-master.csv": ("step2_output", "crown_master.csv", "text/csv", "crown_master.csv"),
    "polygon-species.csv": ("step2_output", "polygon_species.csv", "text/csv", "polygon_species.csv"),
    "confusion-matrix.png": ("step3_output", "confusion_matrix.png", "image/png", None),
    "stac-item.json": ("step4_output", "stac_item.json", "application/json", "stac_item.json"),
}


def _asset_path(project_id: str, run: int, asset: str):
    spec = _ASSETS.get(asset)
    if spec is None:
        raise HTTPException(404, {"code": "NOT_FOUND", "message": f"Unknown asset '{asset}'",
            "hint": "ask for one of: " + ", ".join(_ASSETS)})
    dir_key, fname, media, download_name = spec
    p = project_paths(project_id, run)
    return os.path.join(p[dir_key], fname), media, download_name


def _run_results_payload(project, run: int) -> dict:
    """Results summary for a specific (possibly archived) run, with run-scoped
    download URLs. 404s if that run never produced final outputs."""
    p = project_paths(project.id, run)
    master = os.path.join(p["step2_output"], "crown_master.csv")
    kmz = os.path.join(p["step4_output"], "species_map.kmz")
    if not (os.path.exists(master) or os.path.exists(kmz)):
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"Run {run} has no final results (it may not have been finalized)",
            "project_id": project.id,
            "hint": "only finalized runs keep results — open the run history and pick a run "
                    "listed as having results"})

    distribution: dict[str, int] = {}
    if os.path.exists(master):
        with open(master) as f:
            for row in csv.DictReader(f):
                sp = row.get("species", "unlabelled") or "unlabelled"
                distribution[sp] = distribution.get(sp, 0) + 1

    base = f"/api/v1/project/runs/{run}/results"
    downloads = {}
    for asset in _ASSETS:
        path, _, _ = _asset_path(project.id, run, asset)
        key = asset.replace("-", "_").replace(".", "_")
        downloads[key] = f"{base}/{asset}" if os.path.exists(path) else None

    payload = {
        "project_id": project.id,
        "run": run,
        "species_distribution": distribution,
        "validation": _read_validation(p),
        "downloads": downloads,
    }
    payload.update(analyze_asset_fields(project, run))
    return payload


def _run_meta_from_rows(db, project) -> list[dict] | None:
    """Build the history from the ``runs`` table, which is the record.

    The JSON path below predates that table and only ever knew about runs the
    archiver had written; a run row created by any other path was invisible to
    it. Rows win wherever they exist, and the JSON stays as the fallback for a
    project that has not been backfilled yet.
    """
    rows = (
        db.query(models.Run)
        .filter_by(project_id=project.id)
        .order_by(models.Run.number)
        .all()
    )
    if not rows:
        return None
    orthos = {o.id: o for o in (project.orthos or [])}
    out = []
    for r in rows:
        o = orthos.get(r.ortho_id)
        out.append({
            "run": r.number,
            "run_name": r.name,
            "params": dict(r.params or {}),
            "model_key": r.model_key,
            "state": r.state,
            "recommended_k": r.recommended_k,
            "available_k": r.available_k,
            "ortho": o.filename if o else None,
            "ortho_id": r.ortho_id,
            "ortho_stem": o.stem if o else None,
        })
    return out


def _run_meta(project) -> list[dict]:
    """All runs (archived + current), oldest first, with results availability."""
    entries = []
    for h in (project.runs or []):
        entries.append(dict(h))
    params = dict(project.params or {})
    # The CURRENT run's ortho: the pin written by _apply_run_config, falling back
    # to the single ortho a pre-library project can only have used. Matches the
    # shape archive_current_run writes, so the current entry and the archived
    # ones stay interchangeable to the frontend.
    cur = next(
        (o for o in (project.orthos or []) if o.id == params.get("ortho_id")), None
    )
    if cur is None:
        cur = next(
            (o for o in (project.orthos or []) if o.stem == params.get("ortho_stem")),
            None,
        )
    if cur is None and len(project.orthos or []) == 1:
        cur = project.orthos[0]
    entries.append({
        "run": project.current_run or 1,
        "run_name": getattr(project, "run_name", None),
        "params": params,
        "model_key": project.model_key,
        "state": project.state,
        "recommended_k": project.recommended_k,
        "available_k": project.available_k,
        "ortho": cur.filename if cur else None,
        "ortho_id": cur.id if cur else None,
        "ortho_stem": cur.stem if cur else None,
    })
    return entries


def _decorate(db, project, entries: list[dict]) -> list[dict]:
    """Add per-run facts the run picker needs: the row, the link, and whether
    the user is allowed to act on it.

    ``can_label`` and ``can_finalize`` are decided HERE and not re-derived in
    the browser. The rule is not "is the state in this list" — finalize also
    needs the run to have labels — and a second copy of that rule in JavaScript
    would be a second thing to keep right. The frontend enables a button when
    the server says so.
    """
    from app.services import run_registry
    from app.services.filebrowser_client import filebrowser_enabled, run_share_url

    rows = {r.number: r for r in
            db.query(models.Run).filter_by(project_id=project.id).all()}
    fb_on = False
    try:
        fb_on = filebrowser_enabled() and bool(getattr(project, "share_hash", None))
    except Exception:                    # noqa: BLE001 - a link is never worth a 500
        fb_on = False
    orthos = {o.id: o for o in (project.orthos or [])}

    for e in entries:
        run = e.get("run") or 1
        p = project_paths(project.id, run)
        row = rows.get(run)
        e["run_id"] = row.id if row else None
        e["is_current"] = run == (project.current_run or 1)
        e["has_results"] = (
            os.path.exists(os.path.join(p["step2_output"], "crown_master.csv"))
            or os.path.exists(os.path.join(p["step4_output"], "species_map.kmz"))
        )
        e["results_url"] = (
            f"/api/v1/project/runs/{run}/results" if e["has_results"] else None
        )
        e["files_url"] = run_share_url(project.share_hash, run) if fb_on else None

        # The run row is authoritative for state and attribution; the JSON
        # history is a compatibility view that predates it.
        if row is not None:
            e["state"] = row.state
            e["label_count"] = run_registry.labels_for(db, row)
            e["can_label"] = run_registry.can_label(row)
            e["can_finalize"] = run_registry.can_finalize(db, row)
            e["created_at"] = row.created_at.isoformat() if row.created_at else None
            e["finished_at"] = row.finished_at.isoformat() if row.finished_at else None
            if row.ortho_id:
                e["ortho_id"] = row.ortho_id
                o = orthos.get(row.ortho_id)
                if o is not None:
                    e["ortho"] = o.filename
                    e["ortho_stem"] = o.stem
        else:
            e.setdefault("label_count", 0)
            e["can_label"] = False
            e["can_finalize"] = False
    return entries


@router.get("/projects/{project_id}/runs")
@router.get("/project/runs")
def list_runs(project=Depends(get_project), ortho_id: str | None = None,
              db: Session = Depends(get_db)):
    """Run history for this project.

    ``ortho_id`` narrows it to the runs done on one orthomosaic — what the
    library rows in step 2 ask for when they are expanded. A run whose input
    was never recorded (it predates the ortho library) is deliberately returned
    by NEITHER filter rather than by all of them: putting somebody's run under
    the wrong survey is worse than admitting the gap.
    """
    entries = _decorate(db, project,
                        _run_meta_from_rows(db, project) or _run_meta(project))
    if ortho_id:
        entries = [e for e in entries if e.get("ortho_id") == ortho_id]
    return {
        "project_id": project.id,
        "current_run": project.current_run or 1,
        "ortho_id": ortho_id,
        "runs": entries,
    }


@router.get("/projects/{project_id}/runs/{run}/results")
@router.get("/project/runs/{run}/results")
def run_results(run: int, project=Depends(get_project)):
    if run < 1 or run > (project.current_run or 1):
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"Run {run} does not exist (runs 1..{project.current_run or 1})",
            "project_id": project.id,
            "hint": "check the run history for this project and use a run number it lists"})
    return _run_results_payload(project, run)


@router.get("/projects/{project_id}/runs/{run}/results/{asset}")
@router.get("/project/runs/{run}/results/{asset}")
def run_asset(run: int, asset: str, project=Depends(get_project)):
    if run < 1 or run > (project.current_run or 1):
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"Run {run} does not exist", "project_id": project.id,
            "hint": "check the run history for this project and use a run number it lists"})
    path, media, download_name = _asset_path(project.id, run, asset)
    if not os.path.exists(path):
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"{asset} not found for run {run}", "project_id": project.id,
            "hint": "that run did not produce this file — confusion-matrix.png is optional "
                    "and needs ground truth; for the rest, use a finalized run"})
    kwargs = {"media_type": media}
    if download_name:
        kwargs["filename"] = download_name
    return FileResponse(path, **kwargs)


def _read_validation(p: dict):
    """Derive simple metrics from step3's validation_detail.csv (acc + counts)."""
    detail = os.path.join(p["step3_output"], "validation_detail.csv")
    if not os.path.exists(detail):
        return None
    try:
        with open(detail) as f:
            rows = list(csv.DictReader(f))
        if not rows:
            return None
        total = len(rows)
        correct = sum(
            1 for r in rows if r.get("true_species") == r.get("pred_species")
        )
        return {
            "matched_samples": total,
            "accuracy": round(correct / total, 4) if total else None,
        }
    except Exception:
        return None
