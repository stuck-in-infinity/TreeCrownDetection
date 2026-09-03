# FastAPI application entry point.

import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

from app.core.settings import settings
from app.api.v1.router import api_router
from app.core.errors import install_error_handlers
from app.core.logging import configure_logging, get_logger, new_request_id, with_context
from app.db.session import init_db
from app.services import activity_log

log = get_logger("app.request")


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()  # set up logging before anything else runs
    init_db()            # convenient in development; production uses Alembic
    yield


app = FastAPI(
    title="Tree-Crown Species Pipeline API",
    version="0.1.0",
    description=(
        "Service around the tree-crown detection, clustering and species-mapping "
        "pipeline. The work runs as two jobs with a human labelling step between "
        "them. See API_SPECIFICATION.docx and API_DESIGN.md."
    ),
    lifespan=lifespan,
)

# Give every error the same response shape.
install_error_handlers(app)

# CORS. `allow_origins=["*"]` cannot be used together with
# `allow_credentials=True`, and this API authenticates through headers, so
# credentials are not needed.
_cors = [o.strip() for o in (settings.cors_origins or "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this, a browser cannot read X-Request-Id on a cross-origin reply.
    expose_headers=["X-Request-Id"],
)


# Methods whose calls are recorded in the activity log, using the sign-in
# headers the frontend sets.
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Paths Airflow calls back on.
_COMPUTE_PATH_PREFIXES = (
    "/api/v1/compute",
    "/api/v1/project/drone_api",
    "/api/v1/project/drone_status",
    "/api/v1/project/analyze",
    "/api/v1/project/finalize",
)


def _is_compute_path(path: str) -> bool:
    if path.startswith(_COMPUTE_PATH_PREFIXES):
        return True
    # /api/v1/projects/{id}/analyze and /finalize are callbacks as well.
    return path.startswith("/api/v1/projects/") and (
        path.endswith("/analyze") or path.endswith("/finalize")
    )


@app.middleware("http")
async def audit_requests(request: Request, call_next):
    # Reuse the caller's correlation id, or make one, and set it for this request.
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    client_ip = request.headers.get("X-Forwarded-For")
    if client_ip:
        client_ip = client_ip.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host

    request.state.request_id = request_id

    started = time.monotonic()
    with with_context(request_id=request_id):
        response = await call_next(request)
    latency_ms = int((time.monotonic() - started) * 1000)

    path = request.url.path
    # Return the id only on /api/ paths that Airflow does not call, so its
    # responses keep exactly the shape the DAGs expect.
    if path.startswith("/api/") and not _is_compute_path(path):
        response.headers["X-Request-Id"] = request_id

    try:
        with with_context(request_id=request_id):
            if response.status_code >= 500:
                log.error(
                    "%s %s -> %s (%dms)",
                    request.method, path, response.status_code, latency_ms,
                )
            else:
                log.info(
                    "%s %s -> %s (%dms)",
                    request.method, path, response.status_code, latency_ms,
                )
    except Exception:
        pass

    try:
        if request.method in _AUDIT_METHODS and path.startswith("/api/"):
            activity_log.append(
                email=request.headers.get("X-User-Email"),
                user_id=request.headers.get("X-User-Id"),
                action=f"{request.method} {path}",
                method=request.method,
                path=path,
                project_id=request.path_params.get("project_id")
                if hasattr(request, "path_params") else None,
                status=response.status_code,
                request_id=request_id,
                client_ip=client_ip,
            )
    except Exception:
        pass
    return response


@app.get("/livez", tags=["meta"])
def livez():
    """Report that the process is up. Used by the Docker healthcheck."""
    return {"status": "ok"}


app.include_router(api_router)
