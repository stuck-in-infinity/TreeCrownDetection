"""SQLAlchemy models. These tables are where project state actually lives.

The states a project moves through:
  CREATED -> UPLOADED -> ANALYZING -> AWAITING_LABELS
          -> LABELS_SUBMITTED -> FINALIZING -> COMPLETED
Any of the compute stages can go to FAILED instead.

Two more states act as short-lived locks, held for one request and never across
requests (see api/v1/projects.py):
  UPLOADING  - input files are being replaced
  DELETING   - the project is being removed
Neither is a state analyze or finalize can start from, which is what makes them
work as locks. Clients should treat any state they do not recognise as busy.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.logging import naive_now
from app.db.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(String, default="default", index=True)
    name: Mapped[str] = mapped_column(String, default="")
    model_key: Mapped[str] = mapped_column(String, default="urban_cambridge")
    state: Mapped[str] = mapped_column(String, default="CREATED", index=True)
    source_epsg: Mapped[int | None] = mapped_column(Integer, nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    recommended_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_k: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # current_run points at the active work/run_<n> folder. runs holds a short
    # history of earlier runs: their parameters and how they ended.
    current_run: Mapped[int] = mapped_column(Integer, default=1)
    # Display name shown for the current run. Folders on disk stay run_<n>.
    run_name: Mapped[str | None] = mapped_column(String, nullable=True)
    runs: Mapped[list | None] = mapped_column(JSON, default=list)
    share_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    # Data-sharing consent, collected after finalize:
    #   0 = no, or not answered yet (the default)
    #   1 = yes, all data
    #   2 = yes, but only the unlabelled Step 1 crown data
    consent: Mapped[int] = mapped_column(Integer, default=0)
    consent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Set by scripts/run_retention.py once a consent=2 project has been cut back
    # to Step 1. The script skips any row that already has it.
    pruned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=naive_now, onupdate=naive_now
    )

    orthos: Mapped[list["Ortho"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    jobs: Mapped[list["Job"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    run_rows: Mapped[list["Run"]] = relationship(
        back_populates="project", cascade="all, delete-orphan",
        order_by="Run.number",
    )
    labels: Mapped[list["ClusterLabel"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Ortho(Base):
    __tablename__ = "orthos"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    stem: Mapped[str] = mapped_column(String)            # filename without extension
    filename: Mapped[str] = mapped_column(String)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    crs: Mapped[str | None] = mapped_column(String, nullable=True)
    bands: Mapped[int | None] = mapped_column(Integer, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="orthos")


class Job(Base):
    __tablename__ = "jobs"

    # This index is how /compute/* claims a run: the INSERT itself, not an
    # earlier SELECT, decides which of two callbacks with the same
    # Idempotency-Key computes. NULLs compare as distinct, so jobs created
    # before dispatch assigns an id are unaffected.
    __table_args__ = (
        UniqueConstraint("project_id", "celery_task_id", name="uq_jobs_project_task"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    type: Mapped[str] = mapped_column(String)            # 'analyze' | 'finalize'
    state: Mapped[str] = mapped_column(String, default="QUEUED")  # QUEUED|RUNNING|SUCCEEDED|FAILED
    current_stage: Mapped[str | None] = mapped_column(String, nullable=True)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    celery_task_id: Mapped[str | None] = mapped_column(String, nullable=True)
    # Correlation id of the HTTP request that created this job, so log lines
    # from the run can be traced back to it. Filled in when the Job is created.
    request_id: Mapped[str | None] = mapped_column(String, nullable=True)
    log_path: Mapped[str | None] = mapped_column(String, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="jobs")


class Run(Base):
    """One analysis run, and everything that belongs to it alone.

    Every field here used to live on ``Project``, which meant a project had
    exactly one run: starting a second overwrote the first's state, parameters
    and chosen k, and ``archive_current_run`` deleted its labels outright. The
    species names the user assigned — the most expensive thing they produce —
    did not survive the next run, so there was nothing to go back to and no way
    to finish an earlier run later.

    A run has two identities, on purpose. ``number`` is the ``n`` in
    ``work/run_<n>``: the key the pipeline, the Airflow DAG, ``project_paths()``
    and every ``/compute/*`` callback already use, so it stays as it was and none
    of them need to change. ``id`` is what the API and the frontend address,
    because a uuid cannot be mistaken for a different project's run 2.

    This does not replace ``Project.state``. That stays as the lock — one
    computing run per project — and as a mirror of the active run, so existing
    callers keep working. ``Run.state`` is the truth about a run. See
    ``services/run_registry.py``.
    """

    __tablename__ = "runs"

    # The run number is the on-disk folder name, so two rows claiming the same
    # number in one project would be two rows claiming the same directory.
    __table_args__ = (
        UniqueConstraint("project_id", "number", name="uq_runs_project_number"),
    )

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)          # work/run_<number>

    #: The orthomosaic this run actually used. Nullable because a run recorded
    #: before the ortho library existed cannot be attributed to one, and saying
    #: "not recorded" is better than guessing.
    ortho_id: Mapped[str | None] = mapped_column(
        ForeignKey("orthos.id"), nullable=True, index=True
    )

    name: Mapped[str | None] = mapped_column(String, nullable=True)
    state: Mapped[str] = mapped_column(String, default="CREATED", index=True)
    model_key: Mapped[str | None] = mapped_column(String, nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    recommended_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    available_k: Mapped[list | None] = mapped_column(JSON, nullable=True)
    chosen_k: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: The classified failure record, same shape ``core.failures.classify``
    #: produces, so the frontend renders a run failure like any other.
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=naive_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=naive_now, onupdate=naive_now
    )

    project: Mapped["Project"] = relationship(back_populates="run_rows")
    ortho: Mapped["Ortho | None"] = relationship()
    labels: Mapped[list["ClusterLabel"]] = relationship(back_populates="run")


class ClusterLabel(Base):
    __tablename__ = "cluster_labels"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), index=True)
    #: Which run these labels describe. Nullable only so the ADD COLUMN
    #: migration can land on an existing table; every new row sets it.
    #: Labels are no longer deleted when a run is archived — that deletion was
    #: what made it impossible to come back and finish an earlier run.
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("runs.id"), nullable=True, index=True
    )
    chosen_k: Mapped[int] = mapped_column(Integer)
    cluster_id: Mapped[int] = mapped_column(Integer)
    species: Mapped[str] = mapped_column(String)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    project: Mapped["Project"] = relationship(back_populates="labels")
    run: Mapped["Run | None"] = relationship(back_populates="labels")
