"""STACD-compliant compute request/response models.

These mirror the STACD backend contract (see STACD_framework-main/backend/main.py
``STACDResponse`` and the DAG generator, which posts a JSON body built from the
algorithm's declared params + an ``execution_id`` and treats the call as failed
unless the response ``status == "success"``).
"""
from pydantic import BaseModel


class ComputeRequest(BaseModel):
    """Body the STACD Airflow DAG node posts to our /compute/* endpoints."""

    execution_id: str          # per-attempt UUID minted by Airflow (for lineage)
    project_id: str            # required: the project to analyze/finalize
    run: int | None = None     # optional; worker uses project.current_run if omitted


class STACDResponse(BaseModel):
    """STACD success/failure envelope. Always returned with HTTP 200 for handled
    outcomes so the DAG can read ``status`` + ``message`` (only unexpected faults
    surface as a 5xx envelope)."""

    status: str                # "success" | "failed"
    message: str
    execution_id: str
    node_type: str
    task_id: str | None = None         # echo of execution_id (CoreStack shape)
    asset_ids: list[str] = []          # produced asset URLs/paths
    execution_time: float = 0.0
    stac: list | None = None           # optional STAC features (forward-compat)
