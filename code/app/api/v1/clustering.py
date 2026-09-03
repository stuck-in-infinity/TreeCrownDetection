"""Everything the user looks at before naming the clusters: the t-SNE and
k-selection plots, per-cluster crown thumbnails, single crown images, and the
detection overlay.

Every route here takes an optional ``run`` query parameter and defaults to the
project's active run. Without it, opening run 3 while run 5 is the newest one
showed run 5's plots and run 5's crowns under run 3's heading — the pictures
quietly disagreed with the run you thought you were looking at.
"""
import csv
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse

from app.api.deps import get_project
from app.core.logging import ERROR_CODES, get_logger
from app.core.storage import project_paths, relative_artifact_path
from app.db import models
from app.db.session import get_db
from app.services import run_registry
from app.services.assets import analyze_asset_fields

import crown_thumbs

router = APIRouter()
log = get_logger("app.clustering")

_REVIEW_STATES = {"AWAITING_LABELS", "LABELS_SUBMITTED", "FINALIZING", "COMPLETED"}


def _run(project) -> int:
    return getattr(project, "current_run", 1) or 1


def _resolve_run(db, project, run: int | None) -> int:
    """Turn the ``run`` query parameter into a run number we know exists.

    Left out, it means the active run. Given, it has to be a run this project
    has actually had: asking for run 9 of a five-run project is a mistake worth
    naming rather than quietly answering with the newest run's files.
    """
    if run is None:
        return _run(project)
    numbers = sorted(
        n for (n,) in db.query(models.Run.number).filter_by(project_id=project.id)
    )
    # A project whose rows predate the runs table has none until the start-up
    # backfill has run. Fall back to the range the run folders imply.
    if not numbers:
        numbers = list(range(1, _run(project) + 1))
    if run not in numbers:
        log.warning("404 RUN_NOT_FOUND project=%s run=%s known=%s",
                    project.id, run, numbers)
        raise HTTPException(404, {
            "code": ERROR_CODES["RUN_NOT_FOUND"],
            "message": f"This project has no run {run}.",
            "project_id": project.id,
            "hint": "open the run list and pick one of the runs shown there",
            "details": {"run": run, "available_runs": numbers},
        })
    return run


def _clustering_dir(project, run: int) -> str:
    return os.path.join(project_paths(project.id, run)["step1_output"], "clustering")


def _run_state(db, project, run: int) -> str:
    """The state of one run, falling back to the project's own.

    The fallback covers a run whose row has not been backfilled yet; the
    project's state is the best answer available in that case.
    """
    row = run_registry.get_run(db, project, run)
    return row.state if row is not None else project.state


def _require_review(db, project, run: int) -> None:
    """Return 425 NOT_READY until THIS run has produced its clustering output.

    Checking the run rather than the project is what lets an older, finished run
    be reviewed while the project is busy analysing a newer one.
    """
    state = _run_state(db, project, run)
    if state not in _REVIEW_STATES:
        log.warning("425 NOT_READY project=%s run=%s state=%s",
                    project.id, run, state)
        raise HTTPException(425, {"code": "NOT_READY",
            "message": f"Run {run} has no clusters to show yet (it is {state}).",
            "project_id": project.id,
            "hint": "clusters appear once that run finishes analysing and reaches "
                    "AWAITING_LABELS — run the analysis first, or pick a run that "
                    "already has results",
            "details": {"run": run, "state": state}})


