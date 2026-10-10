"""Write a STAC Item describing a finished pipeline run.

Once ``job_b_finalize`` has produced the KMZ and CSV outputs, this module writes
a SpatioTemporal Asset Catalog (STAC) Item for the run: a standard,
machine-readable description of its footprint, parameters and downloadable
files. It follows ``tree_crown_stac_item.example.yaml``, but is written as JSON,
which is STAC's normal format, and filled in with the run's real values.

The item goes to the run's ``step4_output/stac_item.json``, next to the KMZ, so
each run keeps its own copy alongside its other outputs.

The asset and link hrefs point at the download endpoints in ``results.py``, so
the manifest works as it stands. They are relative by default; set
``TCP_PUBLIC_BASE_URL`` (for example ``https://api.example.com``) to write
absolute hrefs suitable for a real STAC catalog.
"""
import csv
import json
import os
from datetime import datetime, timezone

from app.core.models_registry import default_backbone
from app.core.settings import settings
from app.core.storage import first_polygon_geojson, project_paths

# Descriptions for the columns of crown_master.csv. A column that is not listed
# here is still written out, but with a generic description.
_COLUMN_DOCS = {
    "image_name": "Crown image filename",
    "polygon_id": "Crown polygon id",
    "site": "Orthomosaic stem",
    "cluster": "KMeans cluster id",
    "species": "Assigned species label",
    "true_species": "Ground-truth species label",
    "pred_species": "Predicted species label",
}

# Columns the STAC table extension should describe as integers. Every other
# column's type is guessed from a sample value.
_INT_COLUMNS = {"polygon_id", "cluster"}


def _run(project) -> int:
    return getattr(project, "current_run", 1) or 1


def _run_settings(project, run: int, run_row=None) -> tuple[dict, str | None]:
    """Return the params and detector model used by ``run``.

    The project only holds the settings of the newest run, so an older run
    reads them from its own ``Run`` row. If the row is missing or empty, the
    project's values are used.
    """
    if run_row is None:
        try:
            from sqlalchemy.orm import object_session

            from app.db import models

            db = object_session(project)
            if db is not None:
                run_row = (
                    db.query(models.Run)
                    .filter_by(project_id=project.id, number=run)
                    .one_or_none()
                )
        except Exception:  # noqa: BLE001 - project is not a DB row
            run_row = None

    params = dict(getattr(run_row, "params", None) or {})
    if not params:
        params = dict(getattr(project, "params", None) or {})
    model_key = getattr(run_row, "model_key", None) or project.model_key
    return params, model_key


def _href(rel_path: str) -> str:
    """Build an asset href: relative, or absolute if public_base_url is set."""
    base = (getattr(settings, "public_base_url", "") or "").rstrip("/")
    return f"{base}{rel_path}" if base else rel_path


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "_" for c in (text or "")]
    s = "".join(keep).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return s


def _ring(bbox: list) -> dict:
    """The bbox as a Polygon wound counter-clockwise.

    RFC 7946 asks for the right-hand rule on exterior rings, and a clockwise
    ring reads as a hole to the consumers that check.
    """
    return {
        "type": "Polygon",
        "coordinates": [[
            [bbox[0], bbox[1]],
            [bbox[2], bbox[1]],
            [bbox[2], bbox[3]],
            [bbox[0], bbox[3]],
            [bbox[0], bbox[1]],
        ]],
    }


