"""Turn a pipeline exception into a message a surveyor can act on.

``classify(exc, stage=...)`` matches the exception against the failure modes
this pipeline actually has and returns a dict with

    code     - a specific machine code rather than COMPUTE_FAILED
    message  - one plain sentence, no library jargon
    hint     - what to change, naming the setting where there is one
    stage    - the pipeline stage that was running
    details  - the raw exception type and text, for engineers
"""
from __future__ import annotations

import errno
import re

# Code used when no rule matches.
GENERIC = "COMPUTE_FAILED"


def _has_errno(exc: BaseException, number: int) -> bool:
    return isinstance(exc, OSError) and getattr(exc, "errno", None) == number


def _type_chain(exc: BaseException) -> str:
    """Exception class names down the __cause__/__context__ chain, lowercased.

    Libraries wrap their errors: rasterio raises its own error around an
    OSError, and torch wraps CUDA failures. Matching only ``type(exc).__name__``
    would miss those.
    """
    names, seen, cur = [], set(), exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        names.append(type(cur).__name__.lower())
        cur = cur.__cause__ or cur.__context__
    return " ".join(names)


def _full_text(exc: BaseException) -> str:
    """Message text down the same chain, lowercased."""
    parts, seen, cur = [], set(), exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        parts.append(str(cur))
        cur = cur.__cause__ or cur.__context__
    return " ".join(parts).lower()


# Each rule is (code, match(exc, text, types) -> bool, message, hint).
# `message` and `hint` may instead be callables taking (exc, text), for details
# only known at failure time such as which file was missing.
_RULES: list[tuple] = [
    (
        "GPU_OUT_OF_MEMORY",
        lambda e, t, ty: (
            "cuda out of memory" in t
            or "outofmemoryerror" in ty
            or ("cudnn" in t and "not enough memory" in t)
        ),
        "The GPU ran out of memory part-way through this run.",
        "Lower Tile size in step 3 (each tile is processed whole, so it sets the "
        "peak memory), or wait until no other run is using the GPU and try again.",
    ),
    (
        "DISK_FULL",
        lambda e, t, ty: _has_errno(e, errno.ENOSPC) or "no space left on device" in t,
        "The server ran out of disk space while writing this run's output.",
        "Tell an administrator — the storage volume is full. Nothing you change "
        "in the settings will get past this.",
    ),
    (
        "RASTER_UNREADABLE",
        lambda e, t, ty: (
            "not recognized as a supported file format" in t
            or "not recognized as being in a supported file format" in t
            or ("rasterioioerror" in ty)
            or ("tiffreaddirectory" in t)
        ),
        "The orthomosaic could not be opened as an image.",
        "The file is most likely incomplete or corrupted — a download that was "
        "cut short looks exactly like this. Upload it again, and prefer the "
        "Google Drive option for very large files.",
    ),
    (
        "MODEL_WEIGHTS_MISSING",
        lambda e, t, ty: (
            isinstance(e, FileNotFoundError) or "no such file" in t
        ) and re.search(r"\.(pth|pt|ckpt)\b", t) is not None,
        "The detector's weight file is not on the server.",
        "Pick a different detector model in step 3. If every model reports this, "
        "the server's models directory is not mounted — tell an administrator.",
    ),
    (
        "NO_CRS",
        lambda e, t, ty: (
            "crs" in t and any(w in t for w in ("none", "missing", "no crs", "undefined"))
        ),
        "The orthomosaic carries no coordinate reference system, so the output "
        "could not be placed on the map.",
        "Enter the EPSG code for your survey in step 3 — the field appears under "
        "the orthomosaic once the server detects this.",
    ),
    (
        "NO_CROWNS_DETECTED",
        lambda e, t, ty: (
            "no crowns" in t
            or "empty geodataframe" in t
            or ("0 features" in t and "crown" in t)
            or ("cannot" in t and "empty" in t and "cluster" in t)
        ),
        "The detector found no tree crowns in this orthomosaic, so there was "
        "nothing to group or name.",
        "Lower Confidence threshold in step 3, and check Tile size suits the "
        "image resolution — the Guide's \"How big does a tree look?\" section "
        "works this out for your survey.",
    ),
    (
        "TOO_FEW_CROWNS_FOR_K",
        lambda e, t, ty: (
            "n_samples" in t and "n_clusters" in t
        ) or "should be >= n_clusters" in t,
        "There were fewer tree crowns than the number of groups the run was "
        "asked to sort them into.",
        "Lower the candidate cluster counts (k) in step 3, or run on a larger "
        "area — you cannot sort 8 crowns into 20 groups.",
    ),
    (
        "MISSING_ARTIFACT",
        lambda e, t, ty: isinstance(e, FileNotFoundError) or "no such file or directory" in t,
        lambda e, t: (
            "A file this stage expected was not there: "
            + (_missing_path(e) or "an intermediate output")
            + "."
        ),
        "The previous stage did not produce what this one needed, so the run "
        "stopped rather than carry on with a gap. The run log holds the stage "
        "that actually failed — send it with the request id below.",
    ),
    (
        "DEPENDENCY_MISSING",
        lambda e, t, ty: isinstance(e, ImportError) or "modulenotfounderror" in ty,
        lambda e, t: (
            "The server is missing a Python package this stage needs"
            + (f" ({getattr(e, 'name', None)})" if getattr(e, "name", None) else "")
            + "."
        ),
        "This is a server build problem, not a setting — tell an administrator "
        "and quote the request id below.",
    ),
    (
        "PERMISSION_DENIED",
        lambda e, t, ty: _has_errno(e, errno.EACCES) or "permission denied" in t,
        "The server was not allowed to read or write one of this run's files.",
        "A storage permission problem on the server — tell an administrator. "
        "Re-running will not change it.",
    ),
    (
        "COMPUTE_KILLED",
        lambda e, t, ty: (
            "killed" in t and "signal" in t
        ) or "sigkill" in t or "exitcode -9" in t or "returned non-zero exit status -9" in t,
        "The run was stopped by the operating system, which almost always means "
        "the server ran out of memory.",
        "Try a smaller orthomosaic or a smaller Tile size. If it keeps "
        "happening on a normal-sized survey, tell an administrator — the "
        "machine may be under-provisioned.",
    ),
]


