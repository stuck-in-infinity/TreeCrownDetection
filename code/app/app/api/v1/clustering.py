"""The human-in-the-loop review: the two DAGs (t-SNE + k-selection), per-cluster
crown thumbnails, individual crown images, and the detection overlay."""
import csv
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import get_project
from app.core.storage import project_paths, relative_artifact_path
from app.services.assets import analyze_asset_fields

router = APIRouter()

_REVIEW_STATES = {"AWAITING_LABELS", "LABELS_SUBMITTED", "FINALIZING", "COMPLETED"}


def _run(project) -> int:
    return getattr(project, "current_run", 1) or 1


def _clustering_dir(project) -> str:
    return os.path.join(project_paths(project.id, _run(project))["step1_output"], "clustering")


def _require_review(project) -> None:
    """425 NOT_READY until clustering artifacts exist for this run (v4 §8.3)."""
    if project.state not in _REVIEW_STATES:
        raise HTTPException(425, {"code": "NOT_READY",
            "message": f"Clustering not ready (state {project.state})",
            "project_id": project.id,
            "hint": "clusters appear once analysis finishes and the project reaches "
                    "AWAITING_LABELS — upload an orthomosaic and run the analysis first"})


def build_clustering_payload(request: Request, project) -> dict:
    """Build the review payload (the two-DAG visuals + metrics).

    Extracted so the synchronous POST /analyze can return it directly without
    a second round-trip through GET /clustering.
    """
    cdir = _clustering_dir(project)
    base = str(request.base_url).rstrip("/")
    avail = project.available_k or []
    # Detection overlay: emit the storage-relative FILE path (not an API URL),
    # e.g. projects/<id>/work/run_<n>/detectree/S3C/overlay.png. None if absent.
    _det = project_paths(project.id, _run(project))["detectree"]
    _subs = (
        sorted(d for d in os.listdir(_det) if os.path.isdir(os.path.join(_det, d)))
        if os.path.isdir(_det)
        else []
    )
    _overlay_f = os.path.join(_det, _subs[0], "overlay.png") if _subs else ""
    overlay_rel = (
        relative_artifact_path(_overlay_f)
        if (_overlay_f and os.path.exists(_overlay_f))
        else None
    )
    per_k = [
        {
            "k": k,
            "tsne_plot_url": f"{base}/api/v1/project/clustering/{k}/tsne.png",
            "clusters_url": f"{base}/api/v1/project/clustering/{k}/clusters",
        }
        for k in avail
    ]
    payload = {
        "project_id": project.id,
        "state": project.state,
        "available_k": avail,
        "recommended_k": project.recommended_k,
        "k_recommendation_table": _read_table(
            os.path.join(cdir, "k_recommendation_table.csv")
        ),
        "k_selection_plot_url": f"{base}/api/v1/project/clustering/k-selection.png",
        "per_k": per_k,
        "detection_overlay_url": overlay_rel,
    }
    payload.update(analyze_asset_fields(project))
    return payload


@router.get("/projects/{project_id}/clustering")
@router.get("/project/clustering")
def clustering_overview(request: Request, project=Depends(get_project)):
    """Review payload: recommended k, metric table, and URLs to the two DAGs."""
    if project.state not in _REVIEW_STATES:
        raise HTTPException(409, {"code": "INVALID_STATE",
            "message": f"Clustering not ready (state {project.state})",
            "project_id": project.id,
            "hint": "wait until the analysis finishes and the project reaches AWAITING_LABELS, "
                    "then reopen this page"})
    return build_clustering_payload(request, project)


@router.get("/projects/{project_id}/clustering/k-selection.png")
@router.get("/project/clustering/k-selection.png")
def k_selection_png(project=Depends(get_project)):
    """DAG #2 — elbow / silhouette / Davies-Bouldin across all k."""
    _require_review(project)
    f = os.path.join(_clustering_dir(project), "k_selection.png")
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "k_selection.png not found",
            "project_id": project.id,
            "hint": "this run produced no k-selection plot — re-run the analysis to "
                    "regenerate the clustering visuals"})
    return FileResponse(f, media_type="image/png")


