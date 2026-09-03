"""FastAPI application entry point.

Run:
    uvicorn app.main:app --reload
"""
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
    configure_logging()  # side-channel logging — set up before anything else
    init_db()          # dev convenience; use Alembic migrations in production

    # Release runs that a restart killed. A project left in ANALYZING has no way
    # out through the API — analyze, reconfigure and delete are all refused from
    # that state — so without this it stays unusable until someone edits the
    # database. Runs dispatched to Airflow are left alone; see the module.
    from app.db.session import SessionLocal
    from app.services.startup_recovery import recover_interrupted_runs
    # Give projects that predate the runs table their run rows, so the run
    # picker has something to show. Idempotent; skips projects that already
    # have rows. Before recovery, so a run released below lands on a real row.
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
        "Two-job, stage-gated async service wrapping the tree-crown detection / "
        "clustering / species-mapping pipeline. "
        "See API_SPECIFICATION.docx and API_DESIGN.md."
    ),
    lifespan=lifespan,
)

# Normalise every error to the structured {"error": {...}} envelope (v4 §8).
install_error_handlers(app)

# CORS. `allow_origins=["*"]` cannot be combined with `allow_credentials=True`;
# this API authenticates via headers (not cookies), so credentials are not
# needed.
#
# The origin list used to be hardcoded to ["*"], under a comment telling the
# reader to tighten it before deploying — which there was no way to do without
# editing this file. It now comes from TCP_CORS_ORIGINS, still defaulting to "*"
# so no existing deployment changes behaviour on upgrade.
_cors = [o.strip() for o in (settings.cors_origins or "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this the browser cannot read X-Request-Id on a cross-origin
    # response, and that header is half of how the frontend tells "the API
    # answered" from "a proxy answered".
    expose_headers=["X-Request-Id"],
)


# ── Per-user audit middleware ──────────────────────────────────────────
# Records who called mutating endpoints, using the Google-sign-in headers set
# by the frontend (X-User-Email / X-User-Id). Reads/health/CORS-preflight are
# skipped to keep the ledger signal-heavy. Best-effort; never blocks a request.
_AUDIT_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# Airflow-facing compute callbacks — never touch these responses (no X-Request-Id
# header). Byte-identical bodies/status per the Airflow contract. Covers the
# compute callbacks (/api/v1/compute/*) and the DAG->backend callback variants
# (drone_api / drone_status / analyze / finalize) under /api/v1/project(s).
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
    # /api/v1/projects/{id}/analyze and /finalize are compute callbacks too.
    return path.startswith("/api/v1/projects/") and (
        path.endswith("/analyze") or path.endswith("/finalize")
    )


@app.middleware("http")
async def audit_requests(request: Request, call_next):
    # Mint (or reuse) a correlation id and bind it for the request scope.
    request_id = request.headers.get("X-Request-Id") or new_request_id()
    client_ip = request.headers.get("X-Forwarded-For")
    if client_ip:
        client_ip = client_ip.split(",")[0].strip()
    elif request.client:
        client_ip = request.client.host

    # Stash it on request.state as well as the ContextVar. ServerErrorMiddleware
    # sits OUTSIDE this middleware, so a 500's handler runs after the ContextVar
    # has unwound — request.state is the only channel that reaches it, and
    # without it the one response that most needs a correlation id is the one
    # response that would not carry one.
    request.state.request_id = request_id

    started = time.monotonic()
    with with_context(request_id=request_id):
        response = await call_next(request)
    latency_ms = int((time.monotonic() - started) * 1000)

    path = request.url.path
    # Side-channel header ONLY on non-compute /api/ paths (Airflow untouched).
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
    """Liveness probe - is the process up. Used by Docker/K8s healthchecks."""
    return {"status": "ok"}


app.include_router(api_router)
