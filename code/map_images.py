"""The two pictures of a mapped site: the species map, and light previews.

A run leaves a detection overlay (crowns outlined on the orthomosaic) and, once
finalized, a KMZ. The KMZ needs Google Earth, so there was nothing a browser
could show to say "this is the site you mapped and this is how it came out".
``render_species_map`` draws that picture: the same crowns, filled by species,
with a legend.

Both the pipeline and the API call it, for the reason ``crown_thumbs`` exists:
the pipeline draws the map when a run is finalized, and the API draws it on
first view for a run finalized before this existed. One function, so the two
cannot disagree.

This module deliberately imports nothing from ``app``. The pipeline runs outside
the web service as well.
"""
import glob
import os
import threading

#: Longest edge of the raster the species map is drawn on. The orthomosaic can
#: be gigapixels; this is a picture for a screen, not a product.
MAX_RASTER_SIDE = 2400


def kml_to_rgba(color: str) -> tuple:
    """Turn a KML ``AABBGGRR`` colour into the (r, g, b, a) matplotlib wants."""
    c = str(color).strip().lstrip("#")
    a, b, g, r = (int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4, 6))
    return (r, g, b, a)


def species_colors(species, palette) -> dict:
    """Colour per species, in the order ``step4_export_kmz`` assigns them:
    alphabetical, ``unlabelled`` last, palette wrapping round. The picture and
    the KMZ then agree on which colour is which species."""
    names = sorted(set(species))
    if "unlabelled" in names:
        names.remove("unlabelled")
        names.append("unlabelled")
    return {sp: kml_to_rgba(palette[i % len(palette)]) for i, sp in enumerate(names)}


def find_base_raster(work_dir: str):
    """The raster to draw a run's map on, or None.

    Prefers the downsampled copy detection ran on: it is small, and it shares
    the orthomosaic's CRS and extent, so the polygons land in the same place.
    Falls back to the run's full-resolution orthomosaic, which is read
    decimated.
    """
    for pattern in (os.path.join(work_dir, "detectree", "*", "downsampled.tif"),
                    os.path.join(work_dir, "ortho", "*.tif"),
                    os.path.join(work_dir, "ortho", "*.tiff")):
        hits = sorted(glob.glob(pattern))
        if hits:
            return hits[0]
    return None


def _atomic_path(dst: str) -> str:
    # Unique per writer, so two requests drawing the same picture at once do
    # not write into each other's file. The extension is kept: both matplotlib
    # and Pillow pick the format from it.
    stem, ext = os.path.splitext(dst)
    return f"{stem}.{os.getpid()}.{threading.get_ident()}.part{ext}"


def render_species_map(gdf, raster_path: str, dst_png: str, palette,
                       overwrite: bool = False) -> bool:
    """Draw ``gdf``'s crowns over ``raster_path``, filled by its ``species``
    column, and save it to ``dst_png``. True if the file is now there.

    ``overwrite`` is for the pipeline, whose labels may have changed since the
    last export; the API leaves it off and reuses what is on disk.

    Never raises. The map is a convenience next to the KMZ and the CSVs, so a
    raster that will not read must not fail the run that produced them.
    """
    try:
        if os.path.exists(dst_png) and not overwrite:
            return True
        if gdf is None or len(gdf) == 0 or not raster_path:
            return False

        import numpy as np
        import rasterio
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.patches as mpatches

        with rasterio.open(raster_path) as src:
            scale = min(1.0, MAX_RASTER_SIDE / max(src.width, src.height))
            h = max(1, int(src.height * scale))
            w = max(1, int(src.width * scale))
            n = min(3, src.count)
            img = src.read(list(range(1, n + 1)), out_shape=(n, h, w))
            bounds, crs = src.bounds, src.crs
        img = np.transpose(img, (1, 2, 0))
        if img.dtype != np.uint8:
            # Whatever the camera recorded, stretched to what a screen shows.
            img = img.astype("float32")
            mn, mx = float(np.nanmin(img)), float(np.nanmax(img))
            if mx > mn:
                img = (img - mn) / (mx - mn) * 255.0
            img = np.nan_to_num(img).astype("uint8")
        if img.shape[2] == 1:
            img = np.repeat(img, 3, axis=2)

        if crs is not None and gdf.crs is not None and gdf.crs != crs:
            gdf = gdf.to_crs(crs)
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty]
        species = gdf["species"].fillna("unlabelled")
        colors = species_colors(species, palette)

        # Through the figure object, never pyplot's current figure: that one is
        # shared by the whole process and concurrent jobs would save each
        # other's plots.
        fig, ax = plt.subplots(figsize=(10, 10 * h / w))
        try:
            ax.imshow(img, extent=[bounds.left, bounds.right, bounds.bottom, bounds.top])
            gdf.plot(ax=ax, color=[colors[s] for s in species],
                     edgecolor="black", linewidth=0.4)
            ax.set_xlim(bounds.left, bounds.right)
            ax.set_ylim(bounds.bottom, bounds.top)
            ax.axis("off")
            counts = species.value_counts()
            ax.legend(
                handles=[
                    mpatches.Patch(facecolor=colors[sp][:3], edgecolor="black",
                                   label=f"{sp.replace('_', ' ')} ({int(counts[sp])})")
                    for sp in colors
                ],
                loc="upper left", bbox_to_anchor=(1.01, 1.0), frameon=False,
                title="Species (crowns)",
            )
            os.makedirs(os.path.dirname(dst_png), exist_ok=True)
            tmp = _atomic_path(dst_png)
            fig.savefig(tmp, dpi=200, bbox_inches="tight")
        finally:
            plt.close(fig)
        os.replace(tmp, dst_png)
        return True
    except Exception as e:
        print(f"  species map not drawn: {e}")
        return False


def write_preview(src_img: str, dst_jpg: str, max_side: int = 1600) -> bool:
    """Write a screen-sized JPEG of ``src_img``. True if the file is now there.

    The overlay is saved at print resolution and runs past 10 MB, which is a
    long wait for a picture shown in a column of a web page. Never raises: the
    caller serves the original when this returns False.
    """
    try:
        if (os.path.exists(dst_jpg)
                and os.path.getmtime(dst_jpg) >= os.path.getmtime(src_img)):
            return True
        from PIL import Image

        with Image.open(src_img) as im:
            im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            tmp = _atomic_path(dst_jpg)
            im.save(tmp, format="JPEG", quality=85)
        os.replace(tmp, dst_jpg)
        return True
    except Exception:
        return False