def build_clustering_payload(request: Request, project, run: int | None = None,
                             run_row=None) -> dict:
    """Build the review payload: the two plots and the cluster metrics.

    It lives in its own function so POST /analyze can return it directly and the
    frontend does not have to call GET /clustering straight afterwards.

    ``run_row`` is the Run being reviewed, when there is one. Its recommended and
    available k are the ones that run produced; the project's copy only ever
    describes the active run, so an older run read from the project would be
    labelled with the wrong k values.
    """
    n = run if run is not None else _run(project)
    cdir = _clustering_dir(project, n)
    base = str(request.base_url).rstrip("/")
    src = run_row if run_row is not None else project
    avail = getattr(src, "available_k", None) or []
    # Every URL carries the run it belongs to, so following any link from this
    # payload keeps you on the same run.
    q = f"?run={n}"
    # For the detection overlay, return the file path relative to the storage
    # root rather than an API URL, for example
    # projects/<id>/work/run_<n>/detectree/S3C/overlay.png, or None if missing.
    _det = project_paths(project.id, n)["detectree"]
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
            "tsne_plot_url": f"{base}/api/v1/project/clustering/{k}/tsne.png{q}",
            "clusters_url": f"{base}/api/v1/project/clustering/{k}/clusters{q}",
        }
        for k in avail
    ]
    payload = {
        "project_id": project.id,
        "run": n,
        "run_id": getattr(run_row, "id", None),
        "ortho_id": getattr(run_row, "ortho_id", None),
        "state": getattr(src, "state", project.state),
        "available_k": avail,
        "recommended_k": getattr(src, "recommended_k", None),
        "k_recommendation_table": _read_table(
            os.path.join(cdir, "k_recommendation_table.csv")
        ),
        "k_selection_plot_url":
            f"{base}/api/v1/project/clustering/k-selection.png{q}",
        "overlay_url": f"{base}/api/v1/project/detection/overlay.png{q}",
        # A link straight into THIS run's output folder, so the review panel can
        # offer it without knowing how shares are built. None when FileBrowser
        # is off, which the frontend renders as no link rather than a dead one.
        "files_url": _run_files_url(project, n),
        "per_k": per_k,
        "detection_overlay_url": overlay_rel,
    }
    payload.update(analyze_asset_fields(project))
    return payload


@router.get("/projects/{project_id}/clustering")
@router.get("/project/clustering")
def clustering_overview(request: Request, run: int | None = None,
                        project=Depends(get_project), db=Depends(get_db)):
    """The review payload: recommended k, the metric table, and the plot URLs."""
    n = _resolve_run(db, project, run)
    state = _run_state(db, project, n)
    if state not in _REVIEW_STATES:
        log.warning("409 INVALID_STATE project=%s run=%s state=%s",
                    project.id, n, state)
        raise HTTPException(409, {"code": "INVALID_STATE",
            "message": f"Run {n} has no clusters to show yet (it is {state}).",
            "project_id": project.id,
            "hint": "wait until that run finishes analysing and reaches "
                    "AWAITING_LABELS, then reopen this page",
            "details": {"run": n, "state": state}})
    return build_clustering_payload(
        request, project, n, run_registry.get_run(db, project, n)
    )


@router.get("/projects/{project_id}/clustering/k-selection.png")
@router.get("/project/clustering/k-selection.png")
def k_selection_png(run: int | None = None, project=Depends(get_project),
                    db=Depends(get_db)):
    """The k-selection plot: elbow, silhouette and Davies-Bouldin for each k."""
    n = _resolve_run(db, project, run)
    _require_review(db, project, n)
    f = os.path.join(_clustering_dir(project, n), "k_selection.png")
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"Run {n} has no k-selection plot.",
            "project_id": project.id,
            "hint": "this run produced no k-selection plot — re-run the analysis to "
                    "regenerate the clustering visuals",
            "details": {"run": n}})
    return FileResponse(f, media_type="image/png")


@router.get("/projects/{project_id}/clustering/{k}/tsne.png")
@router.get("/project/clustering/{k}/tsne.png")
def tsne_png(k: int, run: int | None = None, project=Depends(get_project),
             db=Depends(get_db)):
    """The t-SNE scatter plot of the clusters for one value of k."""
    n = _resolve_run(db, project, run)
    _require_review(db, project, n)
    f = os.path.join(_clustering_dir(project, n), f"tsne_k{k}.png")
    if not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"Run {n} has no t-SNE plot for k={k}.",
            "project_id": project.id,
            "hint": "pick a k from this run's available_k list — no plot was drawn for the "
                    "one you asked for",
            "details": {"run": n, "k": k}})
    return FileResponse(f, media_type="image/png")