@router.get("/projects/{project_id}/clustering/{k}/tsne.png")
@router.get("/project/clustering/{k}/tsne.png")
def tsne_png(k: int, project=Depends(get_project)):
    """DAG #1 — t-SNE cluster scatter for a given k."""
    _require_review(project)
    f = os.path.join(_clustering_dir(project), f"tsne_k{k}.png")
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "t-SNE plot not found",
            "project_id": project.id,
            "hint": "pick a k from this run's available_k list — no plot was drawn for the "
                    "one you asked for"})
    return FileResponse(f, media_type="image/png")


@router.get("/projects/{project_id}/clustering/{k}/clusters")
@router.get("/project/clustering/{k}/clusters")
def clusters_overview(
    k: int, request: Request, project=Depends(get_project), samples: int = 8
):
    """Per-cluster sample crown thumbnails so the user can name each cluster."""
    _require_review(project)
    kdir = os.path.join(_clustering_dir(project), f"k{k}")
    if not os.path.isdir(kdir):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": f"clusters for k={k} not found",
            "project_id": project.id,
            "hint": "choose a k that this run listed in available_k, or re-run the analysis "
                    "with that number of clusters"})

    base = str(request.base_url).rstrip("/")
    out = []
    for ci in range(k):
        cf = os.path.join(kdir, f"cluster_{ci}")
        if not os.path.isdir(cf):
            continue
        tifs = sorted(f for f in os.listdir(cf) if f.lower().endswith(".tif"))
        out.append(
            {
                "cluster": ci,
                "count": len(tifs),
                "sample_crowns": [
                    f"{base}/api/v1/project/crowns/{name}"
                    for name in tifs[:samples]
                ],
            }
        )
    return {"project_id": project.id, "k": k, "clusters": out}


@router.get("/projects/{project_id}/crowns/{image_name}")
@router.get("/project/crowns/{image_name}")
def crown_png(image_name: str, project=Depends(get_project)):
    """Render a single crown GeoTIFF to PNG for the labeling UI."""
    safe = os.path.basename(image_name)
    src = os.path.join(project_paths(project.id, _run(project))["step1_output"], "crowns", safe)
    if not os.path.exists(src):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "crown not found",
            "project_id": project.id,
            "hint": "this crown belongs to an older run — reload the cluster samples for the "
                    "current run and use the links it gives you"})
    png = _tif_to_png_bytes(src)
    if png is None:
        raise HTTPException(503, {"code": "DEPENDENCY_MISSING",
            "message": "raster rendering unavailable (rasterio/PIL not installed)",
            "hint": "a server dependency is missing — ask an administrator; nothing you "
                    "change in this project will help"})
    return StreamingResponse(png, media_type="image/png")


@router.get("/projects/{project_id}/detection/overlay.png")
@router.get("/project/detection/overlay.png")
def overlay_png(project=Depends(get_project)):
    """Detection overlay for the project's orthomosaic."""
    det = project_paths(project.id, _run(project))["detectree"]
    subs = (
        sorted(d for d in os.listdir(det) if os.path.isdir(os.path.join(det, d)))
        if os.path.isdir(det)
        else []
    )
    f = os.path.join(det, subs[0], "overlay.png") if subs else ""
    if not f or not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND", "message": "overlay not found",
            "project_id": project.id,
            "hint": "the detection overlay is written during analysis — run the analysis for "
                    "this project, then reload"})
    return FileResponse(f, media_type="image/png")


# ── helpers ────────────────────────────────────────────────────────────
def _read_table(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except Exception:
        return []


def _tif_to_png_bytes(path: str):
    try:
        import io

        import numpy as np
        import rasterio
        from PIL import Image

        with rasterio.open(path) as src:
            n = min(3, src.count)
            arr = src.read(list(range(1, n + 1)))
        arr = np.transpose(arr, (1, 2, 0)).astype("float32")
        mn, mx = float(np.nanmin(arr)), float(np.nanmax(arr))
        if mx > mn:
            arr = (arr - mn) / (mx - mn) * 255.0
        arr = np.nan_to_num(arr).astype("uint8")
        if arr.shape[2] == 1:
            arr = arr[:, :, 0]
        buf = io.BytesIO()
        Image.fromarray(arr).save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        return None
