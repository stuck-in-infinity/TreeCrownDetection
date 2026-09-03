# ════════════════════════════════════════════════════════════════════════
# Tree-Crown backend — "venv image, code mounted" build for the workstation.
#
# The image carries ONLY the virtual-env (/opt/venv) + system libs. The
# application code is NOT copied in — it is bind-mounted at /code at run time
# (see docker-compose). Data/output is bind-mounted at /data (empty initially),
# detector weights at /models (read-only).
#
# Defaults to CPU torch for portability. For an NVIDIA/CUDA build, pass
#   --build-arg TORCH_INDEX=https://download.pytorch.org/whl/cu118
# and use a CUDA base image + `--gpus all` (see README).
# ════════════════════════════════════════════════════════════════════════
FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/data/hf-cache

# System libs:
#   build-essential + git -> compile detectron2 from source
#   libgl1 libglib2.0-0   -> OpenCV/matplotlib runtime used by detectron2/detectree2
#   curl                  -> container HEALTHCHECK against /livez
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential git curl libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# ── baked virtual-env ────────────────────────────────────────────────────
RUN python -m venv /opt/venv
ENV VIRTUAL_ENV=/opt/venv PATH="/opt/venv/bin:$PATH"

# pkg_resources (imported by detectron2.model_zoo) was removed from setuptools 81+.
RUN pip install --upgrade pip && pip install "setuptools<81" wheel

# 1) Torch FIRST so the detectron2 source build links against it.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install torch==2.7.1 torchvision==0.22.1 torchaudio==2.7.1 --index-url ${TORCH_INDEX} --extra-index-url https://pypi.org/simple

# 2) API / service-layer deps (only the requirements file is copied — not the code).
COPY code/requirements-api.txt /tmp/requirements-api.txt
RUN pip install -r /tmp/requirements-api.txt

# 3) Pipeline deps (geospatial + ML support).
RUN pip install \
        timm \
        rasterio geopandas shapely fiona \
        numpy pandas scikit-learn \
        matplotlib seaborn \
        tqdm simplekml

# 4) detectron2 (pinned to the commit this project builds with) + detectree2.
# --no-build-isolation: detectron2 setup.py imports torch at build time, so it
# must build against the venv torch (not pip's isolated build env).
RUN pip install --no-build-isolation "git+https://github.com/facebookresearch/detectron2.git@e0ec4e189d438848521aee7926f9900e114229f5"
RUN pip install detectree2

# 5) Enforce the pipeline's Pillow pin LAST.
RUN pip install "Pillow==9.5.0"

# Code is mounted here at run time; nothing is COPYed.
WORKDIR /code
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
