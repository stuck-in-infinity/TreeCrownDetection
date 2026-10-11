"""Public gallery: decides what a visitor without sign-in can see.

Items come from two places:
  * runs whose owner gave consent (Run.consent 1 or 2)
  * folders in the starred directory, added by an admin (see starred/README.md)

Each item has a tier:
  crowns  orthomosaic picture, crown outlines
  all     the above + species map, KMZ, crown table, STAC item, files link

The tier is checked here for every file, so a "crowns" item can never give
out a species file. A bad starred folder is skipped.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.core.logging import get_logger, naive_now
from app.core.settings import settings
from app.core.storage import run_dir
from app.db import models

import map_images

log = get_logger("app.gallery")

TIER_ALL = "all"
TIER_CROWNS = "crowns"

# Starred items get the id "s-<folder name>". Runs use their uuid.
STARRED_PREFIX = "s-"
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

# Where to look for each file inside an item's folder. The simple name is
# tried first (starred folder), then the path used inside a run folder.
_CANDIDATES = {
    "overlay": ("overlay.png", "overlay.jpg", "overlay.jpeg",
                "detectree/*/overlay.png"),
    "species_map": ("species_map.png", "species_map.jpg", "species_map.jpeg",
                    "step4_output/species_map.png"),
    "ortho_image": ("ortho.jpg", "ortho.jpeg", "ortho.png"),
    "raster": ("detectree/*/downsampled.tif", "ortho/*.tif", "ortho/*.tiff",
               "ortho.tif", "ortho.tiff"),
    # Full-resolution orthomosaic, used only to read the pixel size.
    "raster_native": ("ortho/*.tif", "ortho/*.tiff", "ortho.tif", "ortho.tiff"),
    "crowns": ("crowns.geojson", "polygons/*.geojson",
               "detectree/*/tree_crowns.geojson"),
    "crown_master": ("crown_master.csv", "step2_output/crown_master.csv"),
    "kmz": ("species_map.kmz", "step4_output/species_map.kmz"),
    "stac": ("stac_item.json", "step4_output/stac_item.json"),
}

# Pictures a visitor can view: kind -> (label, tier needed).
IMAGE_KINDS = {
    "ortho": ("Orthomosaic", TIER_CROWNS),
    "overlay": ("Crown outlines", TIER_CROWNS),
    "species_map": ("Species map", TIER_ALL),
}
IMAGE_SIZES = {"thumb": 640, "preview": 1600}

# Downloads: file name -> (key above, label, media type, tier needed).
DOWNLOADS = {
    "crowns.geojson": ("crowns", "Crown outlines (GeoJSON)",
                       "application/geo+json", TIER_CROWNS),
    "species_map.kmz": ("kmz", "Species map (KMZ)",
                        "application/vnd.google-earth.kmz", TIER_ALL),
    "crown_master.csv": ("crown_master", "Crown table (CSV)", "text/csv", TIER_ALL),
    "stac_item.json": ("stac", "Catalogue record (STAC)", "application/json", TIER_ALL),
}


@dataclass
class Item:
    id: str
    dir: str
    tier: str
    starred: bool
    title: str
    place: str | None = None
    date: str | None = None            # YYYY-MM-DD
    summary: dict = field(default_factory=dict)
    files_url: str | None = None
    how: list[str] = field(default_factory=list)
    description: str | None = None
    source_run_id: str | None = None   # starred only: run id from meta.json
    source_run: tuple | None = None    # starred only: (project_id, run) from stac_item.json
    project_id: str | None = None      # runs only
    run_number: int | None = None      # runs only

    def allows(self, tier: str) -> bool:
        return tier == TIER_CROWNS or self.tier == TIER_ALL


def find(item_dir: str, key: str) -> str | None:
    """Return the first file that exists for ``key``, or None."""
    for pattern in _CANDIDATES[key]:
        if "*" in pattern:
            hits = sorted(h for h in glob.glob(os.path.join(item_dir, pattern))
                          if os.path.isfile(h))
            if hits:
                return hits[0]
        else:
            path = os.path.join(item_dir, pattern)
            if os.path.isfile(path):
                return path
    return None


def asset_path(item: Item, name: str) -> tuple[str, str] | None:
    """Return (path, media type) for a download, or None.

    None means: unknown name, not allowed for this tier, or file missing.
    """
    spec = DOWNLOADS.get(name)
    if spec is None:
        return None
    key, _label, media, tier = spec
    if not item.allows(tier):
        return None
    path = find(item.dir, key)
    return (path, media) if path else None


def image_kinds(item: Item) -> list[str]:
    """List the pictures this item can show."""
    out = []
    if find(item.dir, "ortho_image") or find(item.dir, "raster"):
        out.append("ortho")
    if find(item.dir, "overlay"):
        out.append("overlay")
    if item.allows(TIER_ALL) and (
            find(item.dir, "species_map") or _species_map_drawable(item)):
        out.append("species_map")
    return out


def _species_map_drawable(item: Item) -> bool:
    # Older runs have no species map file, but it can be drawn from their data.
    return (not item.starred
            and find(item.dir, "crown_master") is not None
            and find(item.dir, "crowns") is not None
            and find(item.dir, "raster") is not None)


def _cache_dir(item: Item) -> str:
    # Small JPEG copies are kept here, not in the item's own folder.
    return os.path.join(settings.storage_root, "gallery_cache", item.id)


def image_path(item: Item, kind: str, size: str) -> str | None:
    """Return a JPEG of the picture at the asked size, or None.

    The JPEG is made on the first request and reused after that.
    """
    spec = IMAGE_KINDS.get(kind)
    side = IMAGE_SIZES.get(size)
    if spec is None or side is None or not item.allows(spec[1]):
        return None
    dst = os.path.join(_cache_dir(item), f"{kind}_{side}.jpg")

    if kind == "ortho":
        src = find(item.dir, "ortho_image")
        if src is None:
            raster = find(item.dir, "raster")
            if raster and map_images.write_raster_preview(raster, dst, side):
                return dst
            return None
    else:
        src = find(item.dir, kind)
        if src is None:
            return None
    if map_images.write_preview(src, dst, side):
        return dst
    # Could not make the small copy. Send the original only if it is a JPEG.
    return src if src.lower().endswith((".jpg", ".jpeg")) else None


def _count_crowns(path: str | None) -> int | None:
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return len(json.load(f).get("features") or [])
    except Exception:                    # noqa: BLE001
        return None


def _species_counts(path: str | None) -> dict[str, int] | None:
    if not path:
        return None
    counts: dict[str, int] = {}
    try:
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sp = (row.get("species") or "unlabelled").strip() or "unlabelled"
                counts[sp] = counts.get(sp, 0) + 1
    except Exception:                    # noqa: BLE001
        return None
    return counts or None


def _raster_facts(item_dir: str) -> dict:
    """Read the surveyed area (km2) and pixel size (cm) from the rasters.

    Area counts only the part of the image that has data. Pixel size is read
    from the full-resolution orthomosaic only.
    """
    out: dict = {}
    try:
        import math

        import rasterio

        def metres_per_pixel(src):
            rx, ry = abs(src.transform.a), abs(src.transform.e)
            if src.crs is not None and src.crs.is_geographic:
                lat = (src.bounds.top + src.bounds.bottom) / 2.0
                return (rx * 111_320.0 * math.cos(math.radians(lat)), ry * 110_540.0)
            return rx, ry

        base = find(item_dir, "raster")
        if base:
            with rasterio.open(base) as src:
                mx, my = metres_per_pixel(src)
                scale = min(1.0, 512 / max(src.width, src.height))
                shape = (max(1, int(src.height * scale)), max(1, int(src.width * scale)))
                valid = float((src.dataset_mask(out_shape=shape) > 0).mean())
                area_m2 = src.width * src.height * mx * my * valid
            if area_m2 > 0:
                # Plots are small, so keep 4 decimals.
                out["area_km2"] = round(area_m2 / 1_000_000.0, 4)
        native = find(item_dir, "raster_native")
        if native:
            with rasterio.open(native) as src:
                mx, my = metres_per_pixel(src)
            if mx > 0:
                out["gsd_cm"] = round((mx + my) / 2.0 * 100.0, 2)
    except Exception:                    # noqa: BLE001
        log.warning("could not read raster facts in %s", item_dir, exc_info=True)
    return out


def build_summary(item_dir: str, tier: str) -> dict:
    """Read the numbers shown on a card: crowns, area, pixel size, species.

    Species counts are read only for the "all" tier.
    """
    summary: dict = {"crowns": _count_crowns(find(item_dir, "crowns"))}
    summary.update(_raster_facts(item_dir))
    if tier == TIER_ALL:
        species = _species_counts(find(item_dir, "crown_master"))
        if species:
            summary["species"] = species
            if summary["crowns"] is None:
                summary["crowns"] = sum(species.values())
    return summary


def species_rows(summary: dict) -> list[dict]:
    """Species list, biggest first, with the same colours as the map."""
    counts = summary.get("species") or {}
    if not counts:
        return []
    from app.services.pipeline_adapter import COLOR_PALETTE

    colors = map_images.species_colors(counts.keys(), COLOR_PALETTE)
    rows = []
    for name, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        r, g, b, _a = colors[name]
        rows.append({
            "name": name.replace("_", " "),
            "count": int(n),
            "color": "#%02x%02x%02x" % (round(r * 255), round(g * 255), round(b * 255)),
        })
    return rows


def _tier_of(consent: int) -> str | None:
    return {1: TIER_ALL, 2: TIER_CROWNS}.get(consent or 0)


def _run_how(run) -> list[str]:
    params = dict(run.params or {})
    how = []
    if run.model_key:
        how.append(f"Detectree2 weights {run.model_key}")
    if params.get("tile_size") is not None:
        how.append(f"tile {params['tile_size']} m")
    if params.get("conf_threshold") is not None:
        how.append(f"confidence {params['conf_threshold']}")
    if params.get("backbone"):
        how.append(f"features {params['backbone']}")
    return how


def _run_files_url(run) -> str | None:
    if not run.share_hash:
        return None
    try:
        from app.services.filebrowser_client import filebrowser_enabled, share_url
        return share_url(run.share_hash) if filebrowser_enabled() else None
    except Exception:                    # noqa: BLE001
        return None


def _area_in_km2(summary: dict) -> dict:
    """Convert old summaries that stored the area in hectares."""
    if "area_km2" not in summary and summary.get("area_ha") is not None:
        summary["area_km2"] = round(float(summary["area_ha"]) / 100.0, 4)
    summary.pop("area_ha", None)
    return summary


def item_from_run(run) -> Item | None:
    tier = _tier_of(run.consent)
    if tier is None or run.public_at is None:
        return None
    summary = _area_in_km2(dict(run.public_summary or {}))
    if tier != TIER_ALL:
        summary.pop("species", None)
    how = _run_how(run)
    if tier == TIER_ALL and run.chosen_k:
        how.append(f"{run.chosen_k} groups, named by the owner")
    when = run.finished_at or run.public_at
    return Item(
        id=run.id,
        dir=run_dir(run.project_id, run.number),
        tier=tier,
        starred=False,
        title=(run.public_title or "").strip() or "Untitled survey",
        place=(run.public_place or "").strip() or None,
        date=when.date().isoformat() if when else None,
        summary=summary,
        files_url=_run_files_url(run) if tier == TIER_ALL else None,
        how=how,
        project_id=run.project_id,
        run_number=run.number,
    )


def _clean(text, limit: int) -> str | None:
    text = " ".join(str(text or "").split())
    return text[:limit] or None


def most_permissive(consents) -> int:
    """Pick one consent value for the whole project from its runs.

    Used by retention: 1 (all) wins over 2 (unlabelled), which wins over 0.
    """
    seen = set(consents)
    return 1 if 1 in seen else 2 if 2 in seen else 0


def publish_run(db, project, run, consent: int, title=None, place=None) -> None:
    """Save a run's consent and add it to (or remove it from) the gallery.

    A run without a detection overlay is not listed.
    """
    now = naive_now()
    run.consent = consent
    run.consent_at = now
    run.public_title = _clean(title, 80)
    run.public_place = _clean(place, 80)

    tier = _tier_of(consent)
    base = run_dir(project.id, run.number)
    if tier is None:
        run.public_at = None
        run.public_summary = None
    elif find(base, "overlay") is None:
        log.warning("run %s of project %s consented but has no overlay; not listed",
                    run.number, project.id)
        run.public_at = None
        run.public_summary = None
    else:
        run.public_summary = build_summary(base, tier)
        run.public_at = now
        if tier == TIER_ALL:
            ensure_run_share(project, run)

    db.add(run)
    db.flush()
    rows = db.query(models.Run.consent).filter_by(project_id=project.id).all()
    project.consent = most_permissive(c for (c,) in rows)
    project.consent_at = now
    db.add(project)
    db.commit()


def ensure_run_share(project, run) -> None:
    """Create a FileBrowser share for this run's folder, if it has none.

    If it fails, the run is still listed, just without the files link.
    """
    if run.share_hash:
        return
    try:
        from app.services.filebrowser_client import create_run_share, filebrowser_enabled
        if filebrowser_enabled():
            run.share_hash = create_run_share(project.id, run.number)
    except Exception as exc:             # noqa: BLE001
        log.warning("no FileBrowser share for run %s of project %s: %s",
                    run.number, project.id, exc)


def backfill_run_consent(db) -> dict:
    """Move old project-level consent onto the right run. Runs at start-up.

    The answer goes to the last run that finished before the consent was
    given. Projects already done are skipped, so it is safe to run again.
    Also retries the FileBrowser share for runs that are missing one.
    """
    moved, shares = 0, 0
    try:
        projects = (db.query(models.Project)
                    .filter(models.Project.consent.in_((1, 2)),
                            models.Project.consent_at.isnot(None)).all())
        for project in projects:
            runs = db.query(models.Run).filter_by(project_id=project.id).all()
            if any(r.consent_at is not None for r in runs):
                continue
            done = [r for r in runs if r.state == "COMPLETED"]
            if not done:
                continue
            before = [r for r in done
                      if r.finished_at and r.finished_at <= project.consent_at]
            target = max(before or done,
                         key=lambda r: (r.finished_at or r.created_at, r.number))
            answered_at = project.consent_at
            publish_run(db, project, target, project.consent, title=project.name)
            # Keep the original consent time.
            target.consent_at = answered_at
            project.consent_at = answered_at
            if target.public_at is not None:
                target.public_at = answered_at
            db.commit()
            moved += 1

        missing = (db.query(models.Run)
                   .filter(models.Run.consent == 1,
                           models.Run.public_at.isnot(None),
                           models.Run.share_hash.is_(None)).all())
        for run in missing:
            ensure_run_share(run.project, run)
            if run.share_hash:
                db.add(run)
                shares += 1
        db.commit()
    except Exception:                    # noqa: BLE001
        log.exception("could not backfill run consent")
        try:
            db.rollback()
        except Exception:                # noqa: BLE001
            pass
    if moved or shares:
        log.info("run consent backfill: %d project answers moved, %d shares created",
                 moved, shares)
    return {"moved": moved, "shares": shares}


def starred_root() -> str:
    if (settings.starred_dir or "").strip():
        return settings.starred_dir.strip()
    # Default: /starred in the container, <repo>/starred otherwise.
    return str(Path(__file__).resolve().parents[3] / "starred")


def _http_url(value) -> str | None:
    value = str(value or "").strip()
    return value if re.match(r"^https?://[^\s<>\"']+$", value) else None


def _number(value, cast=float):
    try:
        return cast(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


def _title_from_slug(slug: str) -> str:
    """Make a title from the folder name: sanjay-van_plot-3 -> Sanjay van plot 3."""
    words = re.sub(r"[-_]+", " ", slug).strip()
    return (words[:1].upper() + words[1:]) or slug


def _stac_source(folder: str) -> tuple | None:
    """Find which run a starred folder was copied from.

    Reads the project id and run number from stac_item.json, if present.
    """
    path = find(folder, "stac")
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            props = (json.load(f).get("properties") or {})
        pid, run = props.get("project_id"), props.get("run")
        return (str(pid), int(run)) if pid and run is not None else None
    except Exception:                    # noqa: BLE001
        return None


def _load_starred(slug: str, folder: str) -> tuple[Item | None, int, str | None]:
    """Read one starred folder. Returns (item, order, problem).

    item is None if the folder cannot be shown; problem says why.
    """
    # meta.json is optional. If it exists but is broken, the folder is hidden.
    meta_path = os.path.join(folder, "meta.json")
    meta = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception as exc:         # noqa: BLE001
            return None, 0, f"meta.json is not valid JSON ({exc})"
        if not isinstance(meta, dict):
            return None, 0, "meta.json must be a JSON object"
    if meta.get("hidden") is True:
        return None, 0, "hidden"
    title = _clean(meta.get("title"), 80) or _title_from_slug(slug)
    if find(folder, "overlay") is None:
        return None, 0, "no overlay.png / overlay.jpg (the crown-outline picture)"

    problem = None
    share = str(meta.get("share") or TIER_ALL).strip().lower()
    if share not in (TIER_ALL, TIER_CROWNS):
        problem = f'"share" must be "all" or "crowns", got {share!r}; showing crowns only'
        share = TIER_CROWNS

    summary = build_summary(folder, share)
    for key, src, cast in (("crowns", "crowns", int), ("area_km2", "area_km2", float),
                           ("gsd_cm", "resolution_cm", float)):
        given = _number(meta.get(src), cast)
        if given is not None and given >= 0:
            summary[key] = given

    files_url = None
    if meta.get("files_url"):
        files_url = _http_url(meta["files_url"])
        if files_url is None:
            problem = '"files_url" is not an http(s) link; left out'

    how = meta.get("how")
    if isinstance(how, str):
        how = [how]
    how = [h for h in (_clean(x, 200) for x in (how if isinstance(how, list) else [])) if h]

    order = _number(meta.get("order"), int)
    item = Item(
        id=STARRED_PREFIX + slug,
        dir=folder,
        tier=share,
        starred=True,
        title=title,
        place=_clean(meta.get("place"), 80),
        date=_clean(meta.get("date"), 32),
        summary=summary,
        files_url=files_url if share == TIER_ALL else None,
        how=how,
        description=_clean(meta.get("description"), 600),
        source_run_id=_clean(meta.get("run_id"), 64),
        source_run=_stac_source(folder),
    )
    return item, (order if order is not None else 1000), problem


def _folder_stamp(folder: str) -> float:
    """Latest change time of the folder and the files directly inside it."""
    stamp = 0.0
    try:
        stamp = os.path.getmtime(folder)
        with os.scandir(folder) as entries:
            for e in entries:
                stamp = max(stamp, e.stat().st_mtime)
    except OSError:
        pass
    return stamp


_starred_lock = threading.Lock()
_starred_cache: dict = {"at": 0.0, "items": [], "problems": {}, "by_slug": {}}
_RESCAN_SECONDS = 15.0


def scan_starred(force: bool = False) -> tuple[list[Item], dict[str, str]]:
    """Return the starred items in display order, and the skipped folders.

    The folder is checked again every 15 seconds, so new folders show up
    without a restart. Unchanged folders are not read again.
    """
    with _starred_lock:
        now = time.monotonic()
        if not force and now - _starred_cache["at"] < _RESCAN_SECONDS:
            return _starred_cache["items"], _starred_cache["problems"]

        root = starred_root()
        seen: dict = {}
        problems: dict[str, str] = {}
        ranked = []
        try:
            names = sorted(os.listdir(root))
        except OSError:
            names = []
        for name in names:
            folder = os.path.join(root, name)
            if name.startswith((".", "_")) or not os.path.isdir(folder):
                continue                 # skip files, drafts (_name) and dotfiles
            if not _SLUG_RE.match(name):
                problems[name] = ("folder name must be lower-case letters, digits, "
                                  "'-' or '_' (max 64)")
                continue
            stamp = _folder_stamp(folder)
            old = _starred_cache["by_slug"].get(name)
            if old is not None and old[0] == stamp and not force:
                loaded = old[1]
            else:
                try:
                    loaded = _load_starred(name, folder)
                except Exception as exc:  # noqa: BLE001
                    loaded = (None, 0, f"could not be read ({exc})")
                if loaded[2] and loaded[2] != "hidden":
                    log.warning("starred/%s: %s", name, loaded[2])
            seen[name] = (stamp, loaded)
            item, order, problem = loaded
            if problem:
                problems[name] = problem
            if item is not None:
                ranked.append((order, item.title.lower(), item))

        ranked.sort(key=lambda t: (t[0], t[1]))
        _starred_cache.update(at=now, items=[t[2] for t in ranked],
                              problems=problems, by_slug=seen)
        return _starred_cache["items"], problems


def get_item(db, item_id: str) -> Item | None:
    """Return the item for a public id, or None if it is not shared."""
    if item_id.startswith(STARRED_PREFIX):
        return next((i for i in scan_starred()[0] if i.id == item_id), None)
    run = db.get(models.Run, item_id)
    if run is None:
        return None
    if run.project is None or run.project.state == "DELETING":
        return None
    return item_from_run(run)


def list_items(db, page: int, per_page: int) -> tuple[list[Item], int]:
    """Return one page of items and the total count.

    Starred items come first, then shared runs, newest first.
    """
    starred, _ = scan_starred()
    # A run that is also starred is shown only once, as the starred item.
    copied = {i.source_run_id for i in starred if i.source_run_id}
    for pid, number in {i.source_run for i in starred if i.source_run}:
        row = (db.query(models.Run.id)
               .filter_by(project_id=pid, number=number).first())
        if row:
            copied.add(row[0])

    query = db.query(models.Run).filter(
        models.Run.consent.in_((1, 2)), models.Run.public_at.isnot(None))
    if copied:
        query = query.filter(models.Run.id.notin_(copied))
    total_runs = query.count()

    start = (page - 1) * per_page
    items = starred[start:start + per_page]
    room = per_page - len(items)
    if room > 0:
        rows = (query.order_by(models.Run.public_at.desc(), models.Run.id)
                .offset(max(0, start - len(starred))).limit(room).all())
        items = items + [i for i in (item_from_run(r) for r in rows) if i]
    return items, len(starred) + total_runs
