import os
import glob
import shutil
import rasterio
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")          # no display and no GUI loop; jobs run off-thread
import matplotlib.pyplot as plt

from rasterio.enums import Resampling

from detectree2.preprocessing.tiling import tile_data
from detectree2.models.train import setup_cfg
from detectree2.models.predict import predict_on_data
from detectree2.models.outputs import project_to_geojson, stitch_crowns, clean_crowns

from detectron2.engine import DefaultPredictor
import torch



# Downsampling the orthomosaic.

def downsample_image(input_path, output_path, scale=1):
    with rasterio.open(input_path) as src:
        new_width = int(src.width * scale)
        new_height = int(src.height * scale)

        data = src.read(
            out_shape=(src.count, new_height, new_width),
            resampling=Resampling.bilinear
        )

        transform = src.transform * src.transform.scale(
            (src.width / new_width),
            (src.height / new_height)
        )

        profile = src.profile
        profile.update({
            "height": new_height,
            "width": new_width,
            "transform": transform
        })

        with rasterio.open(output_path, "w", **profile) as dst:
            dst.write(data)

    return output_path


def get_ortho_gsd(ortho_path):
    """Read the ground sample distance, in metres per pixel, from the raster's
    own geotransform. Nothing is assumed or measured; this is what the file
    already records."""
    with rasterio.open(ortho_path) as src:
        return abs(src.transform.a)


# The resolution, after downsampling, that detection is tuned for. It comes
# from the Sanjay Van orthomosaic at a downsample of 0.3, the setting known to
# detect well there, using its real GSD as measured by rasterio, 2.04 cm/px,
# rather than the nominal 2.5 cm. Using the nominal figure overblurred Sanjay
# Van once this calculation became adaptive: its effective resolution went from
# 6.8 cm to 8.33 cm, which the "Native GSD" log line confirmed. Every other
# orthomosaic's downsample scale is worked out from this target instead of
# applying a flat 0.3 whatever its native resolution.
TARGET_EFFECTIVE_GSD_M = 0.02 / 0.3


def compute_downsample_scale(ortho_path, target_gsd_m=TARGET_EFFECTIVE_GSD_M):
    """Choose a downsample scale that brings this orthomosaic's resolution to
    target_gsd_m, whatever its native resolution. The scale never exceeds 1.0,
    so the image is not upsampled past the real data."""
    native_gsd = get_ortho_gsd(ortho_path)
    scale = min(1.0, native_gsd / target_gsd_m)
    effective_gsd = native_gsd / scale
    print(
        f"  Native GSD: {native_gsd * 100:.2f} cm/px -> downsample scale: {scale:.3f} "
        f"-> effective GSD: {effective_gsd * 100:.2f} cm/px "
        f"(target: {target_gsd_m * 100:.2f} cm/px)"
    )
    return scale


def resolve_ortho_path(ortho_path):
    """Resolve the input path. If it names a folder, take the first image in it."""
    if os.path.isdir(ortho_path):
        candidates = sorted(
            glob.glob(os.path.join(ortho_path, "*.tif"))
            + glob.glob(os.path.join(ortho_path, "*.tiff"))
        )
        if not candidates:
            raise FileNotFoundError(
                f"No .tif / .tiff images found in directory: {ortho_path}"
            )
        if len(candidates) > 1:
            print(
                f"⚠️ Multiple orthomosaic images found in {ortho_path}; using the first one: {candidates[0]}"
            )
        return candidates[0]
    return ortho_path


# Building the model. It is built once and reused across orthomosaics.

# Defaults for values that used to be hardcoded inside build_predictor and
# run_detectree2_pipeline. They keep their original values, so turning them into
# parameters changed no behaviour. docs/lowres_run_params.md says what to raise
# them to for lower-resolution imagery.
DEFAULT_DETECTIONS_PER_IMAGE = 6      # detectron2 itself defaults to 100
DEFAULT_MIN_SIZE_TEST = 512           # detectree2 predicts at around 1000
DEFAULT_AREA_MIN = 4                  # square metres, in the ortho's CRS units
DEFAULT_AREA_MAX = 2000               # square metres
DEFAULT_FULL_COVERAGE = False         # True also tiles the right and bottom edges


