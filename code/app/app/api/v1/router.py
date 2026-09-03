from fastapi import APIRouter

from app.api.v1 import (
    analyze,
    clustering,
    compute,
    finalize,
    health,
    labels,
    projects,
    results,
    runs,
)

api_router = APIRouter(prefix="/api/v1")
# First, and deliberately: it is the probe everything else is diagnosed with.
api_router.include_router(health.router, tags=["meta"])
api_router.include_router(projects.router, tags=["projects"])
api_router.include_router(runs.router, tags=["runs"])
api_router.include_router(analyze.router, tags=["analyze"])
api_router.include_router(clustering.router, tags=["clustering"])
api_router.include_router(labels.router, tags=["labels"])
api_router.include_router(finalize.router, tags=["finalize"])
api_router.include_router(results.router, tags=["results"])
api_router.include_router(compute.router, tags=["compute"])
