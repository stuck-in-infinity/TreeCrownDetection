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

# Transient states used purely as mutual-exclusion claims. Neither is a valid
# launch state for analyze/finalize, so holding one locks a run out for the
# duration of the request; both are released (or the row deleted) before it
# returns. See db/models.py for the durable state machine.
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
    """List the signed-in user's projects with their public FileBrowser share URL.

    Powers the landing page "your past runs" list. ``user`` is the API-key/SSO
    identity (single-tenant ``"default"`` until Google SSO lands, then the email).
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
    """List the registered Detectree2 detector weight files (+ availability/default)."""
    return list_models()


@router.get("/feature-extractors")
def get_feature_extractors(user: str = Depends(require_api_key)):
    """List the allowed DINOv2 feature-extractor models (valid model_name values)."""
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
        # Validate the feature-extraction backbone against the allowlist so a bad
        # model_name fails here rather than deep in the worker (Step 1B).
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

    # Create a permanent FileBrowser share for this project's output folder.
    # Project creation succeeds either way.
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
    """409 if a run (or another dataset edit) is in flight. Shared by every
    dataset-mutating path so the message is consistent."""
    if project.state in _BUSY_STATES:
        raise HTTPException(409, {
            "code": ERROR_CODES["CONFLICT_BUSY"],
            "message": f"Cannot {what} while a run is in progress",
            "project_id": project.id,
            "hint": "wait for the current run to finish, then retry",
        })


def _assert_ortho_add_allowed(project) -> None:
    """Adding an orthomosaic to the library.

    Deliberately *only* the busy check. The old freeze (423 ORTHO_LOCKED once
    the project had been analyzed once) made a reusable library impossible: you
    could never add a second site to a project that had already produced a run.
    Adding is safe because no existing run's inputs change — each run records
    the single ortho it used, and prior runs' work/run_<n>/ folders are never
    re-read from the library.
    """
    _assert_project_not_busy(project, "add an orthomosaic")


def _assert_ortho_unlocked(project) -> None:
    """The original v5 dataset freeze. UNCHANGED — now used by the ground-truth
    upload only, which keeps exactly the protection it had before."""
    if project.state in _BUSY_STATES:
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": "Cannot change the dataset while a run is in progress",
            "hint": "wait for the current run to finish, then change the dataset",
            "project_id": project.id,
        })
    # A bumped run counter or any archived run history means this project has
    # already been analyzed at least once  dataset is frozen even though a
    # freshly-opened re-run sits in UPLOADED.
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

    # Type-check provided param overrides up front so a bad value fails here with
    # 400 instead of deep in the worker with 500 (v4 section 8.3).
    if body.params:
        _validate_param_overrides(body.params)
        # Cross-field rules must be checked against the merged result, or an
        # invalid pair (e.g. area_min > stored area_max) would be persisted here
        # and only surface later at analyze time.
        _validate_merged_params(project, body.params)

    # Merge param overrides onto the existing params, then validate model choices.
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

    # Claim the state atomically before touching anything. The busy check above
    # is check-then-act: an analyze trigger can win the gap and start a run,
    # after which archiving the run and stamping UPLOADED here would bump
    # current_run out from under the running job and immediately re-open the
    # project for a second, concurrent analyze. Constraining the allowed source
    # to the exact state we validated against makes the loser fail cleanly.
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
    # Claim before deleting anything: without this, a delete landing while a job
    # runs pulls the whole project tree out from under it mid-compute. Any
    # non-busy state may be claimed; the transient DELETING state is never
    # observable, since the row goes away in the same request.
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

    **Append-semantics** (was: set-semantics). Every upload adds a row; nothing
    already registered is deleted, and the library is append-only — there is no
    route that removes a single orthomosaic. When a project reaches its quota or
    count limit the upload is refused; the remedy is a new project, not a delete.

    Allowed whenever the project is not busy — adding cannot disturb a prior
    run, since each run is pinned to the single ortho it was launched with.

    Capacity is checked *before* the body is accepted (see
    ``_assert_project_capacity``), and the filename stem is de-duplicated within
    the project (``site_a`` -> ``site_a_2``); the response's ``orthos`` list
    carries the stem the file actually got.
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
        # Inside the lock: the claim is what makes check-then-act safe, so two
        # simultaneous uploads cannot both pass the capacity gate.
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
    """Add an orthomosaic to the library by downloading it from a public Google
    Drive link.

    Synchronous: the request blocks while the server downloads the file, so set a
    long client read timeout for large orthos. Only Google Drive share links set to
    'anyone with the link' are supported. Same **append-semantics**, same capacity
    gate and same stem de-duplication as the file upload — both paths share
    ``_assert_project_capacity`` / ``_unique_stem`` / ``_register_ortho``.
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
        import gdown  # noqa: F401 - presence check only; the child does the work
    except ImportError:
        raise HTTPException(503, {"code": "DEPENDENCY_MISSING",
            "message": "Server is missing the 'gdown' dependency required for URL uploads.",
            "hint": ("ask an administrator to install it; meanwhile upload the file with the file picker instead")})

    with _dataset_edit_lock(db, project) as pre_state:
        # Same gate, same place as the browser upload: inside the claim, before
        # a single byte is fetched.
        _assert_project_capacity(db, project)
        return _download_ortho_from_drive(project, db, file_id, pre_state)


