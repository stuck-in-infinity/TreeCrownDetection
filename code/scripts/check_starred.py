#!/usr/bin/env python3
"""Say what the public gallery makes of each folder under the starred directory.

Run it from ``code/`` with the same environment as the API:
    python scripts/check_starred.py

Exit status is 1 if any folder is skipped, so it can gate a deploy.
The format is described in ``starred/README.md``.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.services import gallery                              # noqa: E402


def main() -> int:
    root = gallery.starred_root()
    print(f"starred directory: {root}")
    if not os.path.isdir(root):
        print("  it does not exist, so the gallery shows shared runs only")
        return 0
    items, problems = gallery.scan_starred(force=True)
    shown = {i.id[len(gallery.STARRED_PREFIX):]: i for i in items}
    for position, item in enumerate(items, start=1):
        slug = item.id[len(gallery.STARRED_PREFIX):]
        pictures = ", ".join(gallery.image_kinds(item)) or "none"
        files = ", ".join(n for n in gallery.DOWNLOADS if gallery.asset_path(item, n)) or "none"
        print(f"  {position}. {slug}: shown as \"{item.title}\" ({item.tier})")
        print(f"       pictures: {pictures}; downloads: {files}; "
              f"crowns: {item.summary.get('crowns')}; "
              f"species: {len(item.summary.get('species') or {}) or None}")
        if slug in problems:
            print(f"       warning: {problems[slug]}")
    skipped = {s: why for s, why in problems.items() if s not in shown}
    for slug, why in sorted(skipped.items()):
        print(f"  -  {slug}: NOT shown, {why}")
    if not items and not skipped:
        print("  no folders yet")
    return 1 if any(why != "hidden" for why in skipped.values()) else 0


if __name__ == "__main__":
    sys.exit(main())
