from fastapi import APIRouter, Depends

from app.api.deps import require_app_request
from app.api.v1 import (
    analyze,
    auth,
    clustering,
    compute,
    finalize,
    health,
    labels,
    projects,
    public,
    results,
    runs,
)

# CSRF check on every route (no-op with sign-in off).
api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_app_request)])
# Health goes first on purpose: it is what everything else is diagnosed with.
api_router.include_router(health.router, tags=["meta"])
api_router.include_router(auth.router, tags=["auth"])
api_router.include_router(projects.router, tags=["projects"])
api_router.include_router(runs.router, tags=["runs"])
api_router.include_router(analyze.router, tags=["analyze"])
api_router.include_router(clustering.router, tags=["clustering"])
api_router.include_router(labels.router, tags=["labels"])
api_router.include_router(finalize.router, tags=["finalize"])
api_router.include_router(results.router, tags=["results"])
api_router.include_router(compute.router, tags=["compute"])
api_router.include_router(public.router, tags=["public"])
