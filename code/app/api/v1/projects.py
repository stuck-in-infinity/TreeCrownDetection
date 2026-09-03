import multiprocessing
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.deps import get_project, require_api_key
from app.core.logging import ERROR_CODES
from app.core.models_registry import (
    DEFAULT_MODEL_KEY,
    list_backbones,
    list_models,
    resolve_backbone,
    resolve_model_path,
)
from app.core.settings import settings
from app.core.storage import delete_project_dir, ensure_project_dirs, project_paths
from app.db import models
from app.db.session import get_db
from app.schemas.project import OrthoFromUrl, ProjectCreate, ProjectUpdate
from app.services import drive_download
from app.services.project_service import (
    USED_RUN_STATES,
    archive_current_run,
    serialize_project,
)
from app.services.state import transition_if

router = APIRouter()

# Short-lived states used only as locks. Neither is a state analyze or finalize
# can start from, so holding one keeps a run out for the length of the request,
# and both are released, or the row deleted, before the request returns. See
# db/models.py for the states a project really moves through.
_UPLOADING = "UPLOADING"
_DELETING = "DELETING"
_BUSY_STATES = ("ANALYZING", "FINALIZING", _UPLOADING, _DELETING)
_DELETABLE_STATES = {
    "CREATED", "UPLOADED", "AWAITING_LABELS", "LABELS_SUBMITTED",
    "COMPLETED", "FAILED",
}


@router.get("/projects/mine")
def my_projects(
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
):
    """List the signed-in user's projects with their public share URLs.

    This is what fills the "your past runs" list on the landing page. ``user``
    is the identity from the API key or sign-in headers: the signed-in email, or
    ``"default"`` when sign-in is off.
    """
    from app.services.filebrowser_client import filebrowser_enabled, share_url

    fb_on = filebrowser_enabled()
    rows = (
        db.query(models.Project)
        .filter_by(user_id=user)
        .order_by(models.Project.updated_at.desc(), models.Project.created_at.desc())
        .all()
    )
    out = []
    for p in rows:
        hash_ = getattr(p, "share_hash", None)
        out.append({
            "project_id": p.id,
            "name": p.name,
            "state": p.state,
            "run_name": getattr(p, "run_name", None),
            "updated_at": p.updated_at.isoformat() if p.updated_at else None,
            "files_url": share_url(hash_) if (fb_on and hash_) else None,
        })
    return {"projects": out}


@router.get("/detectors")
def get_detectors(user: str = Depends(require_api_key)):
    """List the Detectree2 detector weights, saying which are present and default."""
    return list_models()


@router.get("/feature-extractors")
def get_feature_extractors(user: str = Depends(require_api_key)):
    """List the DINOv2 feature extractors, the values model_name accepts."""
    return list_backbones()


@router.post("/projects", status_code=201)
def create_project(
    body: ProjectCreate,
    db: Session = Depends(get_db),
    user: str = Depends(require_api_key),
):
    model_key = body.model_key or DEFAULT_MODEL_KEY
    try:
        resolve_model_path(model_key)
        # Check the feature extractor against the catalog now, so a bad
        # model_name fails here instead of inside the worker at Step 1B.
        body.params.model_name = resolve_backbone(body.params.model_name)
    except ValueError as e:
        raise HTTPException(400, {"code": "BAD_REQUEST", "message": str(e),
            "hint": ("pick a detector and feature extractor the server offers — GET "
                     "/api/v1/detectors and /api/v1/feature-extractors list the "
                     "valid values"),
        })

    project = models.Project(
        user_id=user,
        name=body.name,
        model_key=model_key,
        source_epsg=body.source_epsg,
        params=body.params.model_dump(),
        state="CREATED",
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    ensure_project_dirs(project.id, project.current_run)

    # Create a permanent FileBrowser share for this project's output folder. If
    # that fails, the project is still created.
    try:
        from app.services.filebrowser_client import create_project_share, filebrowser_enabled
        if filebrowser_enabled():
            project.share_hash = create_project_share(project.id)
            db.add(project)
            db.commit()
    except Exception as exc:
        import logging
        logging.getLogger(__name__).warning("FileBrowser share creation failed: %s", exc)

    return serialize_project(project)


@router.get("/projects")
def list_projects(db: Session = Depends(get_db), user: str = Depends(require_api_key)):
    rows = (
        db.query(models.Project)
        .filter_by(user_id=user)
        .order_by(models.Project.created_at.desc())
        .all()
    )
    return [serialize_project(p) for p in rows]


@router.get("/projects/{project_id}")
@router.get("/project")
def get_one(project=Depends(get_project)):
    return serialize_project(project)



def _assert_project_not_busy(project, what: str) -> None:
    """Raise 409 if a run, or another change to the inputs, is already going.

    Every path that changes a project's inputs calls this, so they all give the
    same message.
    """
    if project.state in _BUSY_STATES:
        raise HTTPException(409, {
            "code": ERROR_CODES["CONFLICT_BUSY"],
            "message": f"Cannot {what} while a run is in progress",
            "project_id": project.id,
            "hint": "wait for the current run to finish, then retry",
        })


def _assert_ortho_add_allowed(project) -> None:
    """Check whether an orthomosaic may be added to the library.

    This is only the busy check, on purpose. The older rule returned 423
    ORTHO_LOCKED once a project had been analyzed, which made a reusable library
    impossible: you could never add a second site to a project that had already
    produced a run. Adding one is safe because no existing run's inputs change.
    Each run records the orthomosaic it used, and an earlier run's
    work/run_<n>/ folder is never read from the library again.
    """
    _assert_project_not_busy(project, "add an orthomosaic")


def _assert_ortho_unlocked(project) -> None:
    """The original dataset freeze, now used by the ground-truth upload only,
    which keeps exactly the protection it had before."""
    if project.state in _BUSY_STATES:
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": "Cannot change the dataset while a run is in progress",
            "hint": "wait for the current run to finish, then change the dataset",
            "project_id": project.id,
        })
    # A run counter above 1, or any archived run history, means this project has
    # been analyzed at least once, so the dataset is frozen even though a newly
    # opened re-run sits in UPLOADED.
    has_prior_run = (project.current_run or 1) > 1 or bool(project.runs)
    if project.state not in ("CREATED", "UPLOADED") or has_prior_run:
        raise HTTPException(423, {
            "code": "ORTHO_LOCKED",
            "message": (
                "The ground-truth dataset is locked because this project has "
                "already been analyzed. Re-runs change parameters and labels "
                "only — start a new project to use different ground truth."
            ),
            "project_id": project.id,
        
            "hint": ("start a new project for the new ground truth — replacing it "
                     "here would invalidate the validation output of the run that "
                     "already used it"),
        })


