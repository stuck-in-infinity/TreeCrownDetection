"""Turn a crown GeoTIFF into a PNG a browser can show.

Crown crops are written as GeoTIFFs, which no browser will display, so both the
pipeline and the API have to convert them. They live here together because the
two must produce the same picture: the pipeline renders thumbnails ahead of time
for the clusters the user is about to look at, and the API renders anything the
pipeline missed, on demand. If the normalisation drifted apart, the same crown
would look different depending on which side happened to draw it.

This module deliberately imports nothing from ``app``. The pipeline runs outside
the web service as well, and pulling the FastAPI package in through the back door
would break that.
"""
import io
import os


def tif_to_png_bytes(path: str, max_side: int | None = None):
    """Render one crown GeoTIFF as PNG bytes, or None if it cannot be read.

    Crown crops are small and their pixel values are whatever the survey camera
    recorded, so the image is stretched from its own minimum to its own maximum
    rather than assumed to be 0-255. ``max_side`` shrinks the longest edge, for
    thumbnails.

    Returns a ``BytesIO`` positioned at the start, ready to stream or write.
    Returns None when the imaging libraries are missing or the file will not
    read — the caller decides whether that is fatal.
    """
    try:
        import numpy as np
        import rasterio
        from PIL import Image

        with rasterio.open(path) as src:
            n = min(3, src.count)
            arr = src.read(list(range(1, n + 1)))
        arr = np.transpose(arr, (1, 2, 0)).astype("float32")
        mn, mx = float(np.nanmin(arr)), float(np.nanmax(arr))
        if mx > mn:
            arr = (arr - mn) / (mx - mn) * 255.0
        arr = np.nan_to_num(arr).astype("uint8")
        if arr.shape[2] == 1:
            arr = arr[:, :, 0]

        img = Image.fromarray(arr)
        if max_side and max(img.size) > max_side:
            # thumbnail() keeps the aspect ratio and never enlarges.
            img.thumbnail((max_side, max_side))

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception:
        return None


def write_thumbnail(src_tif: str, dst_png: str, max_side: int = 200) -> bool:
    """Render ``src_tif`` to ``dst_png``. True if the file is now there.

    Never raises. A thumbnail is a convenience — the API falls back to rendering
    on demand — so a crown that will not convert must not take down the run that
    produced it.
    """
    try:
        if os.path.exists(dst_png):
            return True
        buf = tif_to_png_bytes(src_tif, max_side=max_side)
        if buf is None:
            return False
        os.makedirs(os.path.dirname(dst_png), exist_ok=True)
        # Write to a neighbouring temp name first, so a reader never catches a
        # half-written PNG if this is running while somebody is browsing.
        tmp = dst_png + ".part"
        with open(tmp, "wb") as f:
            f.write(buf.getvalue())
        os.replace(tmp, dst_png)
        return True
    except Exception:
        return False