def build_predictor(
    model_path,
    conf_threshold=0.85,
    detections_per_image=DEFAULT_DETECTIONS_PER_IMAGE,
    min_size_test=DEFAULT_MIN_SIZE_TEST,
):
    """Build a detectron2 ``DefaultPredictor`` from detectree2 weights.

    It is separate from the pipeline so the worker can build one per process and
    reuse it for every orthomosaic; see app/workers/tasks.py.

    ``detections_per_image`` is the most crowns a tile may return. Anything past
    it is dropped silently, so the value has to be higher than the densest tile
    you expect. ``min_size_test`` is the size every tile is resized to before
    inference, which sets how large a crown looks to the network:
    ``pixels_per_metre = min_size_test / (tile_size + 2 * buffer)``.

    Callers that cache predictors should note that both arguments are fixed into
    the object returned, so they belong in the cache key along with model_path
    and conf_threshold.
    """
    cfg = setup_cfg(update_model=model_path)

    if torch.backends.mps.is_available():
        cfg.MODEL.DEVICE = "mps"
    elif torch.cuda.is_available():
        cfg.MODEL.DEVICE = "cuda"
    else:
        cfg.MODEL.DEVICE = "cpu"

    cfg.MODEL.ROI_HEADS.SCORE_THRESH_TEST = conf_threshold
    cfg.MODEL.ROI_HEADS.DETECTIONS_PER_IMAGE = int(detections_per_image)

    # Tiles are square, so the minimum and maximum are the same and detectron2
    # never adjusts for aspect ratio. Keep them equal.
    cfg.INPUT.MIN_SIZE_TEST = int(min_size_test)
    cfg.INPUT.MAX_SIZE_TEST = int(min_size_test)

    return DefaultPredictor(cfg)


# The detection pipeline.

