"""Per-project artifact storage layout.

Maps the pipeline's fixed ``Config`` directories onto one isolated tree per
project, so concurrent jobs cannot collide. Everything is on the local
filesystem; moving to S3 or MinIO means rewriting these helpers only.

Run versioning: the inputs (uploaded orthomosaics and ground truth) are shared
by every run and live at the project level, while the computed directories are
scoped to a run under ``work/run_<n>/``. Re-running with new parameters (see
``PATCH /projects/{id}``) increments ``project.current_run`` and computes into a
new ``run_<n+1>`` folder, so earlier runs stay on disk.
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
    """All directories used by one project run.

    ``input_ortho`` and ``input_gt`` are shared across runs; the rest live under
    ``work/run_<run>/``. The ``run`` entry is the run number itself, included
    for convenience, and the directory-creation helpers skip it.
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


# Keys naming the shared input directories. They hold the uploaded orthomosaic
# library and the ground truth, which persist across every run of a project, so
# deleting one would destroy data the server cannot rebuild. ``reset_dirs``
# rejects these keys rather than trusting each caller to leave them out.
_PROTECTED_KEYS = frozenset({"root", "input_ortho", "input_gt"})


def reset_dirs(project_id: str, keys: list[str], run: int = 1) -> dict:
    """Remove and recreate the named work directories for a clean re-run.

    Only touches the given run, so retrying a failed run resets its own
    artifacts and leaves other runs alone. Jobs call this on startup so leftover
    files from an earlier attempt (``dinov2_features.npy``,
    ``tsne_coordinates.csv``, old clusters) cannot be picked up as fresh output.

    Only directories under ``work/run_<n>/`` may be reset. Watch the two similar
    keys: ``"ortho"`` is ``work/run_<n>/ortho``, the per-run copy the pipeline
    reads, while ``"input_ortho"`` is the project's upload library. Passing the
    latter raises ``ValueError`` instead of deleting every orthomosaic.
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


# Species-labelled outputs, produced after Step 1. Consent value 2 ("unlabelled
# only") deletes these and keeps everything through Step 1: detectree, ortho,
# polygons, crowns and clustering.
_LABELLED_OUTPUT_KEYS = ["step2_output", "step3_output", "step4_output"]


def prune_labelled_outputs(project_id: str, run: int = 1) -> list[str]:
    """Delete a run's step2/3/4 outputs and keep everything through Step 1.

    Safe to call more than once; a second call removes whatever is left.
    Returns the directories that existed and were removed.
    """
    paths = project_paths(project_id, run)
    removed = []
    for key in _LABELLED_OUTPUT_KEYS:
        d = paths[key]
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(d)
    return removed