@contextmanager
def _dataset_edit_lock(db: Session, project):
    pre_state = project.state
    if not transition_if(db, project, {pre_state}, _UPLOADING):
        db.refresh(project)
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": f"Project is busy (state {project.state}); try again",
            "hint": ("another upload or run claimed this project a moment ago \u2014 wait for it to finish and retry"),
            "project_id": project.id,
        })
    try:
        yield pre_state
    finally:
        remaining = db.query(models.Ortho).filter_by(project_id=project.id).count()
        transition_if(db, project, {_UPLOADING}, pre_state if remaining else "CREATED")


@router.patch("/projects/{project_id}")
@router.patch("/project")
def update_project(
    body: ProjectUpdate,
    project=Depends(get_project),
    db: Session = Depends(get_db),
):

    if project.state in _BUSY_STATES:
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": "Cannot change parameters while a run is in progress",
            "hint": ("wait for the current run to finish \u2014 its settings are frozen while it computes"),
            "project_id": project.id,
        })
    if not project.orthos:
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "Upload an orthomosaic before configuring a re-run",
            "hint": "add a GeoTIFF in step 2 first",
            "project_id": project.id,
        })

    # Check the parameter types now, so a bad value fails here with a 400 rather
    # than inside the worker with a 500.
    if body.params:
        _validate_param_overrides(body.params)
        # Rules that span two fields have to be checked against the merged
        # result. Otherwise a bad pair, such as area_min above the stored
        # area_max, would be saved here and only fail later at analyze time.
        _validate_merged_params(project, body.params)

    # Merge the new params into the existing ones, then check the model choices.
    new_params = dict(project.params or {})
    if body.params:
        new_params.update(body.params)
    try:
        if body.model_key is not None:
            resolve_model_path(body.model_key)
        if "model_name" in new_params:
            new_params["model_name"] = resolve_backbone(new_params.get("model_name"))
    except ValueError as e:
        raise HTTPException(400, {"code": "BAD_REQUEST", "message": str(e),
                                  "project_id": project.id,
            "hint": ("pick a detector and feature extractor the server offers — GET "
                     "/api/v1/detectors and /api/v1/feature-extractors list the "
                     "valid values"),
        })

    # Take the state with a conditional update before changing anything. The
    # busy check above reads the state and then acts on it, so an analyze
    # trigger can start a run in between. Writing UPLOADED after that would
    # archive the run and move current_run on underneath the running job, and
    # immediately let a second analyze start. Requiring the exact state we
    # checked against means the losing request fails cleanly instead.
    pre_state = project.state
    if not transition_if(db, project, {pre_state}, "UPLOADED"):
        db.refresh(project)
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": f"Project moved to {project.state} while being reconfigured",
            "project_id": project.id,
            "hint": "wait for the current run to finish, then retry",
        })

    if pre_state in USED_RUN_STATES:
        archive_current_run(db, project)

    project.params = new_params
    if body.model_key is not None:
        project.model_key = body.model_key
    if body.source_epsg is not None:
        project.source_epsg = body.source_epsg
    if body.run_name is not None:
        project.run_name = body.run_name.strip() or None
    db.add(project)
    db.commit()
    db.refresh(project)
    ensure_project_dirs(project.id, project.current_run)
    return serialize_project(project)