def run_detectree2_pipeline(
    ortho_path,
    predictor=None,
    output_dir="output",
    tile_size=10,
    buffer=10,
    iou_threshold=0.9,
    conf_threshold=0.85,
    model_path=None,
    target_gsd_m=TARGET_EFFECTIVE_GSD_M,
    area_min=DEFAULT_AREA_MIN,
    area_max=DEFAULT_AREA_MAX,
    full_coverage=DEFAULT_FULL_COVERAGE,
    detections_per_image=DEFAULT_DETECTIONS_PER_IMAGE,
    min_size_test=DEFAULT_MIN_SIZE_TEST,
):
    """Detect tree crowns in one orthomosaic and write the results.

    ``detections_per_image`` and ``min_size_test`` are only used when this
    function builds its own predictor, that is when ``predictor`` is None. If a
    predictor is passed in, those values are already fixed into it and the
    arguments here are ignored, so the caller is responsible for them.
    """

    os.makedirs(output_dir, exist_ok=True)

    tiles_dir = os.path.join(output_dir, "tiles")
    pred_dir = os.path.join(output_dir, "predictions")

    # Remove anything left from a previous run.
    if os.path.exists(tiles_dir):
        shutil.rmtree(tiles_dir)
    if os.path.exists(pred_dir):
        shutil.rmtree(pred_dir)

    os.makedirs(tiles_dir, exist_ok=True)
    os.makedirs(pred_dir, exist_ok=True)

    # Step 0: downsample the orthomosaic.
    ortho_path = resolve_ortho_path(ortho_path)

    print("Step 0: Downsampling image...")
    scale = compute_downsample_scale(ortho_path, target_gsd_m=target_gsd_m)
    ortho_path = downsample_image(ortho_path, os.path.join(output_dir, "downsampled.tif"), scale=scale)

    # Step 1: cut the orthomosaic into tiles.
    print("Step 1: Tiling orthomosaic...")

    tile_data(
        ortho_path,
        tiles_dir,
        buffer=buffer,
        tile_width=tile_size,
        tile_height=tile_size,
        threshold=0.05,
        nan_threshold=0.3,
        full_coverage=bool(full_coverage),
    )

    tiles = [f for f in os.listdir(tiles_dir) if f.endswith(".png") or f.endswith(".tif")]

    print("Tiles created:", len(tiles))
    print("Sample tiles:", tiles[:5])

    if len(tiles) == 0:
        raise RuntimeError("❌ No tiles generated.")

    # Step 2: load the detector.
    print("Step 2: Loading model...")
    if predictor is None:
        if model_path is None:
            raise ValueError(
                "run_detectree2_pipeline needs either a prebuilt `predictor` "
                "or a `model_path` to build one."
            )
        predictor = build_predictor(
            model_path,
            conf_threshold=conf_threshold,
            detections_per_image=detections_per_image,
            min_size_test=min_size_test,
        )

    # Step 3: run detection on every tile.
    print("Step 3: Running predictions...")
    predict_on_data(directory=tiles_dir, predictor=predictor)

    # detectree2 writes into ./predictions, so move the files where we want them.
    print("Fixing prediction output path...")
    default_pred_dir = os.path.join(os.getcwd(), "predictions")

    if os.path.exists(default_pred_dir):
        for f in os.listdir(default_pred_dir):
            src = os.path.join(default_pred_dir, f)
            dst = os.path.join(pred_dir, f)
            if os.path.isfile(src):
                shutil.move(src, dst)
    else:
        raise RuntimeError("❌ No predictions folder found!")

    # Step 4: turn the per-tile predictions into GeoJSON.
    print("Step 4: Convert predictions to GeoJSON...")
    project_to_geojson(tiles_dir, pred_dir, pred_dir)

    geojson_files = glob.glob(os.path.join(pred_dir, "*.geojson"))
    print("GeoJSON files found:", len(geojson_files))

    if len(geojson_files) == 0:
        raise RuntimeError("❌ No GeoJSON generated.")

    # Step 5: join the per-tile crowns into one layer.
    print("Step 5: Stitch crowns...")
    crowns = stitch_crowns(pred_dir, 1)

    # Step 6: clean up the crowns.
    print("Step 6: Cleaning crowns...")

    if isinstance(crowns, gpd.GeoSeries):
        crowns = gpd.GeoDataFrame(geometry=crowns)

    crs = crowns.crs

    crowns = crowns[crowns.is_valid]

    # Smooth the polygon outlines.
    crowns["geometry"] = crowns.geometry.simplify(0.6)

    crowns = gpd.GeoDataFrame(crowns, geometry="geometry", crs=crs)

    # Drop crowns the detector was not confident about.
    if "Confidence_score" in crowns.columns:
        conf_col = "Confidence_score"
    else:
        conf_col = None

    if conf_col:
        crowns = crowns[crowns[conf_col] > conf_threshold]

    # Filter by crown area. The units come from the ortho's CRS, and both
    # bounds are exclusive.
    crowns["area"] = crowns.geometry.area
    crowns = crowns[
        (crowns["area"] > float(area_min)) & (crowns["area"] < float(area_max))
    ].copy()

    crowns = gpd.GeoDataFrame(crowns, geometry="geometry", crs=crs)

    # Remove overlapping duplicates.
    crowns = clean_crowns(crowns, iou_threshold, conf_threshold)

    # Save the crowns.
    geojson_path = os.path.join(output_dir, "tree_crowns.geojson")
    crowns.to_file(geojson_path, driver="GeoJSON")

    print(f"GeoJSON saved: {geojson_path}")

    # Step 7: draw the crowns over the orthomosaic.
    print("Step 7: Creating overlay image...")

    with rasterio.open(ortho_path) as src:
        img = src.read([1, 2, 3])
        img = img.transpose(1, 2, 0)

        bounds = src.bounds
        extent = [bounds.left, bounds.right, bounds.bottom, bounds.top]

    fig, ax = plt.subplots(figsize=(10, 10))
    ax.imshow(img, extent=extent)

    crowns.plot(ax=ax, facecolor="none", edgecolor="red", linewidth=0.8)

    ax.set_xlim(bounds.left, bounds.right)
    ax.set_ylim(bounds.bottom, bounds.top)

    overlay_path = os.path.join(output_dir, "overlay.png")
    # Work through the figure object rather than pyplot's current figure, which
    # is shared by the whole process, so concurrent jobs cannot save each
    # other's plots.
    ax.axis("off")
    fig.savefig(overlay_path, dpi=300, bbox_inches="tight")
    plt.close(fig)

    print(f"Overlay saved: {overlay_path}")

    return geojson_path, overlay_path, ortho_path



if __name__ == "__main__":

    ortho_image = "data/examples/s1_tree_rgb.tif"
    model_weights = "models/250711_tropical_closed_canopy.pth"

    run_detectree2_pipeline(
        ortho_path=ortho_image,
        model_path=model_weights,
        output_dir="results"
    )
