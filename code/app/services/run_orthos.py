"""Which orthomosaics a run used.

Stored in run params: ``ortho_ids`` / ``ortho_stems`` lists, or the older
single ``ortho_id`` / ``ortho_stem``. Per-ortho status is in
``work/run_<n>/orthos.json``, written by the worker.
"""
import json
import os

STATUS_FILE = "orthos.json"


def pinned(params: dict | None) -> list[dict]:
    """``[{id, stem}]`` in run order."""
    params = dict(params or {})
    ids = list(params.get("ortho_ids") or [])
    stems = list(params.get("ortho_stems") or [])
    if ids or stems:
        n = max(len(ids), len(stems))
        ids += [None] * (n - len(ids))
        stems += [None] * (n - len(stems))
        return [{"id": i, "stem": s} for i, s in zip(ids, stems)]
    if params.get("ortho_id") or params.get("ortho_stem"):
        return [{"id": params.get("ortho_id"), "stem": params.get("ortho_stem")}]
    return []


def refs(params: dict | None) -> set[str]:
    """All ids and stems, for ``in_use``."""
    out: set[str] = set()
    for o in pinned(params):
        out.update(v for v in (o["id"], o["stem"]) if v)
    return out


def describe(params: dict | None, library, work_dir: str | None = None) -> list[dict]:
    """``[{id, stem, filename, status, n_crowns, error}]`` for a run."""
    by_id = {o.id: o for o in (library or [])}
    by_stem = {o.stem: o for o in (library or [])}
    status = read_status(work_dir) if work_dir else {}
    out = []
    for p in pinned(params):
        o = by_id.get(p["id"]) or by_stem.get(p["stem"])
        stem = p["stem"] or (o.stem if o else None)
        st = status.get(stem) or {}
        out.append({
            "id": p["id"] or (o.id if o else None),
            "stem": stem,
            "filename": o.filename if o else st.get("filename"),
            "status": st.get("status"),
            "n_crowns": st.get("n_crowns"),
            "error": st.get("error"),
        })
    return out


def read_status(work_dir: str) -> dict:
    """``{stem: row}`` from orthos.json, or {}."""
    try:
        with open(os.path.join(work_dir, STATUS_FILE)) as f:
            rows = json.load(f)
        return {r["stem"]: r for r in rows if isinstance(r, dict) and r.get("stem")}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def write_status(work_dir: str, rows: list[dict]) -> None:
    tmp = os.path.join(work_dir, STATUS_FILE + ".part")
    with open(tmp, "w") as f:
        json.dump(rows, f, indent=1)
    os.replace(tmp, os.path.join(work_dir, STATUS_FILE))
