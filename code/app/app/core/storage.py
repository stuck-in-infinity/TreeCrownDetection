"""Per-project artifact storage layout.

Maps the pipeline's hard-coded ``Config`` directories onto a single isolated
tree per project so concurrent jobs never collide. Local filesystem for now;
swap the body of these helpers for an S3/MinIO backend later without touching
the rest of the app.

**Run versioning.** Inputs (the uploaded ortho + ground truth) are shared across
runs and live at the project level. The derived/compute directories are scoped
to a *run*, under ``work/run_<n>/...``. Re-running with new parameters (see
``PATCH /projects/{id}``) bumps ``project.current_run`` and computes into a fresh
``run_<n+1>`` folder, so previous runs are preserved on disk rather than wiped.
"""
import os
import shutil

from app.core.settings import settings


def project_root(project_id: str) -> str:
    return os.path.join(settings.storage_root, "projects", project_id)


def relative_artifact_path(path: str) -> str:
    """Return a portable path relative to the configured storage root."""
    try:
        return os.path.relpath(path, settings.storage_root).replace(os.sep, "/")
    except ValueError:
        return path.replace(os.sep, "/")


def run_dir(project_id: str, run: int) -> str:
    return os.path.join(project_root(project_id), "work", f"run_{run}")


def project_paths(project_id: str, run: int = 1) -> dict:
    """All directories used for a project run (see API_DESIGN.md §5).

    ``input_ortho`` / ``input_gt`` are run-independent (shared across runs); the
    rest are scoped to ``work/run_<run>/``. The integer ``run`` is included for
    convenience and is skipped by the dir-creation helpers.
    """
    root = project_root(project_id)
    work = run_dir(project_id, run)
    return {
        "root": root,
        "input_ortho": os.path.join(root, "input", "ortho"),
        "input_gt": os.path.join(root, "input", "ground_truth"),
        "run": run,
        "work": work,                                       # == Config.WORKDIR
        "detectree": os.path.join(work, "detectree"),
        "ortho": os.path.join(work, "ortho"),               # == Config.ORTHO_FOLDER
        "polygons": os.path.join(work, "polygons"),         # == Config.POLY_FOLDER
        "step1_output": os.path.join(work, "step1_output"),
        "step2_output": os.path.join(work, "step2_output"),
        "step3_output": os.path.join(work, "step3_output"),
        "step4_output": os.path.join(work, "step4_output"),
        "logs": os.path.join(work, "logs"),
    }


def ensure_project_dirs(project_id: str, run: int = 1) -> dict:
    paths = project_paths(project_id, run)
    for key, p in paths.items():
        if key == "run":
            continue
        os.makedirs(p, exist_ok=True)
    return paths


# Keys that name SHARED, run-independent INPUT directories. These hold the
# uploaded orthomosaic library and the ground truth, which now persist across
# every run of a project — wiping one would silently destroy user data that no
# upload path can reconstruct. ``reset_dirs`` refuses them outright rather than
# relying on every future caller passing the right key list.
_PROTECTED_KEYS = frozenset({"root", "input_ortho", "input_gt"})


def reset_dirs(project_id: str, keys: list[str], run: int = 1) -> dict:
    """Remove and recreate the named work directories for a clean re-run.

    Operates only within the given run, so re-running a FAILED run can reset its
    own artifacts without touching sibling runs. Used at the start of a job so
    stale cached artifacts from a previous attempt (e.g. ``dinov2_features.npy``,
    ``tsne_coordinates.csv``, old clusters) can never leak into a fresh compute.

    Only run-scoped ``work/run_<n>/...`` directories may be reset. Note the
    similarly-named keys: ``"ortho"`` is ``work/run_<n>/ortho`` (the per-run copy
    the pipeline reads) while ``"input_ortho"`` is the project-level upload
    library — passing the latter raises ``ValueError`` instead of deleting every
    orthomosaic in the project.
    """
    bad = _PROTECTED_KEYS.intersection(keys)
    if bad:
        raise ValueError(
            f"reset_dirs refuses to wipe shared input directories: {sorted(bad)}"
        )
    paths = project_paths(project_id, run)
    for key in keys:
        d = paths[key]
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
        os.makedirs(d, exist_ok=True)
    return paths


def delete_project_dir(project_id: str) -> None:
    root = project_root(project_id)
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)


# Labelled (species) outputs produced after Step 1. Removed for consent=2
# ("unlabelled only") — everything through Step 1 (detectree/ortho/polygons/
# crowns/clustering) is kept.
_LABELLED_OUTPUT_KEYS = ["step2_output", "step3_output", "step4_output"]


def prune_labelled_outputs(project_id: str, run: int = 1) -> list[str]:
    """Delete only the labelled outputs (step2/3/4) of a run; keep everything
    through Step 1. Idempotent — re-running just removes whatever remains.
    Returns the list of directories removed (that existed)."""
    paths = project_paths(project_id, run)
    removed = []
    for key in _LABELLED_OUTPUT_KEYS:
        d = paths[key]
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed
