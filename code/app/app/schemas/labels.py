from pydantic import BaseModel


class ClusterLabelIn(BaseModel):
    cluster: int
    species: str
    notes: str | None = None


class LabelsIn(BaseModel):
    chosen_k: int
    mapping: list[ClusterLabelIn]
