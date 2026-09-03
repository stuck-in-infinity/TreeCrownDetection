"""Bridge between the web layer and the existing pipeline functions.

The pipeline (``predict.py`` / ``tree_crown_pipeline.py``) reads a plain config
object with UPPER_CASE attributes. ``build_config`` constructs one per project,
pointed at that project's storage directories and tuning params.
"""
import csv
import os
import re
import types

from app.core.models_registry import default_backbone, resolve_model_path
from app.core.storage import project_paths

# KML colours (AABBGGRR), copied from the pipeline's Config.
COLOR_PALETTE = [
    "990000ff", "9900ff00", "99ff0000", "9900ffff",
    "99ff00ff", "99ff8800", "9900ffff", "99ffffff",
]


def normalize_species(val) -> str:
    """Match the pipeline's normalisation: lower, spaces/hyphens -> underscore."""
    s = str(val or "").strip().lower()
    s = re.sub(r"[\s\-]+", "_", s)
    return s or "unlabelled"


def build_config(project) -> types.SimpleNamespace:
    """Return an attribute-bag config the pipeline functions accept."""
    p = project_paths(project.id, getattr(project, "current_run", 1) or 1)
    params = dict(project.params or {})
    _, model_path = resolve_model_path(project.model_key)

    cfg = types.SimpleNamespace()

    # detection (Step 0)
    cfg.DETECTREE_MODEL = model_path
    cfg.TILE_SIZE = params.get("tile_size", 10)
    cfg.BUFFER = params.get("buffer", 10)
    cfg.IOU_THRESHOLD = params.get("iou_threshold", 0.9)
    cfg.CONF_THRESHOLD = params.get("conf_threshold", 0.85)
    # Defaults below mirror predict.py's DEFAULT_* constants — keep in sync
    # with PipelineParams so an older project without these keys behaves
    # exactly as it did before they were exposed.
    cfg.DETECTIONS_PER_IMAGE = params.get("detections_per_image", 6)
    cfg.MIN_SIZE_TEST = params.get("min_size_test", 512)
    cfg.AREA_MIN = params.get("area_min", 4)
    cfg.AREA_MAX = params.get("area_max", 2000)
    cfg.FULL_COVERAGE = params.get("full_coverage", False)

    # folders
    cfg.WORKDIR = p["work"]
    cfg.ORTHO_FOLDER = p["ortho"]
    cfg.POLY_FOLDER = p["polygons"]
    cfg.STEP1_OUTPUT = p["step1_output"]
    cfg.STEP2_OUTPUT = p["step2_output"]
    cfg.STEP3_VALIDATION_OUTPUT = p["step3_output"]
    cfg.STEP4_OUTPUT = p["step4_output"]
    cfg.GROUND_TRUTH_CSV = p["input_gt"]   # step3_validate treats this as a folder

    # features + clustering (Step 1)
    cfg.MODEL_NAME = params.get("model_name") or default_backbone()
    cfg.IMG_SIZE = params.get("img_size", 224)
    cfg.BATCH_SIZE = params.get("batch_size", 64)  # keep in sync with PipelineParams
    cfg.PCA_COMPONENTS = params.get("pca_components", 50)
    cfg.K_LIST = params.get("k_list", [2, 4, 6, 8, 10])
    cfg.COPY_TO_CLUSTER_FOLDERS = True

    # species + export (Steps 2/4)
    cfg.CHOSEN_K = params.get("chosen_k", 2)
    cfg.SOURCE_EPSG = project.source_epsg or 32643
    cfg.COLOR_PALETTE = COLOR_PALETTE

    return cfg


def write_species_map_csv(
    project, chosen_k: int, mapping: dict[int, dict], run: int | None = None
) -> str:
    """Write the ``k{chosen_k}_cluster_species_map.csv`` that step2 reads.

    ``mapping`` maps cluster_id -> {"species": str, "notes": str}.
    Clusters missing from the mapping are written as 'unlabelled'.

    ``run`` pins the target run folder. Callers that already read
    ``current_run`` should pass it, so a concurrent re-analyze bumping the
    counter mid-request cannot redirect this file into the new run's folder.
    """
    if run is None:
        run = getattr(project, "current_run", 1) or 1
    clustering_dir = os.path.join(
        project_paths(project.id, run)["step1_output"],
        "clustering",
    )
    os.makedirs(clustering_dir, exist_ok=True)
    out = os.path.join(clustering_dir, f"k{chosen_k}_cluster_species_map.csv")
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["cluster", "cluster_folder", "species", "notes"])
        for cid in range(chosen_k):
            m = mapping.get(cid, {})
            w.writerow(
                [cid, f"cluster_{cid}", normalize_species(m.get("species", "")),
                 m.get("notes", "") or ""]
            )
    return out