@router.get("/projects/{project_id}/clustering/{k}/clusters")
@router.get("/project/clustering/{k}/clusters")
def clusters_overview(
    k: int, request: Request, run: int | None = None,
    project=Depends(get_project), db=Depends(get_db), samples: int = 8
):
    """The crowns to show for each cluster, so the user can name each one.

    Membership comes from ``k{k}_assignments.csv`` rather than from the
    per-cluster folders, so this still answers correctly when the run was made
    with ``COPY_TO_CLUSTER_FOLDERS`` off and those folders do not exist.
    """
    n = _resolve_run(db, project, run)
    _require_review(db, project, n)
    cdir = _clustering_dir(project, n)
    rows, order = _cluster_members(cdir, k)
    if rows is None:
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"Run {n} has no clusters for k={k}.",
            "project_id": project.id,
            "hint": "choose a k that this run listed in available_k, or re-run the analysis "
                    "with that number of clusters",
            "details": {"run": n, "k": k}})

    base = str(request.base_url).rstrip("/")
    out = []
    for ci in range(k):
        members = rows.get(ci, [])
        out.append(
            {
                "cluster": ci,
                "count": len(members),
                "crowns": [
                    {
                        "name": name,
                        "dist": dist,
                        "thumb_url":
                            f"{base}/api/v1/project/crowns/{name}?run={n}&k={k}",
                    }
                    for name, dist in members[:samples]
                ],
                # The old shape, kept so nothing that reads sample_crowns breaks.
                "sample_crowns": [
                    f"{base}/api/v1/project/crowns/{name}?run={n}&k={k}"
                    for name, _ in members[:samples]
                ],
            }
        )
    return {"project_id": project.id, "run": n, "k": k,
            "order": order, "clusters": out}


@router.get("/projects/{project_id}/crowns/{image_name}")
@router.get("/project/crowns/{image_name}")
def crown_png(image_name: str, run: int | None = None, k: int | None = None,
              project=Depends(get_project), db=Depends(get_db)):
    """One crown as a PNG, for the labelling screen.

    With ``k``, this prefers the thumbnail the pipeline rendered during analysis
    and falls back to converting the GeoTIFF here, writing the result alongside
    so the next request is a plain file read. A crown never changes once its run
    has finished, so the answer is cacheable either way.
    """
    n = _resolve_run(db, project, run)
    safe = os.path.basename(image_name)
    headers = {"Cache-Control": "public, max-age=86400"}

    # The fast path: a thumbnail written while the run was analysing.
    if k is not None:
        thumb = os.path.join(_clustering_dir(project, n), f"k{k}", "thumbs",
                             os.path.splitext(safe)[0] + ".png")
        if os.path.exists(thumb):
            return FileResponse(thumb, media_type="image/png", headers=headers)

    src = os.path.join(project_paths(project.id, n)["step1_output"], "crowns", safe)
    if not os.path.exists(src):
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"Run {n} has no crown named {safe}.",
            "project_id": project.id,
            "hint": "this crown belongs to a different run — reload the cluster samples "
                    "for the run you are looking at and use the links it gives you",
            "details": {"run": n, "crown": safe}})

    # Missing thumbnail, or no k given. Render it now.
    if k is not None:
        # Write it where the pipeline would have, so this happens once per crown
        # rather than once per view. A read-only or full disk just means we keep
        # rendering; it is not worth failing the request over.
        if crown_thumbs.write_thumbnail(src, thumb):
            return FileResponse(thumb, media_type="image/png", headers=headers)
        log.warning("could not cache thumbnail project=%s run=%s path=%s",
                    project.id, n, thumb)

    png = crown_thumbs.tif_to_png_bytes(src)
    if png is None:
        # Either the imaging libraries are missing or this particular TIF will
        # not read. Log the path: it is the only way an administrator can tell
        # the two apart, and the user's message cannot say which it was.
        log.error("503 crown render failed project=%s run=%s path=%s",
                  project.id, n, src)
        raise HTTPException(503, {"code": "DEPENDENCY_MISSING",
            "message": "This crown image could not be rendered.",
            "project_id": project.id,
            "hint": "a server dependency is missing or the file is unreadable — ask an "
                    "administrator; nothing you change in this project will help",
            "details": {"run": n, "crown": safe}})
    return StreamingResponse(png, media_type="image/png", headers=headers)


