from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.models_registry import default_backbone


class PipelineParams(BaseModel):
    """User-tunable knobs; mirror the pipeline's Config fields.

    Bounds exist so a bad value fails at trigger time with a readable 400
    rather than deep inside detectron2 after tiling has already run. They are
    enforced by ``_validate_param_overrides`` (projects.py), which rebuilds
    each field's annotation together with its FieldInfo — the constraints are
    NOT carried by the bare annotation.

    Defaults match what these values were hardcoded to before they were lifted
    into parameters, so exposing them changed no existing behaviour.
    """

    # detection (Step 0)
    tile_size: int = Field(default=10, ge=1, le=1000)
    buffer: int = Field(default=10, ge=0, le=500)
    iou_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    conf_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    # Cap on crowns returned per tile. Anything beyond it is discarded with no
    # warning, so it must exceed the densest tile expected. detectron2's own
    # default is 100; this pipeline shipped with 6.
    detections_per_image: int = Field(default=6, ge=1, le=500)
    # Size every tile is resized to before inference. Together with the tile
    # footprint it sets crown size at the network:
    #   pixels_per_metre = min_size_test / (tile_size + 2 * buffer)
    min_size_test: int = Field(default=512, ge=256, le=2048)
    # Crown area filter, in the ortho CRS's units (m2 for UTM). Exclusive.
    area_min: float = Field(default=4.0, ge=0.0, le=100000.0)
    area_max: float = Field(default=2000.0, ge=1.0, le=100000.0)
    # False skips the right/bottom remainder strip of the ortho.
    full_coverage: bool = False
    # features + clustering (Step 1)
    k_list: list[int] = Field(default_factory=lambda: [2, 4, 6, 8, 10])
    pca_components: int | None = Field(default=50, ge=2, le=768)
    # Bounded by the deployment's GPU VRAM, so deliberately NOT exposed in the UI
    # — raising it OOMs the run. Still accepted here so stored project params stay
    # valid and an operator can override it via the API.
    batch_size: int = 64
    # Fixed by the chosen DINOv2 backbone (models.yaml img_size); the frontend
    # sends the selected backbone's value rather than asking the user.
    img_size: int = 224
    model_name: str = Field(default_factory=default_backbone)

    @model_validator(mode="after")
    def _check_cross_field(self):
        """Relationships the per-field bounds cannot see.

        Must be run against the MERGED params, not just the overrides: sending
        area_min alone still has to be checked against the stored area_max.
        See ``_validate_trigger_body`` in runs.py.
        """
        if self.area_min >= self.area_max:
            raise ValueError(
                f"area_min ({self.area_min}) must be smaller than "
                f"area_max ({self.area_max})"
            )
        return self


class ProjectCreate(BaseModel):
    """Project creation takes only a display name (v5): the UUID is generated
    server-side and returned as the reference for every other endpoint. Model +
    parameter configuration moved to the analyze trigger. model_key /
    source_epsg / params are still accepted for backwards compatibility."""

    name: str = ""
    model_key: str | None = None          # None -> server default (urban_cambridge)
    source_epsg: int | None = None        # None -> auto-detected from the GeoTIFF
    params: PipelineParams = Field(default_factory=PipelineParams)


class AnalyzeTrigger(BaseModel):
    """Optional body for POST /runs/analyze: name this run and configure the
    detector / feature extractor / pipeline params in the same call. params is
    merged onto the project's existing params (not replaced wholesale)."""

    action: str | None = None
    run_name: str | None = None
    project_id: str | None = None
    model_key: str | None = None
    source_epsg: int | None = None
    params: dict | None = None
    # Which orthomosaic in the project's library this run should use. Optional:
    # omitted with exactly one ortho means "that one" (every pre-multi-ortho
    # client keeps working); omitted with several is rejected 400
    # ORTHO_SELECTION_REQUIRED. Persisted onto ``project.params`` by
    # ``_apply_run_config`` so the worker and the run history both see it.
    ortho_id: str | None = None
    execution_id: str | None = None  # set by Airflow callbacks — triggers direct execution


class FinalizeTrigger(BaseModel):
    """Optional body for POST /runs/finalize: identify the project explicitly."""

    action: str | None = None
    project_id: str | None = None


class ProjectUpdate(BaseModel):
    """Partial update for a re-run: change params (and optionally model/EPSG) and
    open the next run on the same uploaded ortho. All fields optional; params is
    merged onto the existing params, not replaced wholesale."""

    model_key: str | None = None
    source_epsg: int | None = None
    params: dict | None = None
    run_name: str | None = None


class OrthoFromUrl(BaseModel):
    """Request body for registering an ortho from a public Google Drive link."""

    url: str


class OrthoOut(BaseModel):
    """One orthomosaic in the project's library.

    ``stem`` is the ortho's identity within the project AND its on-disk name
    (``input/ortho/<stem>.tif``); it is de-duplicated on upload, so it may carry
    a ``_2`` / ``_3`` suffix. ``filename`` is the name the user uploaded, kept
    unchanged for display. ``id`` and ``size_bytes`` are additive fields — older
    clients that ignore them are unaffected.
    """

    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    stem: str
    filename: str
    width: int | None = None
    height: int | None = None
    crs: str | None = None
    bands: int | None = None
    size_bytes: int | None = None


class ProjectOut(BaseModel):
    project_id: str
    name: str
    model_key: str
    state: str
    source_epsg: int | None = None
    params: dict
    recommended_k: int | None = None
    available_k: list[int] | None = None
    current_run: int = 1
    run_name: str | None = None
    runs: list = []
    orthos: list[OrthoOut] = []
    error: str | None = None
    last_error: dict | None = None
    files_url: str | None = None
    created_at: datetime
    updated_at: datetime
