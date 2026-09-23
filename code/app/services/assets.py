import os

from app.core.storage import (
    first_polygon_geojson,
    project_paths,
    relative_artifact_path,
)
from app.services.stac import build_stac_item, read_stac_item


HOSTING_PLATFORM = os.getenv("TCP_HOSTING_PLATFORM", "act4dws4")
METHODOLOGY_URL = "https://github.com/SaharshLaud/STACD_framework/blob/dev/README.md"


def run_version(project, run: int | None = None) -> int:
    return run or getattr(project, "current_run", 1) or 1


def analyze_asset_id(project, run: int | None = None) -> str:
    """Return the path to the crown-polygon GeoJSON that analyze produced."""
    version = run_version(project, run)
    found = first_polygon_geojson(project.id, version)
    if found:
        return found
    return project_paths(project.id, version)["step1_output"]


def analyze_asset_fields(project, run: int | None = None) -> dict:
    version = run_version(project, run)
    asset_id = analyze_asset_id(project, version)
    return asset_response_fields(project, asset_id, version, stage="analyze")


def asset_response_fields(
    project, asset_id: str, run: int | None = None, stage: str | None = None
) -> dict:
    version = run_version(project, run)
    portable_asset_id = _portable_asset_id(asset_id)
    stac = stac_response(project, portable_asset_id, version, stage=stage)
    return {
        "project_id": project.id,
        "asset_id": portable_asset_id,
        "asset_ids": [portable_asset_id],
        "version": str(version),
        "hosting_platform": HOSTING_PLATFORM,
        "stac": stac,
        "stac_spec": stac,
    }


def stac_response(
    project, asset_id: str, run: int | None = None, stage: str | None = None
) -> dict:
    """The run's STAC item, as served inline beside the asset fields.

    A finalized run already has one on disk, written with the chosen k and the
    finalize stage in its id. Rebuilding here instead would answer with a
    different id and a null ``chosen_k`` for the same run, because the callers
    that ask for a results payload pass ``stage="analyze"`` and know nothing
    about k. So the file wins wherever it exists.
    """
    version = run_version(project, run)
    item = read_stac_item(project, version) or build_stac_item(
        project, run=version, stage=stage
    )
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