@router.get("/projects/{project_id}/detection/overlay.png")
@router.get("/project/detection/overlay.png")
def overlay_png(run: int | None = None, project=Depends(get_project),
                db=Depends(get_db)):
    """The detected crowns drawn over the project's orthomosaic."""
    n = _resolve_run(db, project, run)
    det = project_paths(project.id, n)["detectree"]
    subs = (
        sorted(d for d in os.listdir(det) if os.path.isdir(os.path.join(det, d)))
        if os.path.isdir(det)
        else []
    )
    f = os.path.join(det, subs[0], "overlay.png") if subs else ""
    if not f or not os.path.exists(f):
        raise HTTPException(404, {"code": "NOT_FOUND",
            "message": f"Run {n} has no detection overlay.",
            "project_id": project.id,
            "hint": "the detection overlay is written during analysis — run the analysis for "
                    "this project, then reload",
            "details": {"run": n}})
    return FileResponse(f, media_type="image/png")


# Helpers.
def _run_files_url(project, run: int) -> str | None:
    """FileBrowser link to one run's folder, or None when it is switched off."""
    try:
        from app.services.filebrowser_client import filebrowser_enabled, run_share_url
        hash_ = getattr(project, "share_hash", None)
        if hash_ and filebrowser_enabled():
            return run_share_url(hash_, run)
    except Exception:
        log.warning("could not build a FileBrowser link project=%s run=%s",
                    project.id, run, exc_info=True)
    return None


def _cluster_members(cdir: str, k: int):
    """Which crowns are in each cluster, most typical first.

    Returns ``({cluster_id: [(crown_name, distance), ...]}, order)`` or
    ``(None, None)`` when this k was never computed. ``order`` says how the
    crowns were sorted, so the caller can be honest about it: "centroid" means
    nearest the cluster centre first, which is what the user wants to see;
    "filename" is the fallback for runs made before the distance was recorded,
    and is really detection order, which says nothing about the cluster.
    """
    csv_path = os.path.join(cdir, f"k{k}_assignments.csv")
    if os.path.exists(csv_path):
        try:
            with open(csv_path) as f:
                rows = list(csv.DictReader(f))
        except OSError:
            rows = []
        if rows:
            has_dist = "dist_to_centroid" in rows[0]
            out: dict[int, list] = {}
            for r in rows:
                try:
                    ci = int(r["cluster"])
                except (KeyError, TypeError, ValueError):
                    continue
                dist = None
                if has_dist:
                    try:
                        dist = float(r["dist_to_centroid"])
                    except (TypeError, ValueError):
                        dist = None
                out.setdefault(ci, []).append((r.get("image_name", ""), dist))
            for ci in out:
                if has_dist:
                    # None sorts last, so a row with an unreadable distance does
                    # not jump to the front of the list.
                    out[ci].sort(key=lambda p: (p[1] is None, p[1]))
                else:
                    out[ci].sort(key=lambda p: p[0])
            return out, ("centroid" if has_dist else "filename")

    # No CSV: fall back to the per-cluster folders, if this run wrote them.
    kdir = os.path.join(cdir, f"k{k}")
    if not os.path.isdir(kdir):
        return None, None
    out = {}
    for ci in range(k):
        cf = os.path.join(kdir, f"cluster_{ci}")
        if not os.path.isdir(cf):
            continue
        out[ci] = [(f, None) for f in
                   sorted(x for x in os.listdir(cf) if x.lower().endswith(".tif"))]
    return (out, "filename") if out else (None, None)



def _read_table(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return list(csv.DictReader(f))
    except Exception:
        return []
