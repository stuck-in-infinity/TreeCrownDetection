#!/usr/bin/env python3
"""Move projects from one owner to another.

Projects created while sign-in was off belong to ``default`` and are hidden
once sign-in is on. Run from code/:

    python scripts/reassign_owner.py --from default --to you@gmail.com          # dry run
    python scripts/reassign_owner.py --from default --to you@gmail.com --apply

Sign-in stores emails lower-cased. To fix older mixed-case owners:

    python scripts/reassign_owner.py --lowercase --apply

Only the DB changes; project folders are named by id.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db.models import Project                              # noqa: E402
from app.db.session import SessionLocal, init_db               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from", dest="src", help="current owner, e.g. default")
    ap.add_argument("--to", dest="dst", help="new owner's email")
    ap.add_argument("--lowercase", action="store_true",
                    help="lower-case every owner instead")
    ap.add_argument("--apply", action="store_true", help="write the change")
    args = ap.parse_args()
    if args.lowercase == bool(args.src or args.dst) or (
            not args.lowercase and not (args.src and args.dst)):
        ap.error("give either --from and --to, or --lowercase")

    init_db()
    db = SessionLocal()
    try:
        if args.lowercase:
            changes = [(p, p.user_id.lower()) for p in db.query(Project).all()
                       if p.user_id and p.user_id != p.user_id.lower()]
        else:
            dst = args.dst.strip().lower()
            changes = [(p, dst) for p in db.query(Project).filter_by(user_id=args.src)]
        for project, new_owner in changes:
            print(f"  {project.id}  {project.name!r}: {project.user_id} -> {new_owner}")
            if args.apply:
                project.user_id = new_owner
        if args.apply:
            db.commit()
            print(f"{len(changes)} project(s) reassigned.")
        else:
            print(f"{len(changes)} project(s) would be reassigned. Add --apply to do it.")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