def _spawn_drive_child(file_id: str, tmp_dir: str):
    """Start the download in its own interpreter. Isolated so tests can patch it.

    ``spawn``, not ``fork``: uvicorn is multi-threaded, and a fork inherits locks
    held by threads that do not exist in the child, which can deadlock in the
    middle of a network library. Spawn pays about a second of start-up, against
    a transfer measured in minutes.
    """
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=drive_download.download_to, args=(file_id, tmp_dir),
                       daemon=True, name=f"ortho-dl-{file_id[:12]}")
    proc.start()
    return proc


def _download_ortho_from_drive(project, db: Session, file_id: str, pre_state: str | None = None):
    """Body of the from-URL upload; runs under ``_dataset_edit_lock``.

    The fetch happens in a child process so the wall-clock budget can be
    enforced. ``gdown.download`` is one blocking call with no cancel hook — in
    process, a stalled Drive connection would hold this worker and the project's
    UPLOADING claim until Drive itself gave up. Out of process it is a PID, and
    a PID can be killed.
    """
    paths = ensure_project_dirs(project.id, project.current_run)
    tmp_dir = tempfile.mkdtemp(prefix="ortho_dl_", dir=paths["input_ortho"])
    try:
        budget = _transfer_budget_s()
        started = time.monotonic()
        proc = _spawn_drive_child(file_id, tmp_dir)
        proc.join(budget)                      # None => wait indefinitely

        if proc.is_alive():
            # Past the budget. SIGKILL, not SIGTERM: gdown installs no signal
            # handler worth waiting on, and the point of the ceiling is that it
            # is not negotiable. The child holds no lock and owns nothing but
            # the scratch directory wiped in the finally below.
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
            # The child died without recording a reason — killed by the OS, out
            # of memory, or a crash inside gdown.
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
    """The project's orthomosaic library, plus its usage against the limits.

    ``in_use`` marks an ortho that a run which produced results depends on. The
    library is append-only — nothing here can be removed through the API — so
    the flag is purely informational: it tells the caller (and a future central
    DBMS deciding what may be reclaimed) which files still back live outputs.
    ``uploaded_at`` is the file's mtime, the only timestamp available without a
    schema change; it is null when the file is missing (``missing: true``).
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


# -- ground-truth zip safety limits (defend against bombs / hostile archives) --
_GT_MAX_MEMBERS = 10000                          # absurd file counts -> reject
_GT_MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024     # 2 GiB uncompressed budget
_GT_MAX_FILE_BYTES = 250 * 1024 * 1024           # 250 MiB per extracted file
_GT_MAX_RATIO = 200                              # uncompressed/compressed ratio guard


@router.post("/projects/{project_id}/ground-truth")
@router.post("/project/ground-truth")
def upload_ground_truth(
    project=Depends(get_project),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload a .zip whose top-level folders are species names containing crown
    .tif files (the structure step3_validate expects).

    Hardened against zip bombs and hostile archives: the compressed upload is
    size-capped, the archive is inspected *before* anything is written, and only
    regular ``*.tif`` members are extracted - each into ``<species>/<file>.tif``
    with a sanitized path and copied through per-file and total-size budgets, so
    a lying header or a decompression bomb cannot fill the disk. Existing ground
    truth is replaced (set-semantics).
    """
    _assert_ortho_unlocked(project)   # ground truth keeps the original v5 freeze
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, {"code": "BAD_ARCHIVE",
            "message": "Ground truth must be a .zip of <species>/*.tif folders",
            "hint": ("zip a folder that holds one sub-folder per species, each containing that species\u2019 crown images"),
            "project_id": project.id})

    import zipfile

    with _dataset_edit_lock(db, project):
        paths = ensure_project_dirs(project.id, project.current_run)
        # Stage the upload OUTSIDE input_gt so input_gt can be wiped for
        # set-semantics. The filename is fixed, so two concurrent uploads would
        # overwrite each other's staging file — the lock is what prevents it.
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


