"""Model catalog, loaded from an external YAML manifest.

Two independent catalogs (see models.yaml):

* **detectors** — Detectree2/detectron2 ``.pth`` weight files, chosen per project
  via ``model_key`` (Step 0 crown detection). ``file`` is resolved against
  ``settings.models_dir``.
* **backbones** — ``timm`` DINOv2 feature extractors, chosen via
  ``params.model_name`` (Step 1B embedding extraction). Validated against an
  allowlist so a bad name fails at project-creation time instead of mid-pipeline.

The catalog lives in ``settings.models_manifest`` so it can be edited / mounted
as a Docker volume without rebuilding the image. If the manifest file is absent,
the built-in ``_FALLBACK`` below is used so the app still boots.
"""
import os
from functools import lru_cache

import yaml

from app.core.settings import settings

# Built-in fallback used only when the manifest file is missing. Mirrors the
# original hardcoded registry so behaviour is unchanged without a manifest.
_FALLBACK: dict = {
    "detectors": {
        "urban_cambridge": {"file": "urban_trees_Cambridge_20230630.pth", "default": True},
        "paracou": {"file": "220723_withParacouUAV.pth"},
        "randresize": {"file": "230103_randresize_full.pth"},
    },
    "backbones": [
        {"name": "vit_base_patch14_dinov2.lvd142m", "img_size": 224, "default": True},
    ],
}


@lru_cache
def _manifest() -> dict:
    path = settings.models_manifest
    if path and os.path.exists(path):
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        data.setdefault("detectors", {})
        data.setdefault("backbones", [])
        return data
    return _FALLBACK


# ── detectors (model_key) ────────────────────────────────────────────────
def _detectors() -> dict:
    return _manifest()["detectors"]


def _default_detector_key() -> str:
    for key, meta in _detectors().items():
        if meta.get("default"):
            return key
    if settings.default_model_key in _detectors():
        return settings.default_model_key
    return next(iter(_detectors()))


# Kept for backwards-compatibility with existing imports.
DEFAULT_MODEL_KEY = _default_detector_key()


def list_models() -> list[dict]:
    """Return the detector registry with availability + default flags for the UI."""
    default_key = _default_detector_key()
    out = []
    for key, meta in _detectors().items():
        fname = meta["file"]
        path = os.path.join(settings.models_dir, fname)
        out.append(
            {
                "key": key,
                "filename": fname,
                "description": meta.get("description", ""),
                "available": os.path.exists(path),
                "default": key == default_key,
            }
        )
    return out


def resolve_model_path(model_key: str | None) -> tuple[str, str]:
    """Return (resolved_key, absolute_path) for a model_key (or the default)."""
    detectors = _detectors()
    key = model_key or _default_detector_key()
    if key not in detectors:
        raise ValueError(
            f"Unknown model_key '{key}'. Valid keys: {sorted(detectors)}"
        )
    path = os.path.abspath(os.path.join(settings.models_dir, detectors[key]["file"]))
    return key, path


# ── backbones (model_name) ─────────────────────────────────────────────────
def _backbones() -> list[dict]:
    """Normalise manifest backbones (allow either plain strings or dict entries)."""
    norm = []
    for b in _manifest()["backbones"]:
        norm.append({"name": b} if isinstance(b, str) else dict(b))
    return norm


def default_backbone() -> str:
    bbs = _backbones()
    for b in bbs:
        if b.get("default"):
            return b["name"]
    return bbs[0]["name"] if bbs else "vit_base_patch14_dinov2.lvd142m"


def valid_backbone_names() -> set[str]:
    return {b["name"] for b in _backbones()}


def list_backbones() -> list[dict]:
    """Return the backbone registry with default flags for the UI."""
    default = default_backbone()
    return [
        {
            "name": b["name"],
            "description": b.get("description", ""),
            "img_size": b.get("img_size"),
            "default": b["name"] == default,
        }
        for b in _backbones()
    ]


def resolve_backbone(model_name: str | None) -> str:
    """Return a valid backbone name (or the default). Raises on unknown names."""
    name = model_name or default_backbone()
    valid = valid_backbone_names()
    if name not in valid:
        raise ValueError(
            f"Unknown model_name '{name}'. Valid feature-extractors: {sorted(valid)}"
        )
    return name