@router.delete("/projects/{project_id}", status_code=204)
@router.delete("/project", status_code=204)
def delete_one(project=Depends(get_project), db: Session = Depends(get_db)):
    # Take the state before deleting anything. Without this, a delete arriving
    # while a job runs removes the whole project tree underneath it. Any state
    # that is not busy can be taken, and nobody ever sees DELETING, because the
    # row is gone by the end of the same request.
    if not transition_if(db, project, _DELETABLE_STATES, _DELETING):
        db.refresh(project)
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": f"Cannot delete while a run is in progress (state {project.state})",
            "project_id": project.id,
            "hint": "wait for the current run to finish",
        })
    delete_project_dir(project.id)
    db.delete(project)
    db.commit()
    return None


@router.post("/projects/{project_id}/orthomosaic")
@router.post("/project/orthomosaic")
def upload_ortho(
    project=Depends(get_project),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Add an orthomosaic to the project's library.

    Each upload adds a row and nothing already registered is removed. There is
    no route that deletes a single orthomosaic. Once a project reaches its quota
    or count limit the upload is refused, and the answer is a new project rather
    than a delete.

    This is allowed whenever the project is not busy. Adding cannot disturb an
    earlier run, because every run records the one orthomosaic it started with.

    Capacity is checked before the request body is accepted, in
    ``_assert_project_capacity``, and the filename stem is made unique within
    the project, so ``site_a`` may become ``site_a_2``. The ``orthos`` list in
    the response gives the stem the file actually got.
    """
    _assert_ortho_add_allowed(project)
    if not (file.filename or "").lower().endswith((".tif", ".tiff")):
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": "Orthomosaic must be a .tif/.tiff GeoTIFF",
            "hint": ("export your survey as a GeoTIFF \u2014 JPEG, PNG and ECW carry no geographic reference, so the output could not be mapped"),
            "project_id": project.id,
        })

    with _dataset_edit_lock(db, project) as pre_state:
        # Checked inside the lock, so two uploads arriving at once cannot both
        # pass the capacity check.
        _assert_project_capacity(db, project)
        paths = ensure_project_dirs(project.id, project.current_run)

        original = os.path.basename(file.filename)
        stem = _unique_stem(db, project.id, os.path.splitext(original)[0])
        dst = os.path.join(paths["input_ortho"], f"{stem}.tif")
        _stream_to_disk(file, dst, max_bytes=settings.max_upload_mb * 1024 * 1024,
                        project_id=project.id)
        return _register_ortho(project, db, dst, stem, original, pre_state=pre_state)


@router.post("/projects/{project_id}/orthomosaic/from-url")
@router.post("/project/orthomosaic/from-url")
def upload_ortho_from_url(
    body: OrthoFromUrl,
    project=Depends(get_project),
    db: Session = Depends(get_db),
):
    """Add an orthomosaic by downloading it from a public Google Drive link.

    The request stays open while the server downloads the file, so clients need
    a long read timeout for a large orthomosaic. Only Drive share links set to
    "anyone with the link" work. This behaves like the file upload in every
    other way, sharing ``_assert_project_capacity``, ``_unique_stem`` and
    ``_register_ortho`` with it.
    """
    _assert_ortho_add_allowed(project)
    url = (body.url or "").strip()
    host = urlparse(url).netloc.lower()
    if not (host == "drive.google.com" or host.endswith(".google.com")):
        raise HTTPException(400, {"code": "BAD_REQUEST",
            "message": "Only Google Drive links are supported.", "project_id": project.id, "hint": ("paste a Google Drive share link, or use the file picker above for a file already on this computer"),})

    file_id = _extract_drive_id(url)
    if not file_id:
        raise HTTPException(400, {"code": "BAD_REQUEST",
            "message": "Could not parse a Google Drive file id from the URL.",
            "hint": ("use the link from Drive\u2019s Share > Copy link \u2014 a browser address-bar URL from an open preview often will not work"),
            "project_id": project.id})

    try:
        import gdown  # noqa: F401 - just checking it is installed; the child uses it
    except ImportError:
        raise HTTPException(503, {"code": "DEPENDENCY_MISSING",
            "message": "Server is missing the 'gdown' dependency required for URL uploads.",
            "hint": ("ask an administrator to install it; meanwhile upload the file with the file picker instead")})

    with _dataset_edit_lock(db, project) as pre_state:
        # The same check, in the same place as the browser upload: inside the
        # lock, before any bytes are fetched.
        _assert_project_capacity(db, project)
        return _download_ortho_from_drive(project, db, file_id, pre_state)


def _spawn_drive_child(file_id: str, tmp_dir: str):
    """Start the download in its own interpreter. Separate so tests can patch it.

    This uses spawn rather than fork, because uvicorn runs threads and a forked
    child can inherit a lock held by a thread that does not exist on its side,
    which can deadlock inside a network library. Spawn costs about a second of
    start-up against a transfer measured in minutes.
    """
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=drive_download.download_to, args=(file_id, tmp_dir),
                       daemon=True, name=f"ortho-dl-{file_id[:12]}")
    proc.start()
    return proc


def _download_ortho_from_drive(project, db: Session, file_id: str, pre_state: str | None = None):
    """The work behind the from-URL upload, run under ``_dataset_edit_lock``.

    The download happens in a child process so the time limit can be applied.
    ``gdown.download`` is one blocking call with no way to cancel it, so in this
    process a stalled Drive connection would hold the worker and the project's
    UPLOADING state until Drive gave up. In its own process it is a PID, and a
    PID can be killed.
    """
    paths = ensure_project_dirs(project.id, project.current_run)
    tmp_dir = tempfile.mkdtemp(prefix="ortho_dl_", dir=paths["input_ortho"])
    try:
        budget = _transfer_budget_s()
        started = time.monotonic()
        proc = _spawn_drive_child(file_id, tmp_dir)
        proc.join(budget)                      # a budget of None waits forever

        if proc.is_alive():
            # Out of time. Kill rather than terminate: gdown installs no signal
            # handler worth waiting for, and the limit is meant to be firm. The
            # child holds no lock and owns nothing but the temporary directory
            # the finally block below removes.
            waited = time.monotonic() - started
            proc.kill()
            proc.join(10)
            raise _transfer_timeout(project.id, "The Drive download", waited)

        err = drive_download.read_error(tmp_dir)
        if err:
            raise HTTPException(400, {"code": "BAD_REQUEST",
                "message": err, "project_id": project.id,
                "hint": ("check the Drive link is set to 'Anyone with the link' and "
                         "points at the GeoTIFF itself"),
            })

        out = drive_download.downloaded_file(tmp_dir)
        if not out:
            # The child died without recording a reason: killed by the operating
            # system, out of memory, or a crash inside gdown.
            raise HTTPException(400, {"code": "BAD_REQUEST",
                "message": ("Download failed - the file may be private, deleted, or over "
                            "its Google Drive download quota."), "project_id": project.id,
                "details": {"child_exitcode": proc.exitcode},
                "hint": ("set the link to 'Anyone with the link' in Drive and try again "
                         "— a file downloaded very often can also be temporarily blocked "
                         "by Drive's own quota"),
            })

        max_bytes = settings.max_upload_mb * 1024 * 1024
        if os.path.getsize(out) > max_bytes:
            raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE",
                "message": f"Downloaded file exceeds the {settings.max_upload_mb} MB limit.",
                "hint": ("upload a smaller crop of the survey, or ask an administrator to raise TCP_MAX_UPLOAD_MB"),
                "project_id": project.id})
        if not out.lower().endswith((".tif", ".tiff")):
            raise HTTPException(400, {"code": "BAD_REQUEST",
                "message": "The Drive file is not a .tif/.tiff GeoTIFF.", "project_id": project.id, "hint": "share the GeoTIFF itself, not a folder, a zip, or a preview image",})

        original = os.path.basename(out)
        stem = _unique_stem(db, project.id, os.path.splitext(original)[0])
        dst = os.path.join(paths["input_ortho"], f"{stem}.tif")
        shutil.move(out, dst)
        return _register_ortho(project, db, dst, stem, original, pre_state=pre_state)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


@router.get("/projects/{project_id}/orthomosaics")
@router.get("/project/orthomosaics")
def list_orthomosaics(project=Depends(get_project), db: Session = Depends(get_db)):
    """The project's orthomosaics, and how much of its quota they use.

    ``in_use`` marks an orthomosaic that a run with results depends on. Nothing
    can be removed from the library through the API, so the flag is only
    information: it says which files still back existing output, which a future
    central store would need in order to decide what can be reclaimed.
    ``uploaded_at`` is the file's modification time, the only timestamp
    available without a schema change, and is null when the file is missing, in
    which case ``missing`` is true.
    """
    paths = project_paths(project.id, project.current_run or 1)
    refs = _run_ortho_refs(project)
    rows = (
        db.query(models.Ortho)
        .filter_by(project_id=project.id)
        .order_by(models.Ortho.stem)
        .all()
    )
    used, count = _project_usage(db, project.id)
    out = []
    for o in rows:
        path = ortho_file_path(paths, o)
        uploaded_at = None
        if path:
            try:
                uploaded_at = datetime.fromtimestamp(os.path.getmtime(path)).isoformat()
            except OSError:
                uploaded_at = None
        out.append({
            "id": o.id,
            "stem": o.stem,
            "filename": o.filename,
            "size_bytes": o.size_bytes,
            "width": o.width,
            "height": o.height,
            "crs": o.crs,
            "crs_epsg": _epsg_from_crs(o.crs),
            "bands": o.bands,
            "uploaded_at": uploaded_at,
            "missing": path is None,
            "in_use": bool(refs & {o.id, o.stem, o.filename}),
        })
    quota = _quota_bytes()
    return {
        "project_id": project.id,
        "state": project.state,
        "orthos": out,
        "usage": {
            "used_bytes": used,
            "quota_bytes": quota or None,
            "count": count,
            "max_orthos": int(getattr(settings, "max_orthos_per_project", 0) or 0) or None,
        },
    }


# Limits on a ground-truth zip, so a hostile archive cannot fill the disk.
_GT_MAX_MEMBERS = 10000                          # reject absurd file counts
_GT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024     # 2 GiB extracted in total
_GT_MAX_FILE_BYTES = 250 * 1024 * 1024           # 250 MiB per extracted file
_GT_MAX_RATIO = 200                              # highest uncompressed:compressed ratio


@router.post("/projects/{project_id}/ground-truth")
@router.post("/project/ground-truth")
def upload_ground_truth(
    project=Depends(get_project),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a .zip whose top-level folders are species names holding crown
    .tif files, which is the layout step3_validate expects.

    The upload is guarded against zip bombs and hostile archives: the compressed
    file is size-capped, the archive is inspected before anything is written,
    and only ordinary ``*.tif`` members are extracted, each into
    ``<species>/<file>.tif`` under a cleaned path and against per-file and total
    size limits. A lying header or a decompression bomb therefore cannot fill
    the disk. Any ground truth already stored is replaced.
    """
    _assert_ortho_unlocked(project)   # ground truth keeps the original freeze
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, {"code": "BAD_ARCHIVE",
            "message": "Ground truth must be a .zip of <species>/*.tif folders",
            "hint": ("zip a folder that holds one sub-folder per species, each containing that species\u2019 crown images"),
            "project_id": project.id})

    import zipfile

    with _dataset_edit_lock(db, project):
        paths = ensure_project_dirs(project.id, project.current_run)
        # Write the upload outside input_gt, so input_gt can be emptied and
        # replaced. The temporary name is fixed, so two uploads at once would
        # overwrite each other's file; the lock is what stops that.
        tmp = os.path.join(paths["root"], "_gt_upload.zip")
        _stream_to_disk(file, tmp, max_bytes=settings.max_upload_mb * 1024 * 1024,
                        project_id=project.id)

        try:
            try:
                zf = zipfile.ZipFile(tmp)
            except zipfile.BadZipFile:
                raise HTTPException(400, {"code": "BAD_ARCHIVE",
                    "message": "File is not a valid .zip archive", "project_id": project.id, "hint": "re-create the zip \u2014 this file is not readable as a zip archive",})
            with zf as z:
                extracted = _safe_extract_gt_tifs(z, paths["input_gt"])
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)

    if extracted == 0:
        raise HTTPException(400, {"code": "BAD_ARCHIVE",
            "message": "Archive contained no usable .tif ground-truth images",
            "hint": ("the zip must hold <species>/<image>.tif \u2014 images at the top level, or inside an extra nested folder, are not picked up"),
            "project_id": project.id})

    species = sorted(
        d for d in os.listdir(paths["input_gt"])
        if os.path.isdir(os.path.join(paths["input_gt"], d))
    )
    return {
        "project_id": project.id,
        "state": project.state,
        "species_folders": species,
        "files_extracted": extracted,
    }


