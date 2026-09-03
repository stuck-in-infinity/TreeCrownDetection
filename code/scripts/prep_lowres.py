"""Prepare the low-res Mission orthomosaics for the detectree2 pipeline.

As delivered, these rasters have two problems:

  1. They have four bands, RGBA, rather than three. detectree2's tiling writes
     out whatever band count it reads, so the tiles come out RGBA too, and the
     alpha channel then either reaches the network as a fourth plane or is
     dropped, depending on the reader. This script removes it.

  2. `nodata` is None, even though 18-37% of each image lies outside the flight
     path and is stored as RGB=(0,0,0). detectree2's `nan_threshold` check adds
     the bands and tests for 0, so black pixels are caught, but the alpha
     channel adds a constant 255 to every valid pixel, which breaks the
     matching white-saturation test for 765. Removing alpha fixes that as well.
     The script also sets nodata=0, so rasterio and GDAL report the mask
     correctly for anything downstream, such as overlays and area statistics.

Nothing else changes: the CRS, transform, resolution and the pixel values of
bands 1 to 3 are copied exactly. The output is tiled and LZW-compressed, so it
reads faster than the source during tiling.

Usage:
    python code/scripts/prep_lowres.py low-res/ low-res-rgb/
"""

import os
import sys
import glob

import rasterio
from rasterio.enums import Resampling  # noqa: F401  (kept for overview step)


def prep_one(src_path: str, dst_path: str) -> dict:
    with rasterio.open(src_path) as src:
        if src.count < 3:
            raise ValueError(f"{src_path}: expected >=3 bands, got {src.count}")

        meta = src.meta.copy()
        meta.update(
            count=3,
            nodata=0,
            driver="GTiff",
            compress="LZW",
            tiled=True,
            blockxsize=512,
            blockysize=512,
            BIGTIFF="IF_SAFER",
        )

        info = {
            "src_bands": src.count,
            "gsd_m": abs(src.transform.a),
            "crs": str(src.crs),
            "width": src.width,
            "height": src.height,
        }

        with rasterio.open(dst_path, "w", **meta) as dst:
            # Copy a window at a time, so the whole 21496 x 14611 raster is
            # never held in memory at once.
            for _, window in src.block_windows(1):
                dst.write(src.read([1, 2, 3], window=window), window=window)

            dst.set_band_description(1, "red")
            dst.set_band_description(2, "green")
            dst.set_band_description(3, "blue")

    return info


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2

    in_dir, out_dir = sys.argv[1], sys.argv[2]
    os.makedirs(out_dir, exist_ok=True)

    # The `._Mission*.tif` files in low-res/ are macOS resource forks, not
    # rasters, so leave them out; rasterio raises an error on them.
    paths = sorted(
        p for p in glob.glob(os.path.join(in_dir, "*.tif"))
        if not os.path.basename(p).startswith("._")
    )
    if not paths:
        print(f"no .tif found in {in_dir}")
        return 1

    for p in paths:
        dst = os.path.join(out_dir, os.path.basename(p))
        if os.path.exists(dst):
            print(f"skip (exists): {dst}")
            continue
        info = prep_one(p, dst)
        print(
            f"{os.path.basename(p):<38} "
            f"{info['src_bands']}band -> 3band  "
            f"{info['gsd_m'] * 100:.2f} cm/px  {info['crs']}  "
            f"{info['width']}x{info['height']}"
        )

    print(f"\ndone -> {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
