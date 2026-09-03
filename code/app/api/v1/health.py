"""An endpoint that answers one question: did my request reach the API?

``/livez`` sits at the application root, but in production this service runs
behind nginx under the path prefix ``/act4dws5/drone``, and only the paths nginx
is configured to route ever arrive. ``/livez`` can therefore be unreachable
while the API is healthy, or reachable while ``/api/v1`` is misrouted. Either
way it cannot answer what someone actually needs to know when a call fails:

    is this 404 from the API, or did nginx never route me there?

This endpoint is on ``/api/v1``, the same prefix as every real call, and names
the service in its response, so one curl settles it:

    $ curl -s https://host/act4dws5/drone/api/v1/health
    {"service":"tree-crown-pipeline","status":"ok",...}   <- reached the app
    <html>...404 Not Found...nginx...</html>              <- never got there

It needs no authentication and does no I/O, so it can answer even when the
database is down. "The app is up but its database is not" is a different problem
from "the app is not up", and a check that reads the database cannot tell you
which one you have.
"""
from fastapi import APIRouter

from app.core.logging import now_ist

router = APIRouter()

# A fixed name, not something to bump per release. It is here so a client that
# sees this string knows it reached this service, rather than a proxy, a captive
# portal, or another app on the same host.
SERVICE_NAME = "tree-crown-pipeline"


@router.get("/health", tags=["meta"])
def health():
    """Identify this service. No auth, no database, no filesystem.

    ``status`` is always ``"ok"``: the signal is whether the process answers at
    all. Nothing that can fail on its own, such as the database, the storage
    volume or Airflow, is checked here, because a health check that fails for
    four different reasons does not tell you which one you have.
    """
    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "api_version": "v1",
        "time": now_ist().isoformat(),
    }