# Helpers.
_DRIVE_ID_PATTERNS = [
    re.compile(r"/file/d/([A-Za-z0-9_-]{10,})"),
    re.compile(r"[?&]id=([A-Za-z0-9_-]{10,})"),
    re.compile(r"/d/([A-Za-z0-9_-]{10,})"),
]


def _extract_drive_id(url: str) -> str | None:
    """Pull the file id out of common Google Drive URL shapes."""
    for rx in _DRIVE_ID_PATTERNS:
        m = rx.search(url)
        if m:
            return m.group(1)
    return None


def ortho_file_path(paths: dict, ortho) -> str | None:
    """The full path of an orthomosaic's file, or None if it is not on disk.

    It is found from ``stem``, which is both the name on disk, ``<stem>.tif``,
    and the orthomosaic's identity within the project. ``filename`` is not used
    first, because since the library was introduced it holds the name the user
    uploaded and can differ. Older rows, written when the two always matched,
    resolve the same way, and ``filename`` is still tried last so a file placed
    by hand is still found.
    """
    root = paths["input_ortho"]
    candidates = [f"{ortho.stem}.tif", f"{ortho.stem}.tiff"]
    if ortho.filename:
        candidates.append(ortho.filename)
    for name in candidates:
        cand = os.path.join(root, name)
        if os.path.exists(cand):
            return cand
    return None


