"""An endpoint whose only job is to answer "did my request reach the API?".

Why ``/livez`` was not enough
-----------------------------
``/livez`` lives at the application root. In production this service sits behind
nginx under a path prefix (``/act4dws5/drone``), and only the paths nginx is
configured to route actually arrive. So ``/livez`` may be unreachable while the
API is perfectly healthy, or reachable while the ``/api/v1`` prefix is
misrouted — either way it cannot answer the question anyone actually has when
something is broken, which is:

    is this 404 coming from the API, or from nginx never routing me there?

This endpoint sits on ``/api/v1``, the same prefix as every real call, and names
the service in its body. One curl settles it:

    $ curl -s https://host/act4dws5/drone/api/v1/health
    {"service":"tree-crown-pipeline","status":"ok",...}   <- reached the app
    <html>...404 Not Found...nginx...</html>              <- never got there

It is deliberately unauthenticated and does no I/O: it must answer even when the
database is down, because "the app is up but its database is not" is a different
problem from "the app is not up", and a probe that touches the database cannot
tell you which you have.
"""
from fastapi import APIRouter

from app.core.logging import now_ist

router = APIRouter()

#: Constant, not a version bump target. The point is identification: a client
#: that sees this string knows it is talking to this service and not to a proxy,
#: a captive portal, or somebody else's app on the same host.
SERVICE_NAME = "tree-crown-pipeline"


@router.get("/health", tags=["meta"])
def health():
    """Identify this service. No auth, no database, no filesystem.

    ``status`` is always ``"ok"`` — if the process cannot answer at all, that is
    the signal. Anything that could fail (the database, the storage volume,
    Airflow) is deliberately NOT probed here, because a health check that fails
    for four different reasons tells you nothing about which one you have.
    """
    return {
        "service": SERVICE_NAME,
        "status": "ok",
        "api_version": "v1",
        "time": now_ist().isoformat(),
    }