def _missing_path(exc: BaseException) -> str | None:
    """The filename out of a FileNotFoundError, basename only.

    Full server paths are not shown to users: they expose the storage layout and
    mean nothing to a forestry team. The basename still says which artifact went
    missing, which is the part that helps.
    """
    name = getattr(exc, "filename", None)
    if not name:
        m = re.search(r"No such file or directory:\s*'([^']+)'", str(exc))
        name = m.group(1) if m else None
    if not name:
        return None
    base = str(name).replace("\\", "/").rstrip("/").split("/")[-1]
    return base or None


def classify(exc: BaseException, stage: str | None = None) -> dict:
    """Interpret a pipeline exception. Never raises, since the failure path
    itself must not fail.

    Returns the dict stored in ``Project.error`` and served as ``last_error``.
    """
    try:
        text = _full_text(exc)
        types = _type_chain(exc)
        for code, match, message, hint in _RULES:
            try:
                hit = match(exc, text, types)
            except Exception:              # noqa: BLE001 - a broken rule must not hide the real error
                hit = False
            if hit:
                return _build(code,
                              message(exc, text) if callable(message) else message,
                              hint(exc, text) if callable(hint) else hint,
                              exc, stage)
        # No rule matched. Name the exception type and the stage, so the message
        # at least says where the run stopped.
        return _build(
            GENERIC,
            f"The run stopped during {stage or 'processing'} with an unexpected "
            f"{type(exc).__name__}.",
            "This one is not a known failure mode. Send the request id below and "
            "the run log to whoever maintains the server.",
            exc, stage,
        )
    except Exception:                      # noqa: BLE001 - last resort
        return {"code": GENERIC, "stage": stage,
                "message": "The run failed.", "hint": None,
                "details": {"exception": type(exc).__name__}}


def _build(code, message, hint, exc, stage) -> dict:
    raw = str(exc).strip()
    return {
        "code": code,
        "stage": stage,
        "message": message,
        "hint": hint,
        "details": {
            # Kept verbatim. The classification above is an interpretation and
            # sits on top of these values, it does not replace them.
            "exception": type(exc).__name__,
            "raw": raw[:2000] if raw else None,
        },
    }