def _footprint_from_geojson(geojson_path: str | None):
    """Work out a footprint from the crown GeoJSON. Returns (geometry, bbox).

    ``tree_crown_pipeline`` reprojects the crown polygons to EPSG:4326 before
    writing them, so the GeoJSON is already in WGS84. This is the fallback for
    when the orthomosaic GeoTIFF carries no CRS and the raster footprint could
    not be worked out. Any failure returns ``(None, None)``, so writing the STAC
    item never holds up finalize.
    """
    if not geojson_path or not os.path.exists(geojson_path):
        return None, None
    try:
        with open(geojson_path) as f:
            gj = json.load(f)
    except Exception:
        return None, None

    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    found = False

    def _walk(coords):
        nonlocal minx, miny, maxx, maxy, found
        if (
            isinstance(coords, (list, tuple))
            and len(coords) >= 2
            and isinstance(coords[0], (int, float))
            and isinstance(coords[1], (int, float))
        ):
            x, y = coords[0], coords[1]
            minx, miny = min(minx, x), min(miny, y)
            maxx, maxy = max(maxx, x), max(maxy, y)
            found = True
            return
        if isinstance(coords, (list, tuple)):
            for c in coords:
                _walk(c)

    feats = gj.get("features") if isinstance(gj, dict) else None
    if feats is None and isinstance(gj, dict) and gj.get("type") == "Feature":
        feats = [gj]
    for feat in feats or []:
        geom = (feat or {}).get("geometry") or {}
        _walk(geom.get("coordinates"))

    if not found:
        return None, None

    bbox = [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)]
    return _ring(bbox), bbox


def _footprint_wgs84(ortho_dir: str):
    """Return (geometry, bbox) in WGS84 covering every orthomosaic in a folder.

    Reads each GeoTIFF's bounds and reprojects them to EPSG:4326. Returns
    ``(None, None)`` if rasterio is not installed or no orthomosaic could be
    read, so writing the STAC item never holds up finalize.
    """
    try:
        import rasterio
        from rasterio.warp import transform_bounds
    except Exception:
        return None, None

    if not os.path.isdir(ortho_dir):
        return None, None

    minx = miny = float("inf")
    maxx = maxy = float("-inf")
    found = False
    for f in os.listdir(ortho_dir):
        if not f.lower().endswith((".tif", ".tiff")):
            continue
        path = os.path.join(ortho_dir, f)
        try:
            with rasterio.open(path) as src:
                if src.crs is None:
                    continue
                l, b, r, t = transform_bounds(
                    src.crs, "EPSG:4326", *src.bounds, densify_pts=21
                )
        except Exception:
            continue
        minx, miny = min(minx, l), min(miny, b)
        maxx, maxy = max(maxx, r), max(maxy, t)
        found = True

    if not found:
        return None, None

    bbox = [round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)]
    return _ring(bbox), bbox


def _run_datetime(candidates: list) -> str:
    """When the run produced its outputs, as a UTC timestamp.

    Taken from the mtime of the first output that exists, newest artifact
    first, so the same run always reports the same instant. Reading the clock
    instead gave every call a different datetime, which made two items for one
    run look like two observations.

    Falls back to now for a run with nothing on disk yet.
    """
    for path in candidates:
        if path and os.path.exists(path):
            stamp = datetime.fromtimestamp(os.path.getmtime(path), timezone.utc)
            return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_columns(master_csv: str) -> list[dict]:
    """Describe crown_master.csv's columns for the STAC table extension."""
    if not os.path.exists(master_csv):
        return []
    try:
        with open(master_csv, newline="") as f:
            header = next(csv.reader(f), [])
    except Exception:
        return []
    cols = []
    for name in header:
        cols.append({
            "name": name,
            "type": "int64" if name in _INT_COLUMNS else "string",
            "description": _COLUMN_DOCS.get(name, f"Column {name}"),
        })
    return cols


