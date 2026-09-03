import os

from app.core.storage import project_paths, relative_artifact_path
from app.services.stac import build_stac_item


HOSTING_PLATFORM = os.getenv("TCP_HOSTING_PLATFORM", "act4dws4")
METHODOLOGY_URL = "https://github.com/SaharshLaud/STACD_framework/blob/dev/README.md"


def run_version(project, run: int | None = None) -> int:
    return run or getattr(project, "current_run", 1) or 1


def analyze_asset_id(project, run: int | None = None) -> str:
    """Return the crown-polygon GeoJSON path produced by analyze."""
    p = project_paths(project.id, run_version(project, run))
    poly = p["polygons"]
    try:
        gj = sorted(f for f in os.listdir(poly) if f.lower().endswith(".geojson"))
        if gj:
            return os.path.join(poly, gj[0])
    except OSError:
        pass
    return p["step1_output"]


def analyze_asset_fields(project, run: int | None = None) -> dict:
    version = run_version(project, run)
    asset_id = analyze_asset_id(project, version)
    return asset_response_fields(project, asset_id, version, stage="analyze")


def asset_response_fields(
    project, asset_id: str, run: int | None = None, stage: str | None = None
) -> dict:
    version = run_version(project, run)
    portable_asset_id = _portable_asset_id(asset_id)
    return {
        "project_id": project.id,
        "asset_id": portable_asset_id,
        "asset_ids": [portable_asset_id],
        "version": str(version),
        "hosting_platform": HOSTING_PLATFORM,
        "stac": stac_response(project, portable_asset_id, version, stage=stage),
    }


def stac_response(
    project, asset_id: str, run: int | None = None, stage: str | None = None
) -> dict:
    version = run_version(project, run)
    item = build_stac_item(project, run=version, stage=stage)
    props = item.setdefault("properties", {})
    props["project_id"] = project.id
    props["run"] = version
    props["methodology_url"] = METHODOLOGY_URL
    props["asset_id"] = asset_id
    return item


def _portable_asset_id(path: str) -> str:
    if os.path.isabs(path):
        return relative_artifact_path(path)
    return path.replace(os.sep, "/")
