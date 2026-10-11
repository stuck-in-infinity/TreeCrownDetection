"""Public gallery routes. No sign-in needed.

These show shared runs and starred runs to anyone. A request gives only a
public id and a file name from a fixed list, never a path. What each item is
allowed to show is decided in services/gallery.py.
"""
import math
import os

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.core.settings import settings
from app.db.session import get_db
from app.services import gallery

router = APIRouter(prefix="/public")
log = get_logger("app.api")

_BASE = "/api/v1/public/gallery"
# Browsers may reuse a picture for 5 minutes.
_IMAGE_CACHE = {"Cache-Control": "public, max-age=300"}


def _not_found():
    # Same reply for missing, not shared and not allowed.
    return HTTPException(404, {"code": "NOT_FOUND",
        "message": "Nothing is shared here.",
        "hint": "open the gallery and pick a survey it lists"})


def _item(db: Session, item_id: str) -> gallery.Item:
    item = gallery.get_item(db, item_id)
    if item is None:
        raise _not_found()
    return item


def _card(item: gallery.Item) -> dict:
    s = item.summary
    species = gallery.species_rows(s) if item.allows(gallery.TIER_ALL) else []
    kinds = gallery.image_kinds(item)
    thumb = kinds[0] if kinds else None
    return {
        "id": item.id,
        "title": item.title,
        "place": item.place,
        "date": item.date,
        "tier": item.tier,
        "starred": item.starred,
        "crowns": s.get("crowns"),
        "area_km2": s.get("area_km2"),
        "species_count": len(species) or None,
        "top_species": [r["name"] for r in species[:2]],
        "thumb_url": f"{_BASE}/{item.id}/image/{thumb}?size=thumb" if thumb else None,
    }


@router.get("/gallery")
def gallery_list(page: int = Query(1, ge=1, le=100_000),
                 per_page: int | None = Query(None, ge=1, le=48),
                 db: Session = Depends(get_db)):
    """One page of shared surveys: starred first, then newest shared run."""
    size = per_page or max(1, min(48, settings.gallery_page_size))
    items, total = gallery.list_items(db, page, size)
    return {
        "page": page,
        "per_page": size,
        "pages": max(1, math.ceil(total / size)),
        "total": total,
        "items": [_card(i) for i in items],
    }


@router.get("/gallery/{item_id}")
def gallery_item(item_id: str, db: Session = Depends(get_db)):
    """Everything the detail page shows for one survey."""
    item = _item(db, item_id)
    card = _card(item)
    downloads = []
    for name, (_key, label, _media, _tier) in gallery.DOWNLOADS.items():
        found = gallery.asset_path(item, name)
        if found:
            downloads.append({"name": name, "label": label,
                              "bytes": os.path.getsize(found[0]),
                              "url": f"{_BASE}/{item.id}/download/{name}"})
    card.update({
        "gsd_cm": item.summary.get("gsd_cm"),
        "species": gallery.species_rows(item.summary)
                   if item.allows(gallery.TIER_ALL) else [],
        "views": [{"kind": k, "label": gallery.IMAGE_KINDS[k][0],
                   "url": f"{_BASE}/{item.id}/image/{k}?size=preview"}
                  for k in gallery.image_kinds(item)],
        "downloads": downloads,
        "files_url": item.files_url,
        "how": item.how,
        "description": item.description,
    })
    return card


@router.get("/gallery/{item_id}/image/{kind}")
def gallery_image(item_id: str, kind: str, size: str = "preview",
                  db: Session = Depends(get_db)):
    item = _item(db, item_id)
    if kind == "species_map" and item.allows(gallery.TIER_ALL):
        # Older runs have no species map file yet, so draw it now.
        from app.api.v1.results import _ensure_species_map
        _ensure_species_map(item.project_id, item.run_number)
    path = gallery.image_path(item, kind, size)
    if path is None:
        raise _not_found()
    return FileResponse(path, media_type="image/jpeg", headers=_IMAGE_CACHE)


@router.get("/gallery/{item_id}/download/{name}")
def gallery_download(item_id: str, name: str, db: Session = Depends(get_db)):
    item = _item(db, item_id)
    found = gallery.asset_path(item, name)
    if found is None:
        raise _not_found()
    path, media = found
    return FileResponse(path, media_type=media, filename=name)