def build_stac_item(
    project,
    chosen_k: int | None = None,
    run: int | None = None,
    stage: str | None = None,
    run_row=None,
) -> dict:
    """Build the STAC Item for ``run`` (default: the project's current run).

    ``stage``, either ``"analyze"`` or ``"finalize"``, is added to the item id so
    the two compute phases produce different ids. Leaving it as ``None`` gives
    the older ``..._run<n>`` id, for callers that do not set a stage.

    ``run_row`` is the run's ``Run`` row. It is looked up if not given.
    """
    run = run or _run(project)
    paths = project_paths(project.id, run)
    params, model_key = _run_settings(project, run, run_row)

    geojson = first_polygon_geojson(project.id, run)
    master_csv = os.path.join(paths["step2_output"], "crown_master.csv")
    poly_csv = os.path.join(paths["step2_output"], "polygon_species.csv")
    kmz = os.path.join(paths["step4_output"], "species_map.kmz")
    cm_png = os.path.join(paths["step3_output"], "confusion_matrix.png")

    # Use the run's own ortho folder, work/run_<n>/ortho, first: it holds just
    # the one orthomosaic this run processed. input/ortho is the project's whole
    # library, so reading it would pull unrelated footprints into this item. The
    # library is only used when the run's working copy has been cleaned up.
    geometry, bbox = _footprint_wgs84(paths["ortho"])
    if bbox is None:
        geometry, bbox = _footprint_wgs84(paths["input_ortho"])
    if bbox is None:
        # No orthomosaic carried a CRS, or none could be read. Fall back to the
        # crown GeoJSON, which is already in WGS84.
        geometry, bbox = _footprint_from_geojson(geojson)

    now = _run_datetime([kmz, geojson, master_csv])
    item_id = f"{_slug(project.name) or 'tree_crown'}_{project.id[:8]}_run{run}"
    if stage:
        item_id += f"_{stage}"

    input_parameters = {
        "tile_size": params.get("tile_size", 10),
        "buffer": params.get("buffer", 10),
        "iou_threshold": params.get("iou_threshold", 0.9),
        "conf_threshold": params.get("conf_threshold", 0.85),
        "detections_per_image": params.get("detections_per_image", 6),
        "min_size_test": params.get("min_size_test", 512),
        "area_min": params.get("area_min", 4),
        "area_max": params.get("area_max", 2000),
        "full_coverage": params.get("full_coverage", False),
        "k_list": params.get("k_list", [2, 4, 6, 8, 10]),
        "pca_components": params.get("pca_components", 50),
        "batch_size": params.get("batch_size", 64),  # same default as PipelineParams
        "img_size": params.get("img_size", 224),
        "model_name": params.get("model_name") or default_backbone(),
        "model_key": model_key,
        "source_epsg": getattr(project, "source_epsg", None) or 32643,
        "chosen_k": chosen_k or params.get("chosen_k"),
    }

    description = (
        "Tree crown vector and species classification output generated from a "
        "drone orthomosaic. The workflow detects individual tree crowns using "
        "Detectree2, converts crown predictions to GeoJSON polygons, extracts "
        "visual embeddings with DINOv2, clusters crowns with KMeans, supports "
        "human-in-the-loop cluster labelling, and exports per-crown species "
        "outputs for GIS and Google Earth review. The STAC item records the "
        "run configuration, model choices, output assets, and WGS84 footprint "
        "so the result can be indexed or consumed by downstream catalog systems."
    )

    properties = {
        "title": "Tree-Crown Species Map",
        "description": description,
        "start_datetime": now,
        "end_datetime": now,
        "datetime": now,
        "keywords": ["forestry", "tree-crown", "species", "drone", "orthomosaic"],
        "collection": "tree_crown_runs",
        "project_id": project.id,
        "run": run,
        "detector_model": model_key,
        "feature_extractor": params.get("model_name") or default_backbone(),
        "source_epsg": getattr(project, "source_epsg", None) or 32643,
        "chosen_k": chosen_k,
        "input_parameters": input_parameters,
        "table:columns": _table_columns(master_csv),
    }
    if bbox is not None:
        properties.update({
            "min_longitude": bbox[0],
            "min_latitude": bbox[1],
            "max_longitude": bbox[2],
            "max_latitude": bbox[3],
            "center_longitude": round((bbox[0] + bbox[2]) / 2, 6),
            "center_latitude": round((bbox[1] + bbox[3]) / 2, 6),
        })

    # These hrefs match the download endpoints in results.py, and they name both
    # the project and the run in the PATH.
    #
    # /project/runs/{run}/results/<asset> would 404 or answer for the wrong
    # project: with no project_id, get_project falls back to "the newest project
    # owned by the caller", and a catalog client sends no identity at all. The
    # /projects/{id}/... family takes the project from the path instead.
    #
    # The run number is in the path for the same reason the project id is: the
    # current-run routes answer for whichever run is live, so an older item
    # would hand out a newer run's files under the older run's metadata.
    #
    # Ownership is still enforced on the way in, so a client has to authenticate
    # as the owner when auth_enabled is on. The identity is deliberately not in
    # the URL: these hrefs end up in a catalog, and an email does not belong
    # there.
    results_base = f"/api/v1/projects/{project.id}/runs/{run}/results"
    assets: dict[str, dict] = {}
    if geojson and os.path.exists(geojson):
        assets["data"] = {
            "href": _href(f"{results_base}/polygons.geojson"),
            "type": "application/geo+json",
            "title": "Tree crown GeoJSON vector layer",
            "roles": ["data"],
        }
    if os.path.exists(kmz):
        assets["kmz"] = {
            "href": _href(f"{results_base}/kmz"),
            "type": "application/vnd.google-earth.kmz",
            "title": "Species map for Google Earth",
            "roles": ["data"],
        }
    if os.path.exists(master_csv):
        assets["crown_master"] = {
            "href": _href(f"{results_base}/crown-master.csv"),
            "type": "text/csv",
            "title": "Per-crown master table",
            "roles": ["data"],
        }
    if os.path.exists(poly_csv):
        assets["polygon_species"] = {
            "href": _href(f"{results_base}/polygon-species.csv"),
            "type": "text/csv",
            "title": "Polygon-to-species table",
            "roles": ["data"],
        }
    if os.path.exists(cm_png):
        assets["confusion_matrix"] = {
            "href": _href(f"{results_base}/confusion-matrix.png"),
            "type": "image/png",
            "title": "Validation confusion matrix",
            "roles": ["overview"],
        }
    item = {
        "type": "Feature",
        "stac_version": "1.1.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/table/v1.2.0/schema.json"
        ],
        "id": item_id,
        "geometry": geometry,
        "properties": properties,
        "assets": assets,
        # Only links that resolve. This API serves no catalog.json or
        # collection.json, so root, parent and collection links would point at
        # nothing, and an item that names a `collection` must link to it. The
        # collection name stays in properties instead.
        "links": [
            {
                "rel": "self",
                "href": _href(f"{results_base}/stac-item.json"),
                "type": "application/json",
            }
        ],
    }
    if bbox is not None:
        item["bbox"] = bbox
    return item


def stac_item_path(project, run: int | None = None) -> str:
    """Where the run's STAC item is written on disk."""
    return os.path.join(
        project_paths(project.id, run or _run(project))["step4_output"], "stac_item.json"
    )


def read_stac_item(project, run: int | None = None) -> dict | None:
    """The item finalize wrote for this run, or None if it wrote none.

    Callers that serve an item inline read it back rather than rebuilding, so
    what the API reports and what the run's folder holds cannot disagree.
    """
    path = stac_item_path(project, run)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            item = json.load(f)
    except (OSError, ValueError):
        return None
    return item if isinstance(item, dict) else None


def write_stac_item(
    project, chosen_k: int | None = None, run: int | None = None, run_row=None
) -> str:
    """Build the STAC Item, write it into the run's step4 output, return the path.

    job_b_finalize is what calls this, so the item is tagged
    ``stage="finalize"``.
    """
    item = build_stac_item(
        project, chosen_k=chosen_k, run=run, stage="finalize", run_row=run_row
    )
    out = stac_item_path(project, run)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(item, f, indent=2)
    return out
