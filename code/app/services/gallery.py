"""Public gallery: decides what a visitor without sign-in can see.

Every item is a run, read from its own work/run_<n> folder. A run is shown if:
  * its owner gave consent (Run.consent 1 or 2), or
  * an admin listed it in starred.json (see starred/README.md). These are
    shown first, with a "Featured" badge.

Each item has a tier:
  crowns  orthomosaic picture, crown outlines
  all     the above + species map, KMZ, crown table, STAC item, files link

The tier is checked here for every file, so a "crowns" item can never give
out a species file. A bad starred.json entry is skipped, it never causes an error.
"""
from __future__ import annotations

import csv
import glob
import json
import os
import re
import threading
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

STARRED_FILE = "starred.json"

# Where to look for each file inside a run folder.
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
    project_id: str | None = None
    run_number: int | None = None

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
    return (find(item.dir, "crown_master") is not None
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


def starred_file() -> str:
    return os.path.join(starred_root(), STARRED_FILE)


def _http_url(value) -> str | None:
    value = str(value or "").strip()
    return value if re.match(r"^https?://[^\s<>\"']+$", value) else None


def _number(value, cast=float):
    try:
        return cast(value) if value is not None and not isinstance(value, bool) else None
    except (TypeError, ValueError):
        return None


_starred_lock = threading.Lock()
_starred_cache: dict = {"stamp": None, "entries": [], "error": None}
_summary_cache: dict = {}


def read_starred_entries() -> tuple[list[dict], str | None]:
    """Read starred.json. Returns (entries, error).

    The file is read again only when it changes, so edits show up without a
    restart. A missing file means no starred runs.
    """
    path = starred_file()
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return [], None
    with _starred_lock:
        if _starred_cache["stamp"] == stamp:
            return _starred_cache["entries"], _starred_cache["error"]
        entries, error = [], None
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data = data.get("starred")
            if not isinstance(data, list):
                error = 'starred.json must be a list, or {"starred": [...]}'
            else:
                entries = [e for e in data if isinstance(e, dict)]
        except Exception as exc:         # noqa: BLE001
            error = f"starred.json is not valid JSON ({exc})"
        if error:
            log.warning(error)
        _starred_cache.update(stamp=stamp, entries=entries, error=error)
        return entries, error


def starred_project_ids() -> set[str]:
    """Projects that hold a starred run. Retention must not delete these."""
    return {str(e["project_id"]) for e in read_starred_entries()[0] if e.get("project_id")}


def _entry_label(entry: dict, index: int) -> str:
    return str(entry.get("title") or f"entry {index + 1}")


def _find_starred_run(db, entry: dict):
    """Find the run a starred.json entry points to, or None."""
    if entry.get("run_id"):
        return db.get(models.Run, str(entry["run_id"]))
    number = _number(entry.get("run"), int)
    if not entry.get("project_id") or number is None:
        return None
    return (db.query(models.Run)
            .filter_by(project_id=str(entry["project_id"]), number=number).first())


def _live_summary(run, tier: str) -> dict:
    """Card numbers for a starred run, read from its folder and cached.

    Read again when the run's result files change (for example after a relabel).
    """
    base = run_dir(run.project_id, run.number)
    stamp = tuple(os.path.getmtime(f) if f else 0
                  for f in (find(base, "crowns"), find(base, "crown_master")))
    cached = _summary_cache.get((run.id, tier))
    if cached and cached[0] == stamp:
        return dict(cached[1])
    summary = build_summary(base, tier)
    _summary_cache[(run.id, tier)] = (stamp, summary)
    return dict(summary)


def _starred_item(db, entry: dict) -> tuple[Item | None, str | None]:
    """Build the gallery item for one starred.json entry. Returns (item, problem)."""
    if entry.get("hidden") is True:
        return None, "hidden"
    run = _find_starred_run(db, entry)
    if run is None or run.project is None:
        return None, 'run not found (give "project_id" and "run", or "run_id")'
    base = run_dir(run.project_id, run.number)
    if find(base, "overlay") is None:
        return None, "the run has no detection overlay"

    # Tier: "share" in the entry, else the owner's consent, else everything.
    problem = None
    share = str(entry.get("share") or "").strip().lower()
    if share and share not in (TIER_ALL, TIER_CROWNS):
        problem = f'"share" must be "all" or "crowns", got {share!r}; showing crowns only'
        share = TIER_CROWNS
    tier = share or _tier_of(run.consent) or TIER_ALL

    summary = _live_summary(run, tier)
    for key, src, cast in (("crowns", "crowns", int), ("area_km2", "area_km2", float),
                           ("gsd_cm", "resolution_cm", float)):
        given = _number(entry.get(src), cast)
        if given is not None and given >= 0:
            summary[key] = given

    files_url = _run_files_url(run)
    if entry.get("files_url"):
        files_url = _http_url(entry["files_url"])
        if files_url is None:
            problem = '"files_url" is not an http(s) link; left out'

    how = entry.get("how")
    if isinstance(how, str):
        how = [how]
    how = [h for h in (_clean(x, 200) for x in (how if isinstance(how, list) else [])) if h]
    if not how:
        how = _run_how(run)
        if tier == TIER_ALL and run.chosen_k:
            how.append(f"{run.chosen_k} groups, named by the owner")

    when = run.finished_at or run.public_at
    item = Item(
        id=run.id,
        dir=base,
        tier=tier,
        starred=True,
        title=(_clean(entry.get("title"), 80) or _clean(run.public_title, 80)
               or _clean(run.project.name, 80) or "Untitled survey"),
        place=_clean(entry.get("place"), 80) or _clean(run.public_place, 80),
        date=_clean(entry.get("date"), 32) or (when.date().isoformat() if when else None),
        summary=summary,
        files_url=files_url if tier == TIER_ALL else None,
        how=how,
        description=_clean(entry.get("description"), 600),
        project_id=run.project_id,
        run_number=run.number,
    )
    return item, problem


def scan_starred(db) -> tuple[list[Item], dict[str, str]]:
    """Return the starred items in display order, and the skipped entries.

    Order: the "order" number, then the position in the file.
    """
    entries, error = read_starred_entries()
    problems: dict[str, str] = {}
    if error:
        problems[STARRED_FILE] = error
    ranked, seen = [], set()
    for index, entry in enumerate(entries):
        label = _entry_label(entry, index)
        try:
            item, problem = _starred_item(db, entry)
        except Exception as exc:         # noqa: BLE001
            item, problem = None, f"could not be read ({exc})"
        if item is not None and item.id in seen:
            item, problem = None, "the same run is listed twice"
        if problem:
            problems[label] = problem
        if item is not None:
            seen.add(item.id)
            order = _number(entry.get("order"), int)
            ranked.append((order if order is not None else 1000, index, item))
    ranked.sort(key=lambda t: (t[0], t[1]))
    return [t[2] for t in ranked], problems


def refresh_run_card(db, project, run_number: int) -> None:
    """Rebuild a shared run's card numbers. Called after finalize.

    Without this, a relabelled run would keep showing its old species counts.
    """
    try:
        run = (db.query(models.Run)
               .filter_by(project_id=project.id, number=run_number).first())
        tier = _tier_of(run.consent) if run else None
        if tier is None or run.public_at is None:
            return
        run.public_summary = build_summary(run_dir(project.id, run_number), tier)
        db.add(run)
        db.commit()
    except Exception:                    # noqa: BLE001
        log.warning("could not refresh the gallery card of run %s", run_number,
                    exc_info=True)
        try:
            db.rollback()
        except Exception:                # noqa: BLE001
            pass


def get_item(db, item_id: str) -> Item | None:
    """Return the item for a public id (the run's id), or None if not shared."""
    run = db.get(models.Run, item_id)
    if run is None or run.project is None or run.project.state == "DELETING":
        return None
    for item in scan_starred(db)[0]:
        if item.id == run.id:
            return item
    return item_from_run(run)


def list_items(db, page: int, per_page: int) -> tuple[list[Item], int]:
    """Return one page of items and the total count.

    Starred items come first, then shared runs, newest first.
    """
    starred, _ = scan_starred(db)
    query = db.query(models.Run).filter(
        models.Run.consent.in_((1, 2)), models.Run.public_at.isnot(None))
    if starred:
        # A starred run is shown only once, as the starred item.
        query = query.filter(models.Run.id.notin_([i.id for i in starred]))
    total_runs = query.count()

    start = (page - 1) * per_page
    items = starred[start:start + per_page]
    room = per_page - len(items)
    if room > 0:
        rows = (query.order_by(models.Run.public_at.desc(), models.Run.id)
                .offset(max(0, start - len(starred))).limit(room).all())
        items = items + [i for i in (item_from_run(r) for r in rows) if i]
    return items, len(starred) + total_runs
