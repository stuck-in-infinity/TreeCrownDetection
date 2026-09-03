"""STAC Item emission for a completed pipeline run.

After ``job_b_finalize`` produces the KMZ + CSV outputs, we emit a SpatioTemporal
Asset Catalog (STAC) Item describing the run — a machine-readable, standards-based
manifest of the run's footprint, parameters and downloadable assets. It mirrors
``tree_crown_stac_item.example.yaml`` but is rendered as JSON (the canonical STAC
serialization) and filled with the run's real values.

The item is written to the run's ``step4_output/stac_item.json`` (next to the
KMZ) so it is preserved per run alongside the rest of that run's artifacts.

Asset/link hrefs point at the live result-download API endpoints (the same ones
``results.py`` exposes), so the manifest is usable as-is today. They are relative
by default; set ``TCP_PUBLIC_BASE_URL`` to emit absolute hrefs for a real STAC
catalog (e.g. ``https://api.example.com``).
"""
import csv
import json
import os
from datetime import datetime, timezone
# (stage-aware STAC ids + geojson footprint fallback added — see FEATURE_PLAN)

from app.core.models_registry import default_backbone
from app.core.settings import settings
from app.core.storage import project_paths, relative_artifact_path

# Human-readable descriptions for the columns of crown_master.csv. Any column not
# listed here still gets emitted, just with a generic description.
_COLUMN_DOCS = {
    "image_name": "Crown image filename",
    "polygon_id": "Crown polygon id",
    "site": "Orthomosaic stem",
    "cluster": "KMeans cluster id",
    "species": "Assigned species label",
    "true_species": "Ground-truth species label",
    "pred_species": "Predicted species label",
}

# STAC table-extension column types inferred from a sample value.
_INT_COLUMNS = {"polygon_id", "cluster"}


def _run(project) -> int:
    return getattr(project, "current_run", 1) or 1


def _href(rel_path: str) -> str:
    """Build an asset href. Relative by default; absolute if PUBLIC_BASE_URL set."""
    base = (getattr(settings, "public_base_url", "") or "").rstrip("/")
    return f"{base}{rel_path}" if base else rel_path


def _slug(text: str) -> str:
    keep = [c.lower() if c.isalnum() else "_" for c in (text or "")]
    s = "".join(keep).strip("_")
    while "__" in s:
        s = s.replace("__", "_")
    return s


def _footprint_from_geojson(geojson_path: str | None):
    """Fallback footprint: (geometry, bbox) in WGS84 from the crown GeoJSON.

    ``tree_crown_pipeline`` already reprojects the crown polygons to EPSG:4326
    (``gdf_all.to_crs(epsg=4326)``) before writing them, so the GeoJSON is
    WGS84. Used when the ortho GeoTIFF has no embedded CRS and the raster-based
    footprint could not be computed. Returns ``(None, None)`` on any failure so
    STAC emission never blocks finalize.
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
    geometry = {
        "type": "Polygon",
        "coordinates": [[
            [bbox[0], bbox[1]],
            [bbox[0], bbox[3]],
            [bbox[2], bbox[3]],
            [bbox[2], bbox[1]],
            [bbox[0], bbox[1]],
        ]],
    }
    return geometry, bbox


def _footprint_wgs84(ortho_dir: str):
    """Return (geometry, bbox) in WGS84 from the union of ortho footprints.

    Reads each GeoTIFF's bounds and reprojects to EPSG:4326. Returns
    ``(None, None)`` if rasterio is unavailable or no readable ortho is found, so
    STAC emission never blocks finalize.
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
    geometry = {
        "type": "Polygon",
        "coordinates": [[
            [bbox[0], bbox[1]],
            [bbox[0], bbox[3]],
            [bbox[2], bbox[3]],
            [bbox[2], bbox[1]],
            [bbox[0], bbox[1]],
        ]],
    }
    return geometry, bbox