def _unique_stem(db: Session, project_id: str, base: str) -> str:
    """Return a stem no other orthomosaic in the project uses.

    A repeated name gets a number: ``site_a``, then ``site_a_2``, then
    ``site_a_3``. The stem is the orthomosaic's identity and its filename on
    disk, so without this one upload would overwrite another's pixels and
    inherit its detections.

    This only works because every caller runs inside ``_dataset_edit_lock``. The
    UPLOADING state lets one upload run at a time per project, so no other
    upload can take the stem between the query here and the insert in
    ``_register_ortho``. The check against the disk is a second guard: a file
    left behind by an interrupted upload also keeps its stem reserved.
    """
    base = re.sub(r"[^A-Za-z0-9._-]", "_", base).strip("._") or "ortho"
    taken = {
        s for (s,) in db.query(models.Ortho.stem).filter_by(project_id=project_id).all()
    }
    paths = project_paths(project_id, 1)
    ortho_dir = paths["input_ortho"]

    def _free(candidate: str) -> bool:
        if candidate in taken:
            return False
        return not any(
            os.path.exists(os.path.join(ortho_dir, candidate + ext))
            for ext in (".tif", ".tiff")
        )

    if _free(base):
        return base
    # Start at _2, so the first duplicate reads as "the second site_a".
    n = 2
    while not _free(f"{base}_{n}"):
        n += 1
        if n > 10000:                      # give up rather than loop forever
            raise HTTPException(409, {
                "code": ERROR_CODES["ORTHO_COUNT_EXCEEDED"],
                "message": f"Too many orthomosaics named '{base}' in this project.",
                "project_id": project_id,
                "hint": "rename the file before uploading",
            })
    return f"{base}_{n}"


