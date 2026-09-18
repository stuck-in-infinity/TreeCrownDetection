"""Which request paths are orchestrator callbacks.

Two separate questions, kept separate:

``is_callback_path``
    Must this response keep the shape the DAGs expect? Used by
    ``main.audit_requests`` to withhold the ``X-Request-Id`` header. Loose:
    matching one route too many only costs a header.

``is_service_callback_path``
    May an anonymous caller be accepted as the orchestrator here? Used by
    ``deps.service_caller``. Exact: matching one route too many hands that
    route's ownership check away. The human trigger routes
    (``/projects/{id}/runs/analyze``, ``/project/runs/analyze``) are not
    callbacks.

Imports nothing, so both the middleware and the dependencies can use it.
"""

_CALLBACK_PATH_PREFIXES = (
    "/api/v1/compute",
    "/api/v1/project/drone_api",
    "/api/v1/project/drone_status",
    "/api/v1/project/analyze",
    "/api/v1/project/finalize",
)

# The paths a DAG POSTs back on. /project/drone_status is absent: it is polled
# by the browser, which is signed in.
_SERVICE_CALLBACK_PATHS = frozenset({
    "/api/v1/compute/analyze",
    "/api/v1/compute/finalize",
    "/api/v1/project/drone_api",
    "/api/v1/project/analyze",
    "/api/v1/project/finalize",
})


def is_callback_path(path: str) -> bool:
    """A path whose response shape the orchestrator depends on."""
    if path.startswith(_CALLBACK_PATH_PREFIXES):
        return True
    # /api/v1/projects/{id}/analyze and /finalize are callbacks as well.
    return path.startswith("/api/v1/projects/") and (
        path.endswith("/analyze") or path.endswith("/finalize")
    )


def is_service_callback_path(path: str) -> bool:
    """A path an unauthenticated orchestrator may be trusted on."""
    path = path.rstrip("/") or path
    if path in _SERVICE_CALLBACK_PATHS:
        return True
    # /api/v1/projects/{id}/analyze and /finalize, nothing deeper, so
    # /projects/{id}/runs/analyze (the human trigger) is excluded.
    parts = path.split("/")
    return (
        len(parts) == 6
        and parts[:4] == ["", "api", "v1", "projects"]
        and parts[5] in ("analyze", "finalize")
    )