# -- helpers --------------------------------------------------------------
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
    """Absolute path of an ortho's file, or None if it is missing on disk.

    Resolved from ``stem``, which IS the on-disk name (``<stem>.tif``) and the
    project-unique identity — not from ``filename``, which since the library
    change holds the user's original upload name and may differ. Legacy rows
    (written when the two were always equal) resolve identically; ``filename``
    is still tried last so a hand-placed file is still found.
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
    """Return a stem unique within the project, suffixing on collision.

    ``site_a`` -> ``site_a`` -> ``site_a_2`` -> ``site_a_3``. The stem is the
    ortho's identity (plan v2 §14.1) and its on-disk filename, so a collision
    would otherwise make one upload silently overwrite another's pixels and
    inherit its detections.

    Safe only because every caller runs inside ``_dataset_edit_lock``: the
    transient UPLOADING claim serialises uploads per project, so no second
    uploader can take the stem between the SELECT here and the INSERT in
    ``_register_ortho``. The on-disk check is a second belt — a file left behind
    by an interrupted upload also blocks reuse of its stem.
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
    # Start at _2 so the first duplicate reads as "the second site_a".
    n = 2
    while not _free(f"{base}_{n}"):
        n += 1
        if n > 10000:                      # pathological; refuse rather than spin
            raise HTTPException(409, {
                "code": ERROR_CODES["ORTHO_COUNT_EXCEEDED"],
                "message": f"Too many orthomosaics named '{base}' in this project.",
                "project_id": project_id,
                "hint": "rename the file before uploading",
            })
    return f"{base}_{n}"


def _project_usage(db: Session, project_id: str) -> tuple[int, int]:
    """(bytes stored, ortho count) for a project. Two indexed queries.

    Rows with a NULL ``size_bytes`` count as 0 — the column is only NULL for
    rows written before it existed, and there is no cheap way to recover the
    number for them.
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
    """Admission check for a NEW upload. Call inside ``_dataset_edit_lock``.

    Semantics (plan v2 §7.1), deliberately: an upload already in flight is
    allowed to COMPLETE, and any FURTHER upload is rejected. So the predicate is
    ``used >= limit`` against what is already committed — NOT
    ``used + incoming > limit``, which would abort the in-flight upload that
    crosses the line. The quota can therefore be overshot by at most one ortho,
    bounded by ``max_upload_mb``.

    A limit of 0 (or below) disables that check.
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
    """Seconds one orthomosaic transfer may take, or None when disabled.

    One budget covers both paths on purpose. From the user's side "the upload
    took too long" is the same event whether the bytes came from their browser
    or from Drive, and two knobs to keep in sync would be one knob too many.
    """
    mins = int(getattr(settings, "ortho_transfer_timeout_min", 0) or 0)
    return mins * 60.0 if mins > 0 else None