def _project_usage(db: Session, project_id: str) -> tuple[int, int]:
    """Return (bytes stored, number of orthomosaics) for a project.

    A row with a NULL ``size_bytes`` counts as 0. The column is only NULL for
    rows written before it existed, and there is no cheap way to fill it in now.
    """
    used = db.query(func.coalesce(func.sum(models.Ortho.size_bytes), 0)).filter(
        models.Ortho.project_id == project_id
    ).scalar() or 0
    count = db.query(func.count(models.Ortho.id)).filter(
        models.Ortho.project_id == project_id
    ).scalar() or 0
    return int(used), int(count)


def _quota_bytes() -> int:
    gb = float(getattr(settings, "project_quota_gb", 0) or 0)
    return int(gb * 1024 * 1024 * 1024)


def _assert_project_capacity(db: Session, project) -> None:
    """Decide whether a new upload may start. Call inside ``_dataset_edit_lock``.

    An upload already running is always allowed to finish, and only a further
    one is refused. The check is therefore ``used >= limit`` against what is
    already stored, not ``used + incoming > limit``, which would stop the very
    upload that crosses the line part-way through. The quota can be exceeded by
    at most one orthomosaic, bounded by ``max_upload_mb``.

    A limit of 0 or less turns that check off.
    """
    used, count = _project_usage(db, project.id)
    max_count = int(getattr(settings, "max_orthos_per_project", 0) or 0)
    if max_count > 0 and count >= max_count:
        raise HTTPException(409, {
            "code": ERROR_CODES["ORTHO_COUNT_EXCEEDED"],
            "message": (
                f"This project already holds {count} orthomosaics, the maximum "
                f"of {max_count}."
            ),
            "project_id": project.id,
            "hint": ("start a new project for the next site, or ask an administrator "
                     "to raise the limit — orthomosaics cannot be removed here"),
            "details": {"orthos": count, "max_orthos": max_count},
        })
    quota = _quota_bytes()
    if quota > 0 and used >= quota:
        gb = used / (1024 * 1024 * 1024)
        raise HTTPException(413, {
            "code": ERROR_CODES["PROJECT_QUOTA_EXCEEDED"],
            "message": (
                f"This project already stores {gb:.1f} GB of orthomosaics, at or "
                f"over its {settings.project_quota_gb} GB quota."
            ),
            "project_id": project.id,
            "hint": ("start a new project for the next site, or ask an administrator "
                     "to raise the limit — orthomosaics cannot be removed here"),
            "details": {"used_bytes": used, "quota_bytes": quota},
        })


def _transfer_budget_s() -> float | None:
    """How many seconds one orthomosaic transfer may take, or None if unlimited.

    One limit covers both the browser upload and the Drive download on purpose.
    To the user, "the transfer took too long" is the same thing either way, and
    two settings would only have to be kept in step with each other.
    """
    mins = int(getattr(settings, "ortho_transfer_timeout_min", 0) or 0)
    return mins * 60.0 if mins > 0 else None


def _transfer_timeout(project_id: str, what: str, waited_s: float):
    """Build the 408 both transfer paths raise, so both look the same."""
    mins = int(getattr(settings, "ortho_transfer_timeout_min", 0) or 0)
    return HTTPException(408, {
        "code": ERROR_CODES["TRANSFER_TIMEOUT"],
        "message": (
            f"{what} passed the {mins}-minute limit for one orthomosaic and was "
            f"stopped after {int(waited_s // 60)}m{int(waited_s % 60):02d}s. "
            f"Nothing was kept."
        ),
        "project_id": project_id,
        "hint": ("retry on a faster connection, or ask an administrator to raise "
                 "TCP_ORTHO_TRANSFER_TIMEOUT_MIN"),
        "details": {"limit_min": mins, "waited_s": round(waited_s, 1)},
    })


def _run_ortho_refs(project) -> set[str]:
    """Every orthomosaic named by a run that produced results.

    Two sources are read:
      * archived runs. An entry in ``project.runs`` holds ``ortho_id`` and
        ``ortho_stem`` if it was archived recently, or just ``ortho``, a
        filename, if it is older, so all three are collected.
      * the current run, once it has reached a state whose output exists:
        AWAITING_LABELS, LABELS_SUBMITTED or COMPLETED. FAILED is left out on
        purpose, since a failed run produced nothing worth keeping and holding
        on to the orthomosaic that failed would not help anyone.

    The result is a plain set of strings, so a caller can test an orthomosaic's
    id, stem and filename against it without knowing which one a run recorded.
    """
    refs: set[str] = set()
    for entry in (project.runs or []):
        if not isinstance(entry, dict):
            continue
        for key in ("ortho_id", "ortho_stem", "ortho"):
            val = entry.get(key)
            if val:
                refs.add(str(val))
    if project.state in ("AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED"):
        params = dict(project.params or {})
        for key in ("ortho_id", "ortho_stem"):
            val = params.get(key)
            if val:
                refs.add(str(val))
        # A run started before orthomosaic selection existed recorded nothing.
        # On a project holding one orthomosaic, that run must have used it.
        if not params.get("ortho_id") and not params.get("ortho_stem"):
            if len(project.orthos) == 1:
                o = project.orthos[0]
                refs.update({o.id, o.stem, o.filename})
    return refs


