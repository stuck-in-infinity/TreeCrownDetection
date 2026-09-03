"""Submit the cluster-to-species mapping by uploading the filled-in CSV.

This is the human step between the two compute jobs; finalize cannot start
until it is done.
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
from app.services.state import transition_if

router = APIRouter()

_LABEL_STATES = {"AWAITING_LABELS", "LABELS_SUBMITTED", "COMPLETED"}
_REQUIRED_COLS = {"cluster", "species"}


@router.post("/projects/{project_id}/labels")
@router.post("/project/labels")
def submit_labels(
    project=Depends(get_project),
    chosen_k: int = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    """Upload the filled-in ``k{chosen_k}_cluster_species_map.csv``.

    The CSV needs at least the ``cluster`` and ``species`` columns;
    ``cluster_folder`` and ``notes`` are accepted but not required. There should
    be one row per cluster, numbered 0 to chosen_k - 1. A blank species is
    stored as ``unlabelled``, and species names are normalised here, so
    'Non Acacia', 'non-acacia' and 'non_acacia' all end up as ``non_acacia``.
    """
    # Each rejection below says what was wrong and what to do about it. This is
    # the one step nobody can skip, and it comes after a run that may have taken
    # half an hour, so a bare "400 Bad Request" would cost the user that run.
    if project.state not in _LABEL_STATES:
        raise HTTPException(409, {
            "code": ERROR_CODES["INVALID_STATE"],
            "message": (
                f"Labels can only be submitted once a run is waiting for them. "
                f"This project is currently {project.state}."
            ),
            "project_id": project.id,
            "hint": ("wait for the analysis to reach AWAITING_LABELS, or start a "
                     "new run if the last one failed"),
            "details": {"state": project.state, "accepted_states": sorted(_LABEL_STATES)},
        })
    if project.available_k and chosen_k not in project.available_k:
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

    # Read the upload. utf-8-sig removes the byte-order mark Excel may add.
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
            continue  # ignore rows whose cluster number is outside chosen_k
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

    # Note which run these labels belong to before writing anything. An analyze
    # trigger arriving now would archive the current run and move the counter on.
    run = project.current_run or 1

    # Replace any mapping already stored for this project.
    db.query(models.ClusterLabel).filter_by(project_id=project.id).delete()
    for cid, m in mapping.items():
        db.add(
            models.ClusterLabel(
                project_id=project.id,
                chosen_k=chosen_k,
                cluster_id=cid,
                species=m["species"],
                notes=m["notes"],
            )
        )

    # Save chosen_k into params. It has to be a new dict, otherwise SQLAlchemy
    # does not notice the change.
    params = dict(project.params or {})
    params["chosen_k"] = chosen_k
    project.params = params
    db.add(project)
    db.commit()

    # Write the CSV that the pipeline's step 2 reads.
    write_species_map_csv(project, chosen_k, mapping, run=run)

    # Move the state with a conditional update. The state check at the top of
    # this handler gives a good error message but is not a lock: parsing, the
    # label rewrite and the CSV write all happen after it, and a re-analyze
    # trigger can arrive in that gap. Assigning the state directly would write
    # LABELS_SUBMITTED over ANALYZING, which would let a finalize start against
    # a run whose Step 1 output is being rebuilt.
    if not transition_if(db, project, _LABEL_STATES, "LABELS_SUBMITTED"):
        # Another request got there first. Remove the rows just written, so the
        # run that is starting does not inherit labels from the clustering it is
        # about to replace.
        db.query(models.ClusterLabel).filter_by(project_id=project.id).delete()
        db.commit()
        db.refresh(project)
        raise HTTPException(409, {
            "code": "CONFLICT_BUSY",
            "message": (
                f"Project moved to {project.state} while the labels were being "
                "saved; the labels were discarded"
            ),
            "project_id": project.id,
            "hint": "wait for the current run to finish, then resubmit",
        })

    counts: dict[str, int] = {}
    for v in mapping.values():
        counts[v["species"]] = counts.get(v["species"], 0) + 1
    return {
        "project_id": project.id,
        "state": project.state,
        "chosen_k": chosen_k,
        "species_counts_preview": counts,
    }
