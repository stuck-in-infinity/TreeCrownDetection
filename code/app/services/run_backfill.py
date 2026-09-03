"""Create ``Run`` rows for projects that predate the runs table.

New runs write their own rows. This exists for the projects already in the
database when the table appeared: their history lives in the ``project.runs``
JSON column plus the live fields on the project row.

It runs once at start-up, next to ``startup_recovery``, and follows the same two
rules for the same reasons. It is safe to run twice — a project that already has
run rows is skipped, so a restart never duplicates anything. And it never
raises: a project left un-backfilled shows an empty run list, which somebody can
put right, whereas a service that will not boot cannot be fixed from the outside.

The one thing it cannot reconstruct is which orthomosaic an old run used.
``ortho_id`` comes from ``params['ortho_id']``, and a run from before the ortho
library existed never had one. The ``ortho`` filename the old archiver wrote
alongside it is no help either: it recorded ``project.orthos[0]``, the first
ortho in the library rather than the one the run actually used. Where the project
has exactly one orthomosaic there is nothing to be ambiguous about and it is
used; otherwise ``ortho_id`` stays NULL and the run shows as "orthomosaic not
recorded". Guessing would file somebody's run under the wrong survey, which is
worse than admitting the gap.
"""
from __future__ import annotations

from app.core.logging import get_logger, naive_now
from app.db import models

log = get_logger("app.run_backfill")


def _ortho_id_for(project, entry_params: dict) -> str | None:
    pinned = (entry_params or {}).get("ortho_id")
    if pinned and any(o.id == pinned for o in (project.orthos or [])):
        return pinned
    orthos = list(project.orthos or [])
    return orthos[0].id if len(orthos) == 1 else None


def backfill_runs(db) -> dict:
    """Give every project without run rows one row per run it has had."""
    created, projects_touched, labels_linked = 0, 0, 0
    try:
        projects = db.query(models.Project).all()
        for project in projects:
            existing = (
                db.query(models.Run).filter_by(project_id=project.id).count()
            )
            if existing:
                continue                       # already done, or born with rows

            current = project.current_run or 1
            rows: list[models.Run] = []

            # Archived runs, from the JSON history.
            for entry in list(project.runs or []):
                params = dict(entry.get("params") or {})
                number = entry.get("run")
                if not isinstance(number, int) or number < 1:
                    continue
                rows.append(models.Run(
                    project_id=project.id,
                    number=number,
                    ortho_id=_ortho_id_for(project, params),
                    name=entry.get("run_name"),
                    state=entry.get("state") or "COMPLETED",
                    model_key=entry.get("model_key"),
                    params=params,
                    recommended_k=entry.get("recommended_k"),
                    available_k=entry.get("available_k"),
                    chosen_k=params.get("chosen_k"),
                    created_at=project.created_at or naive_now(),
                    finished_at=project.updated_at,
                ))

            # The live run, from the project's own fields.
            live_params = dict(project.params or {})
            rows.append(models.Run(
                project_id=project.id,
                number=current,
                ortho_id=_ortho_id_for(project, live_params),
                name=getattr(project, "run_name", None),
                state=project.state,
                model_key=project.model_key,
                params=live_params,
                recommended_k=project.recommended_k,
                available_k=project.available_k,
                chosen_k=live_params.get("chosen_k"),
                created_at=project.created_at or naive_now(),
                started_at=project.created_at,
                finished_at=project.updated_at,
            ))

            # Two history entries claiming the same number would violate the
            # unique constraint and abort the whole backfill; keep the last.
            by_number = {r.number: r for r in rows}
            for run in by_number.values():
                db.add(run)
            db.flush()
            created += len(by_number)
            projects_touched += 1

            # Existing labels describe whatever run was live when they were
            # submitted, which is the current one — archiving used to delete
            # them, so no older label rows can exist.
            live_row = by_number.get(current)
            if live_row is not None:
                n = (
                    db.query(models.ClusterLabel)
                    .filter(models.ClusterLabel.project_id == project.id,
                            models.ClusterLabel.run_id.is_(None))
                    .update({models.ClusterLabel.run_id: live_row.id},
                            synchronize_session=False)
                )
                labels_linked += n or 0

        db.commit()
    except Exception:                          # noqa: BLE001 - must never block start-up
        log.exception("run backfill failed; continuing without it")
        try:
            db.rollback()
        except Exception:                      # noqa: BLE001
            pass
        return {"runs_created": 0, "projects": 0, "labels_linked": 0, "error": True}

    if created:
        log.warning("run backfill: %d run row(s) across %d project(s), %d label(s) linked",
                    created, projects_touched, labels_linked)
    return {"runs_created": created, "projects": projects_touched,
            "labels_linked": labels_linked}