def _register_ortho(project, db: Session, dst: str, stem: str, original: str | None = None,
                    pre_state: str | None = None):

    meta = _raster_meta(dst)
    if meta.get("crs_epsg") and not project.source_epsg:
        project.source_epsg = meta["crs_epsg"]

    o = models.Ortho(project_id=project.id, stem=stem)
    o.filename = original or f"{stem}.tif"
    o.width = meta.get("width")
    o.height = meta.get("height")
    o.crs = meta.get("crs")
    o.bands = meta.get("bands")
    o.size_bytes = os.path.getsize(dst)
    db.add(o)
    db.add(project)
    db.commit()

    # This runs under _dataset_edit_lock, so the project is in the UPLOADING
    # state and releasing it here is what makes the new orthomosaic visible. The
    # release is conditional, so it cannot overwrite a state someone else set.
    #
    # A project whose current run has already produced results goes back to
    # exactly the state it was in, not to UPLOADED. That state is the only
    # record that work/run_<n> has been used: _apply_run_config in runs.py
    # archives the run and increments current_run only when the state before
    # analyze is one of USED_RUN_STATES. Writing UPLOADED here would erase that,
    # so the next analyze would reuse the same run folder, the worker's
    # reset_dirs would delete the finished run's step1 to step4 output, and the
    # run would never reach project.runs. Adding an orthomosaic has to leave the
    # existing run alone, which is the reason _assert_ortho_add_allowed no
    # longer applies the old freeze.
    #
    # It also keeps `in_use` accurate: _run_ortho_refs reads the current run's
    # orthomosaic only while the project is in one of those states.
    released = pre_state if pre_state in USED_RUN_STATES else "UPLOADED"
    transition_if(db, project, {_UPLOADING, "CREATED", "UPLOADED"}, released)
    db.refresh(project)
    return serialize_project(project)


def _validate_param_overrides(overrides: dict) -> None:

    from typing import Annotated

    from pydantic import TypeAdapter

    from app.schemas.project import PipelineParams
    fields = PipelineParams.model_fields
    for key, val in (overrides or {}).items():
        if key in fields:
            try:
                TypeAdapter(
                    Annotated[fields[key].annotation, fields[key]]
                ).validate_python(val)
            except Exception as e:
                raise HTTPException(400, {"code": "BAD_REQUEST",
                    "message": f"Invalid value for param '{key}': {e}",
                    "hint": ("that setting is out of range — the Parameter Guide lists what "
                             "each one accepts and what changing it does"),
                })


def _validate_merged_params(project, overrides: dict) -> None:
    """Check PipelineParams' cross-field rules against the params as they will
    be stored, not just against the values coming in.

    Both the project update and the analyze trigger merge new values into the
    project's existing params, so checking each key on its own is not enough.
    Sending only ``area_min: 5000`` is within that field's bounds, but is
    invalid next to a stored ``area_max`` of 2000. Both write paths call this,
    so a bad combination cannot be saved.

    Keys PipelineParams does not know about are dropped first, so older or
    operator-only params already on the project cannot fail the check.
    """
    from pydantic import ValidationError

    from app.schemas.project import PipelineParams

    merged = {**(getattr(project, "params", None) or {}), **(overrides or {})}
    known = {k: v for k, v in merged.items() if k in PipelineParams.model_fields}
    try:
        PipelineParams(**known)
    except ValidationError as e:
        first = e.errors()[0]
        raise HTTPException(400, {
            "code": "BAD_REQUEST",
            "message": first.get("msg", str(e)),
            "project_id": project.id,
        
            "hint": ("correct that setting in step 3 — the Parameter Guide explains "
                     "what it accepts and what changing it does"),
        })


def _stream_to_disk(
    file: UploadFile,
    dst: str,
    chunk: int = 1024 * 1024,
    max_bytes: int | None = None,
    project_id: str = "",
) -> None:
    """Write an upload to disk, stopping if it grows too large or takes too long.

    ``max_bytes`` stops the write as soon as the body goes past it, so an
    oversized upload is never fully written.

    The time limit is checked once per chunk. With 1 MiB chunks that stops
    within about a second of the deadline on any connection worth waiting for,
    and on a connection so slow that one chunk read blocks past the deadline,
    the check still runs when that read returns. Either way the part-written
    file is deleted, because a half-written GeoTIFF is worse than none: the next
    thing to open it would be rasterio.
    """
    budget = _transfer_budget_s()
    started = time.monotonic()
    written = 0

    def _abort(exc: HTTPException, out) -> HTTPException:
        out.close()
        try:
            os.remove(dst)
        except OSError:
            pass
        try:
            file.file.close()
        except Exception:                     # noqa: BLE001 - this path is already failing
            pass
        return exc

    with open(dst, "wb") as out:
        while True:
            data = file.file.read(chunk)
            if not data:
                break
            written += len(data)
            if max_bytes is not None and written > max_bytes:
                raise _abort(HTTPException(413, {"code": "UPLOAD_TOO_LARGE",
                    "message": f"Upload exceeds the {max_bytes // (1024 * 1024)} MB limit."}), out)
            if budget is not None:
                elapsed = time.monotonic() - started
                if elapsed > budget:
                    raise _abort(_transfer_timeout(project_id, "The upload", elapsed), out)
            out.write(data)
    file.file.close()