def _table_columns(master_csv: str) -> list[dict]:
    """Build STAC table-extension columns from crown_master.csv's header."""
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
) -> dict:
    """Construct the STAC Item dict for the project's current run.

    ``stage`` (``"analyze"`` / ``"finalize"``) is appended to the item id so the
    two compute phases emit distinct, non-colliding STAC items. ``None`` keeps
    the legacy id (``..._run<n>``) for any caller that does not set a stage.
    """
    run = run or _run(project)
    paths = project_paths(project.id, run)
    params = dict(getattr(project, "params", None) or {})

    geojson = _first_geojson(paths["polygons"])
    master_csv = os.path.join(paths["step2_output"], "crown_master.csv")
    poly_csv = os.path.join(paths["step2_output"], "polygon_species.csv")
    kmz = os.path.join(paths["step4_output"], "species_map.kmz")
    cm_png = os.path.join(paths["step3_output"], "confusion_matrix.png")

    # Prefer the RUN's ortho folder (work/run_<n>/ortho): it holds exactly the
    # one orthomosaic this run processed, whereas input/ortho is now a library
    # of every ortho ever uploaded to the project and would union unrelated
    # footprints into this run's item. Falls back to the library for a run whose
    # working copy has been cleaned up.
    geometry, bbox = _footprint_wgs84(paths["ortho"])
    if bbox is None:
        geometry, bbox = _footprint_wgs84(paths["input_ortho"])
    if bbox is None:
        # Ortho lacked an embedded CRS / no readable raster — fall back to the
        # crown GeoJSON, which is already WGS84 (see _footprint_from_geojson).
        geometry, bbox = _footprint_from_geojson(geojson)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        "batch_size": params.get("batch_size", 64),  # match PipelineParams
        "img_size": params.get("img_size", 224),
        "model_name": params.get("model_name") or default_backbone(),
        "model_key": project.model_key,
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
        "project_id": project.id,
        "run": run,
        "detector_model": project.model_key,
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

    # Asset/link hrefs mirror the live result-download endpoints in results.py.
    results_base = "/api/v1/project/results"
    assets: dict[str, dict] = {}
    if geojson and os.path.exists(geojson):
        assets["data"] = {
            "href": relative_artifact_path(geojson),
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
    assets["style"] = {
        "href": "https://raw.githubusercontent.com/core-stack-org/QGIS-Styles/main/Land/LULC0_12class.qml",
        "type": "application/xml",
        "title": "QGIS Style file",
        "roles": ["metadata"],
    }

    item = {
        "type": "Feature",
        "stac_version": "1.1.0",
        "stac_extensions": [
            "https://stac-extensions.github.io/table/v1.2.0/schema.json"
        ],
        "id": item_id,
        "collection": "tree_crown_runs",
        "geometry": geometry,
        "properties": properties,
        "assets": assets,
        "links": [
            {
                "rel": "root",
                "href": "catalog.json",
                "type": "application/json",
                "title": "Tree Crown Spatio Temporal Asset Catalog",
            },
            {
                "rel": "collection",
                "href": "collection.json",
                "type": "application/json",
                "title": "tree_crown_runs",
            },
            {
                "rel": "parent",
                "href": "collection.json",
                "type": "application/json",
                "title": "tree_crown_runs",
            },
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
    """Filesystem path of the run's STAC item."""
    return os.path.join(
        project_paths(project.id, run or _run(project))["step4_output"], "stac_item.json"
    )


def _first_geojson(poly_dir: str) -> str | None:
    try:
        files = sorted(f for f in os.listdir(poly_dir) if f.lower().endswith(".geojson"))
    except OSError:
        return None
    return os.path.join(poly_dir, files[0]) if files else None


def write_stac_item(project, chosen_k: int | None = None) -> str:
    """Build and persist the STAC Item to the run's step4 output. Returns path.

    Written by job_b_finalize, so it is tagged ``stage="finalize"``.
    """
    item = build_stac_item(project, chosen_k=chosen_k, stage="finalize")
    out = stac_item_path(project)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as f:
        json.dump(item, f, indent=2)
    return out
