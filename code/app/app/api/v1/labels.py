"""Submit the cluster -> species mapping by uploading the filled CSV.

This closes the human-in-the-loop gate between DAG 1 and DAG 2.
"""
import csv
import io

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.core.logging import ERROR_CODES
from app.api.deps import get_project
from app.db import models
from app.db.session import get_db
from app.services.pipeline_adapter import normalize_species, write_species_map_csv
from app.services import run_registry
from app.services.state import transition_if

router = APIRouter()

_LABEL_STATES = {"AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED"}
_REQUIRED_COLS = {"cluster", "species"}


@router.post("/projects/{project_id}/runs/{run}/labels")
@router.post("/project/runs/{run}/labels")
@router.post("/projects/{project_id}/labels")
@router.post("/project/labels")
def submit_labels(
    project=Depends(get_project),
    run: int | None = None,
    chosen_k: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload the filled ``k{chosen_k}_cluster_species_map.csv``.

    The CSV must have at least the columns ``cluster`` and ``species``;
    ``cluster_folder`` and ``notes`` are accepted but optional. One row per
    cluster (0 to chosen_k − 1). Empty species values are stored as
    ``unlabelled``; whitespace, case, and hyphens in species names are
    normalised server-side so 'Non Acacia', 'non-acacia', and 'non_acacia'
    all become ``non_acacia``.
    """
    # Every rejection below names what was wrong AND what to do about it. This
    # is the one step in the pipeline a person cannot skip, and it is reached
    # after a run that may have taken half an hour — a bare "400 Bad Request"
    # here costs the user that run's worth of patience.
    # Which run is being labelled. Omitted means the active one, which is what
    # every existing caller means. Named explicitly, it may be any run of this
    # project that has reached clustering — including one that finished weeks
    # ago while a different run is computing right now.
    target_run = run if run is not None else (project.current_run or 1)
    run_row = run_registry.ensure_run(db, project, target_run)
    db.commit()
    is_active = target_run == (project.current_run or 1)

    if run_row.state not in _LABEL_STATES:
        raise HTTPException(409, {
            "code": ERROR_CODES["INVALID_STATE"],
            "message": (
                f"Labels can only be submitted once a run has finished "
                f"clustering. Run {target_run} is currently {run_row.state}."
            ),
            "project_id": project.id,
            "hint": ("wait for that run to reach AWAITING_LABELS, or pick a run "
                     "that already has results"),
            "details": {"run": target_run, "state": run_row.state,
                        "accepted_states": sorted(_LABEL_STATES)},
        })
    if run_row.available_k and chosen_k not in run_row.available_k:
        raise HTTPException(400, {
            "code": ERROR_CODES["INVALID_PARAM"],
            "message": (
                f"chosen_k = {chosen_k} is not one of the cluster counts this run "
                f"produced ({', '.join(str(k) for k in project.available_k)})."
            ),
            "project_id": project.id,
            "hint": ("pick one of the listed values — they are the k values the "
                     "run actually clustered at"),
            "details": {"chosen_k": chosen_k, "available_k": list(project.available_k)},
        })
    if not (file.filename or "").lower().endswith(".csv"):
        raise HTTPException(400, {
            "code": ERROR_CODES["BAD_FORMAT"],
            "message": (
                f"The labels file must be a .csv; you sent "
                f"'{file.filename or 'a file with no name'}'."
            ),
            "project_id": project.id,
            "hint": ("in Excel or Sheets choose File > Save as / Download > CSV, "
                     "then upload that file"),
        })

    # Parse the upload. utf-8-sig strips a BOM if Excel added one.
    try:
        raw = file.file.read().decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HTTPException(400, {
            "code": ERROR_CODES["BAD_FORMAT"],
            "message": (
                "The labels file is not readable as UTF-8 text, so the species "
                "names could not be decoded."
            ),
            "project_id": project.id,
            "hint": ("re-save the CSV as UTF-8 — this usually happens when a "
                     "spreadsheet saves in a regional encoding and the species "
                     "names contain non-English characters"),
            "details": {"decode_error": str(exc)},
        })
    finally:
        file.file.close()

    reader = csv.DictReader(io.StringIO(raw))
    cols = set(reader.fieldnames or [])
    missing = _REQUIRED_COLS - cols
    if missing:
        raise HTTPException(400, {
            "code": ERROR_CODES["MISSING_PARAM"],
            "message": (
                f"The labels CSV is missing required column(s): "
                f"{', '.join(sorted(missing))}."
            ),
            "project_id": project.id,
            "hint": ("keep the header row from the template the run produced — "
                     f"it must contain {', '.join(sorted(_REQUIRED_COLS))}"),
            "details": {"missing": sorted(missing), "found": sorted(cols),
                        "required": sorted(_REQUIRED_COLS)},
        })

    mapping: dict[int, dict] = {}
    for row in reader:
        try:
            cid = int(str(row.get("cluster", "")).strip())
        except (TypeError, ValueError):
            continue
        if cid < 0 or cid >= chosen_k:
            continue  # silently ignore rows outside the chosen_k range
        mapping[cid] = {
            "species": normalize_species(row.get("species", "")),
            "notes": (row.get("notes") or "").strip(),
        }

    if not mapping:
        raise HTTPException(400, {
            "code": ERROR_CODES["NO_LABELS"],
            "message": (
                f"No usable rows were found in the labels CSV. Rows are used only "
                f"when the 'cluster' column holds a whole number from 0 to "
                f"{chosen_k - 1}."
            ),
            "project_id": project.id,
            "hint": ("check the cluster column really holds numbers (not text or "
                     "blanks), and that they match the k you chose"),
            "details": {"chosen_k": chosen_k, "valid_cluster_range": [0, chosen_k - 1]},
        })

    # Replace any previous mapping FOR THIS RUN only. Scoping by run is what
    # lets run 2 keep its labels while run 4 is being labelled — the old
    # project-wide delete is exactly what made past runs unfinishable.
    db.query(models.ClusterLabel).filter_by(
        project_id=project.id, run_id=run_row.id
    ).delete()
    for cid, m in mapping.items():
        db.add(
            models.ClusterLabel(
                project_id=project.id,
                run_id=run_row.id,
                chosen_k=chosen_k,
                cluster_id=cid,
                species=m["species"],
                notes=m["notes"],
            )
        )

    # chosen_k belongs to the run. It is also written onto the project when the
    # run IS the active one, because finalize and the pipeline still read it
    # from there.
    run_params = dict(run_row.params or {})
    run_params["chosen_k"] = chosen_k
    run_row.params = run_params
    run_row.chosen_k = chosen_k
    db.add(run_row)
    if is_active:
        params = dict(project.params or {})
        params["chosen_k"] = chosen_k
        project.params = params
        db.add(project)
    db.commit()

    # write the canonical CSV the pipeline's step2 reads, into THAT run's folder
    write_species_map_csv(project, chosen_k, mapping, run=target_run)

    # Claim the state atomically. The check at the top of this handler is a
    # fast-fail for a good error message, not a guard: parsing, the label
    # rewrite and the CSV write all sit between it and here, and a re-analyze
    # trigger can win that window. A plain assignment would then stamp
    # LABELS_SUBMITTED over ANALYZING, dropping the busy guard and letting a
    # finalize start against a run whose step-1 outputs are being rewritten.
    if is_active:
        # The active run shares its state with the project, so the claim has to
        # be atomic: parsing, the label rewrite and the CSV write all sit
        # between the check at the top and here, and a re-analyze trigger can
        # win that window. A plain assignment would stamp LABELS_SUBMITTED over
        # ANALYZING, dropping the busy guard and letting a finalize start
        # against a run whose step-1 outputs are being rewritten.
        if not transition_if(db, project, _LABEL_STATES, "LABELS_SUBMITTED"):
            # Lost the race — drop the rows we just wrote so a freshly-opened
            # run doesn't inherit labels from the clustering it is about to
            # replace. Scoped to this run; other runs' labels are untouched.
            db.query(models.ClusterLabel).filter_by(
                project_id=project.id, run_id=run_row.id
            ).delete()
            db.commit()
            db.refresh(project)
            raise HTTPException(409, {
                "code": "CONFLICT_BUSY",
                "message": (
                    f"Project moved to {project.state} while the labels were "
                    "being saved; the labels were discarded"
                ),
                "project_id": project.id,
                "hint": "wait for the current run to finish, then resubmit",
            })
    else:
        # An older run. Its state is its own — the project may well be ANALYZING
        # a different run right now, and saying so here would drop that run's
        # busy guard. Nothing shared is touched: the labels and the CSV both
        # live under work/run_<target>/.
        run_row.state = "LABELS_SUBMITTED"
        db.add(run_row)
        db.commit()

    counts: dict[str, int] = {}
    for v in mapping.values():
        counts[v["species"]] = counts.get(v["species"], 0) + 1
    return {
        "project_id": project.id,
        "run": target_run,
        "run_id": run_row.id,
        "state": run_row.state,
        "project_state": project.state,
        "chosen_k": chosen_k,
        "species_counts_preview": counts,
    }
