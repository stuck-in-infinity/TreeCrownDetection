#!/usr/bin/env python3
"""Show how the public gallery reads starred.json.

Run it from code/ with the same environment as the API:
    python scripts/check_starred.py

Exit status is 1 if any entry is skipped. Format: starred/README.md.
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.session import SessionLocal                       # noqa: E402
from app.services import gallery                              # noqa: E402


def main() -> int:
    path = gallery.starred_file()
    print(f"starred file: {path}")
    if not os.path.isfile(path):
        print("  not found, so the gallery shows shared runs only")
        return 0
    db = SessionLocal()
    try:
        items, problems = gallery.scan_starred(db)
        for position, item in enumerate(items, start=1):
            pictures = ", ".join(gallery.image_kinds(item)) or "none"
            files = ", ".join(n for n in gallery.DOWNLOADS if gallery.asset_path(item, n)) or "none"
            print(f"  {position}. \"{item.title}\" ({item.tier}) "
                  f"project {item.project_id} run {item.run_number}")
            print(f"       pictures: {pictures}; downloads: {files}; "
                  f"crowns: {item.summary.get('crowns')}; "
                  f"species: {item.summary.get('species')}")
            if item.title in problems:
                print(f"       warning: {problems[item.title]}")
        shown = {i.title for i in items}
        skipped = {k: v for k, v in problems.items() if k not in shown}
        for label, why in skipped.items():
            print(f"  -  {label}: NOT shown, {why}")
        if not items and not skipped:
            print("  no entries yet")
        return 1 if any(why != "hidden" for why in skipped.values()) else 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
