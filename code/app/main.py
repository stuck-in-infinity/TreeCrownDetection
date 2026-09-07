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

    # Release runs that a restart killed. A project left in ANALYZING has no way
    # out through the API — analyze, reconfigure and delete are all refused from
    # that state — so without this it stays unusable until someone edits the
    # database. Runs dispatched to Airflow are left alone; see the module.
    from app.db.session import SessionLocal
    from app.services.startup_recovery import recover_interrupted_runs
    # Give projects that predate the runs table their run rows, so the run
    # picker has something to show. Safe to run twice — it skips projects that
    # already have rows. Before recovery, so a run released below lands on a
    # real row.
    from app.services.run_backfill import backfill_runs

    _db = SessionLocal()
    try:
        backfill_runs(_db)
        recover_interrupted_runs(_db)
    finally:
        _db.close()

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
# headers the frontend sets. Every write is recorded.
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Reads are recorded too, but only the ones that hand data back — a download, an
# export or a crown image someone looked at. Recording every GET would bury the
# ledger: pollState() hits /project and /project/runs/status every three seconds
# for the whole length of a run, and neither says anything about who saw what.
# Matched as substrings of the path, so both URL shapes (/projects/{id}/... and
# the pid-in-query /project/...) and every asset under /results are covered.
_AUDIT_GET_MARKERS = (
    "/results",             # summary JSON, KMZ, the CSVs, confusion matrix, STAC
    "/clustering",          # the review screen's data, k-selection and t-SNE plots
    "/crowns/",             # individual crown images
    "/detection/overlay",   # the detection overlay
)


def _should_audit(method: str, path: str) -> bool:
    """Whether this call belongs in the activity ledger."""
    if not path.startswith("/api/"):
        return False
    if method in _AUDIT_METHODS:
        return True
    return method == "GET" and any(m in path for m in _AUDIT_GET_MARKERS)


def _caller_identity(request: Request) -> tuple[str | None, str | None]:
    """The signed-in email and user id behind this request, if any.

    The header is what the frontend's fetch() calls send. The ``?user=``
    fallback matters just as much here: a download link and a crown image are
    fetched by the browser itself through <a href> and <img src>, which carry no
    custom headers, so those requests name the caller in the query string
    instead (see api/deps.py). Reading only the header would record every
    download and every crown image as anonymous, which is most of what the
    ledger is for.
    """
    email = request.headers.get("X-User-Email") or request.query_params.get("user")
    return email, request.headers.get("X-User-Id")


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

    user_email, user_id = _caller_identity(request)

    request.state.request_id = request_id

    started = time.monotonic()
    with with_context(request_id=request_id, user_email=user_email):
        response = await call_next(request)
    latency_ms = int((time.monotonic() - started) * 1000)

    path = request.url.path
    # Return the id only on /api/ paths that Airflow does not call, so its
    # responses keep exactly the shape the DAGs expect.
    if path.startswith("/api/") and not _is_compute_path(path):
        response.headers["X-Request-Id"] = request_id

    try:
        with with_context(request_id=request_id, user_email=user_email):
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
        if _should_audit(request.method, path):
            activity_log.append(
                email=user_email,
                user_id=user_id,
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