def _transfer_timeout(project_id: str, what: str, waited_s: float):
    """The 408 both paths raise. Same code, same shape, either origin."""
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
    """Every ortho reference held by a run that has produced results.

    Sources, in the order they were added to the schema:
      * archived runs — ``project.runs[i]`` carries ``ortho_id`` / ``ortho_stem``
        for runs archived after this change and ``ortho`` (a filename) for older
        ones, so all three shapes are matched;
      * the CURRENT run, once it has reached a state whose artifacts exist
        (AWAITING_LABELS / LABELS_SUBMITTED / COMPLETED). FAILED is excluded on
        purpose: a failed run produced nothing worth protecting, and refusing to
        delete the ortho that failed would be the exact opposite of helpful.

    Returned as an untyped set of strings so a caller can test an ortho's id,
    stem and filename against it without caring which one a given run recorded.
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
        # A run launched before ortho selection existed recorded nothing; on a
        # single-ortho project that run can only have used the one ortho.
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

    # Runs under _dataset_edit_lock, so the project is in the transient
    # UPLOADING state and this release is what publishes the result. Conditional
    # so it can never overwrite a state someone else legitimately set.
    #
    # A project whose current run has already produced results goes back to
    # EXACTLY where it was, not to UPLOADED. That state is the only marker that
    # work/run_<n> is used: _apply_run_config (runs.py) archives the run and
    # bumps current_run only when the pre-analyze state is in USED_RUN_STATES.
    # Stamping UPLOADED here would erase it, so the next analyze would reuse the
    # same run folder and the worker's reset_dirs would wipe the finished run's
    # step1-4 outputs — the run also never reaching project.runs. Adding an
    # orthomosaic must leave the run already in the project untouched; that is
    # the whole premise of _assert_ortho_add_allowed dropping the old freeze.
    #
    # It also keeps `in_use` honest: _run_ortho_refs reads the current run's
    # ortho only while the project sits in one of those states.
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
    """Apply PipelineParams' cross-field rules to the params as they will be
    STORED, not just to the incoming overrides.

    Both the project-update path and the analyze trigger merge overrides onto
    the project's existing params, so per-key validation is not enough: sending
    only ``area_min: 5000`` passes every individual bound while still producing
    an invalid pair against a stored ``area_max`` of 2000. Called from both
    write paths so a bad combination can never be persisted.

    Unknown keys are dropped before validating, so legacy or operator-only
    params already on the project cannot break the check.
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
    """Stream an upload to disk, bounded by size AND by wall-clock time.

    ``max_bytes`` aborts as soon as the body exceeds it, so an oversized upload
    can never be fully written.

    The time budget is checked once per chunk. At 1 MiB
    chunks that is a fine enough grain to stop within a second of the deadline
    on any connection fast enough to matter — and on a connection so slow that
    a single chunk read blocks past the deadline, the check still fires on the
    read that returns. Either way the partial file is deleted: a half-written
    GeoTIFF on disk is worse than no file, because the next thing to open it
    would be rasterio.
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
        except Exception:                     # noqa: BLE001 - already failing
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
    """Best-effort raster metadata; empty dict if rasterio is unavailable."""
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
    """Best-effort EPSG code from the stored CRS string (e.g. 'EPSG:32643').

    Read from the string already on the row rather than re-opening the GeoTIFF,
    so listing a library of ten orthos costs no raster I/O. Returns None for a
    WKT-only CRS — the UI just shows the raw string in that case.
    """
    m = _EPSG_RX.search(crs or "")
    return int(m.group(1)) if m else None


def _safe_member_path(name: str) -> str | None:
    """Return a sanitized ``<species>/<file>`` path for a zip member, or None if
    unsafe. Strips drive letters and leading separators, rejects ``..`` and
    absolute paths, flattens deep nesting to ``<species>/<file>``, and limits
    each component to a safe charset."""
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
    """Validate a ground-truth zip and extract only safe ``*.tif`` members.

    Defends against zip bombs (member-count, total-size and per-member
    compression-ratio caps) and hostile paths (traversal, absolute paths,
    symlinks/devices). Returns the number of files written.
    """
    infos = z.infolist()
    if len(infos) > _GT_MAX_MEMBERS:
        raise HTTPException(400, {"code": "BAD_ARCHIVE", "message": f"Archive has too many entries (> {_GT_MAX_MEMBERS})", "hint": "split the ground truth into smaller zips and upload them separately"})

    # Pre-flight on the headers: catch obvious bombs before writing a single byte.
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

    # Set-semantics: drop any previous ground truth, then recreate the folder.
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(dest, exist_ok=True)

    written_total = 0
    count = 0
    for info in infos:
        if info.is_dir():
            continue
        # Skip anything that isn't a regular file (symlink/device/fifo). A mode of
        # 0 (common for Windows-made zips) is treated as a regular file.
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
        # Stream-copy enforcing REAL byte counts (never trust the header).
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
