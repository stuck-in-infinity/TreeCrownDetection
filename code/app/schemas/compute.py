"""Request and response models for the STACD compute contract.

These match what the STACD backend expects. Its DAG generator posts a JSON body
made of the algorithm's declared parameters plus an ``execution_id``, and treats
the call as failed unless the response has ``status == "success"``.
"""
from pydantic import BaseModel


class ComputeRequest(BaseModel):
    """Body the STACD Airflow DAG node posts to the /compute/* endpoints."""

    execution_id: str          # UUID Airflow creates per attempt, for tracing
    project_id: str            # the project to analyze or finalize
    run: int | None = None     # defaults to project.current_run when omitted


class STACDResponse(BaseModel):
    """Success or failure envelope the DAG reads.

    Anything we handle comes back as HTTP 200 so the DAG can read ``status`` and
    ``message``; only unexpected faults produce a 5xx envelope instead.
    """

    status: str                # "success" or "failed"
    message: str
    execution_id: str
    node_type: str
    task_id: str | None = None         # repeats execution_id, as CoreStack expects
    asset_ids: list[str] = []          # paths or URLs of what was produced
    execution_time: float = 0.0
    stac: list | None = None           # STAC features, if the caller wants them
