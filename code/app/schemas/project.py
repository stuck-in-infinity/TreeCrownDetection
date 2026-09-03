from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.models_registry import default_backbone


class PipelineParams(BaseModel):
    """The settings a user can change. These mirror the pipeline's Config fields.

    The bounds are here so a bad value fails at trigger time with a readable 400
    instead of somewhere inside detectron2 after tiling has already run.
    ``_validate_param_overrides`` in projects.py applies them by rebuilding each
    field's annotation along with its FieldInfo; the annotation alone does not
    carry the constraints.

    The defaults are the values these settings were hardcoded to before they
    became parameters, so exposing them did not change any behaviour.
    """

    # Step 0: crown detection.
    tile_size: int = Field(default=10, ge=1, le=1000)
    buffer: int = Field(default=10, ge=0, le=500)
    iou_threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    conf_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    # Most crowns kept per tile. Anything past this is dropped silently, so the
    # value has to be higher than the densest tile you expect. detectron2
    # defaults to 100; this pipeline started out at 6.
    detections_per_image: int = Field(default=6, ge=1, le=500)
    # Size each tile is resized to before inference. With the tile footprint it
    # decides how large a crown looks to the network:
    #   pixels_per_metre = min_size_test / (tile_size + 2 * buffer)
    min_size_test: int = Field(default=512, ge=256, le=2048)
    # Crown area filter, in the units of the ortho's CRS, so square metres for
    # UTM. Both bounds are exclusive.
    area_min: float = Field(default=4.0, ge=0.0, le=100000.0)
    area_max: float = Field(default=2000.0, ge=1.0, le=100000.0)
    # False leaves out the leftover strip along the right and bottom edges.
    full_coverage: bool = False
    # Step 1: features and clustering.
    k_list: list[int] = Field(default_factory=lambda: [2, 4, 6, 8, 10])
    pca_components: int | None = Field(default=50, ge=2, le=768)
    # Limited by the GPU memory on the machine, so the UI does not offer it;
    # raising it runs the GPU out of memory. It is still accepted here so stored
    # project params validate and an operator can override it through the API.
    batch_size: int = 64
    # Set by the chosen DINOv2 backbone (img_size in models.yaml). The frontend
    # sends the selected backbone's value instead of asking the user.
    img_size: int = 224
    model_name: str = Field(default_factory=default_backbone)

    @model_validator(mode="after")
    def _check_cross_field(self):
        """Check the rules that involve more than one field.

        This has to run on the merged params rather than the overrides alone:
        sending only area_min still has to be checked against the stored
        area_max. See ``_validate_trigger_body`` in runs.py.
        """
        if self.area_min >= self.area_max:
            raise ValueError(
                f"area_min ({self.area_min}) must be smaller than "
                f"area_max ({self.area_max})"
            )
        return self


class ProjectCreate(BaseModel):
    """Creating a project needs only a display name. The server generates the
    UUID and returns it, and every other endpoint refers to the project by it.
    The model and parameters are chosen at the analyze trigger instead, but
    model_key, source_epsg and params are still accepted here so older clients
    keep working."""

    name: str = ""
    model_key: str | None = None          # None uses the server default
    source_epsg: int | None = None        # None reads it from the GeoTIFF
    params: PipelineParams = Field(default_factory=PipelineParams)


class AnalyzeTrigger(BaseModel):
    """Optional body for POST /runs/analyze. It names the run and picks the
    detector, feature extractor and pipeline parameters in the same call. The
    params given here are merged into the project's existing params rather than
    replacing them."""

    action: str | None = None
    run_name: str | None = None
    project_id: str | None = None
    model_key: str | None = None
    source_epsg: int | None = None
    params: dict | None = None
    # Which orthomosaic from the project's library this run should use. If the
    # project holds exactly one, leaving this out selects it, so clients written
    # before the library existed keep working. Leaving it out when the project
    # holds several is rejected with 400 ORTHO_SELECTION_REQUIRED.
    # ``_apply_run_config`` saves it onto ``project.params``, so the worker and
    # the run history both record which orthomosaic was used.
    ortho_id: str | None = None
    execution_id: str | None = None  # set by Airflow callbacks; runs the compute directly


class FinalizeTrigger(BaseModel):
    """Optional body for POST /runs/finalize, naming the project explicitly."""

    action: str | None = None
    project_id: str | None = None


class ProjectUpdate(BaseModel):
    """Body for setting up a re-run: change the parameters, and optionally the
    model or EPSG, and open the next run on the same uploaded orthomosaic. Every
    field is optional, and params is merged into the existing params rather than
    replacing them."""

    model_key: str | None = None
    source_epsg: int | None = None
    params: dict | None = None
    run_name: str | None = None


class OrthoFromUrl(BaseModel):
    """Body for adding an orthomosaic from a public Google Drive link."""

    url: str


class OrthoOut(BaseModel):
    """One orthomosaic in the project's library.

    ``stem`` identifies the orthomosaic within the project and is also its name
    on disk, at ``input/ortho/<stem>.tif``. Uploads make it unique, so it may
    end in ``_2`` or ``_3``. ``filename`` is the name the user uploaded and is
    kept as-is for display.
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