def _raster_meta(path: str) -> dict:
    """Read a raster's size, CRS and band count. Empty dict if that fails."""
    try:
        import rasterio

        with rasterio.open(path) as src:
            try:
                epsg = src.crs.to_epsg() if src.crs else None
            except Exception:
                epsg = None
            return {
                "width": src.width,
                "height": src.height,
                "crs": str(src.crs) if src.crs else None,
                "crs_epsg": epsg,
                "bands": src.count,
            }
    except Exception:
        return {}


_EPSG_RX = re.compile(r"EPSG:(\d+)", re.IGNORECASE)


def _epsg_from_crs(crs: str | None) -> int | None:
    """Pull the EPSG code out of a stored CRS string such as 'EPSG:32643'.

    It reads the string already on the row rather than opening the GeoTIFF
    again, so listing ten orthomosaics costs no raster reads. A CRS given only
    as WKT returns None, and the UI shows the raw string in that case.
    """
    m = _EPSG_RX.search(crs or "")
    return int(m.group(1)) if m else None


def _safe_member_path(name: str) -> str | None:
    """Turn a zip member's name into a safe ``<species>/<file>`` path.

    Returns None if the name cannot be made safe. Drive letters and leading
    separators are removed, ``..`` and absolute paths are rejected, deeper
    nesting is flattened to ``<species>/<file>``, and each part is restricted to
    safe characters.
    """
    name = name.replace("\\", "/")
    if name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        return None
    parts = [p for p in name.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        return None
    fname = parts[-1]
    species = parts[-2] if len(parts) >= 2 else "unlabelled"

    def _clean(seg: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", seg).strip("._") or "x"

    safe_name = _clean(fname)
    if not safe_name.lower().endswith(".tif"):
        return None
    return os.path.join(_clean(species), safe_name)


def _safe_extract_gt_tifs(z, dest: str) -> int:
    """Check a ground-truth zip and extract only its safe ``*.tif`` members.

    It limits the number of members, the total extracted size and each member's
    compression ratio, so a decompression bomb cannot fill the disk, and it
    rejects dangerous paths such as ``..``, absolute paths, symlinks and device
    files. Returns how many files were written.
    """
    infos = z.infolist()
    if len(infos) > _GT_MAX_MEMBERS:
        raise HTTPException(400, {"code": "BAD_ARCHIVE", "message": f"Archive has too many entries (> {_GT_MAX_MEMBERS})", "hint": "split the ground truth into smaller zips and upload them separately"})

    # Check the zip's own headers first, to catch obvious bombs before writing.
    declared_total = 0
    for info in infos:
        if info.is_dir():
            continue
        declared_total += info.file_size
        if info.file_size > _GT_MAX_FILE_BYTES:
            raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "message": f"Archive contains an oversized member: {info.filename}", "hint": "ground-truth crown images should be small chips, not full orthomosaics"})
        if info.compress_size > 0 and (info.file_size / info.compress_size) > _GT_MAX_RATIO:
            raise HTTPException(400, {"code": "BAD_ARCHIVE", "message": "Archive looks like a decompression bomb (suspicious ratio)", "hint": "re-create the zip from the original folder with normal compression"})
    if declared_total > _GT_MAX_TOTAL_BYTES:
        raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "message": "Archive exceeds the uncompressed size budget", "hint": "split the ground truth into smaller zips and upload them separately"})

    # Replace rather than merge: delete the previous ground truth and start over.
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)

    written_total = 0
    count = 0
    for info in infos:
        if info.is_dir():
            continue
        # Skip anything that is not an ordinary file, such as a symlink, device
        # or fifo. A mode of 0, which zips made on Windows often have, is
        # treated as an ordinary file.
        mode = (info.external_attr >> 16) & 0o170000
        if mode and mode != 0o100000:
            continue
        if not info.filename.lower().endswith(".tif"):
            continue
        rel = _safe_member_path(info.filename)
        if rel is None:
            continue
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target) or dest, exist_ok=True)
        # Copy in chunks and count the bytes that actually arrive, rather than
        # trusting the size in the zip header.
        file_bytes = 0
        with z.open(info) as src, open(target, "wb") as out:
            while True:
                buf = src.read(1024 * 1024)
                if not buf:
                    break
                file_bytes += len(buf)
                written_total += len(buf)
                if file_bytes > _GT_MAX_FILE_BYTES or written_total > _GT_MAX_TOTAL_BYTES:
                    out.close()
                    try:
                        os.remove(target)
                    except OSError:
                        pass
                    raise HTTPException(413, {"code": "UPLOAD_TOO_LARGE", "message": "Archive exceeds size limits during extraction", "hint": "split the ground truth into smaller zips and upload them separately"})
                out.write(buf)
        count += 1
    return count
