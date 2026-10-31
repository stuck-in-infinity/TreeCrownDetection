from fastapi import Depends, Header, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.callbacks import is_service_callback_path
from app.core.settings import settings
from app.db import models
from app.db.session import get_db
from app.services import auth

# Identities that name no person: require_api_key's fallbacks and the string
# the ledger writes for an unattributed call. Never recorded as an owner.
_PLACEHOLDER_IDENTITIES = {"", "default", "system", "anonymous"}

_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

def require_app_request(request: Request) -> None:
    """CSRF check: writes that carry the session cookie need X-Requested-With.

    Another site can't add this header without a CORS preflight, which only
    passes for TCP_CORS_ORIGINS. GETs are exempt (img and download links).
    Also applies to /auth/* writes. Does nothing with sign-in off.
    """
    if not settings.auth_enabled or request.method in _SAFE_METHODS:
        return
    if (auth.COOKIE_NAME not in request.cookies
            and not request.url.path.startswith("/api/v1/auth/")):
        return
    if not request.headers.get("X-Requested-With"):
        raise HTTPException(
            status_code=403,
            detail={"code": "CSRF_REJECTED",
                    "message": "This request did not come from the app's page.",
                    "hint": "send the X-Requested-With header"},
        )

def session_email(request: Request) -> str:
    """Email of the signed-in user, or 401."""
    who = auth.identify(request)
    if not who:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Google sign-in required"},
        )
    return who[0]

def service_caller(
    request: Request,
    x_service_token: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> bool:
    """True when the caller is the orchestrator rather than a browser.

    Two ways to qualify:

    1. ``X-Service-Token`` matching ``compute_token``. Gated on compute_token
       being set, or every anonymous request would pass as the system.
    2. ``trust_unauthenticated_callbacks``, for an Airflow we do not administer
       and so cannot give the token. The request must be on a callback path and
       name no user; within those bounds it is unauthenticated access, so read
       the setting's comment before turning it on.

    A verified JWT would become a third branch. This is the only place that
    decides who the orchestrator is. A request with a valid session is never
    the orchestrator.
    """
    if settings.compute_token and x_service_token == settings.compute_token:
        return True
    if settings.trust_unauthenticated_callbacks and is_service_callback_path(request.url.path):
        if settings.auth_enabled and auth.identify(request):
            return False
        # /projects/{id}/analyze is reachable by both, so without this the flag
        # would skip the ownership check for signed-in users too.
        named_user = x_user_email or request.query_params.get("user")
        return not named_user
    return False

def attribute_to_owner(request: Request | None, project) -> None:
    """Record whose project this request acted on, for the activity ledger.

    Airflow sends no ``X-User-Email``, and the middleware cannot find the owner
    itself: a callback names its project in the request body, which the
    middleware cannot read without consuming the stream. The endpoint has the
    resolved project, so it leaves the owner on ``request.state`` for
    ``main._attributed_identity`` to read after the response.

    Never raises: an unattributed ledger line beats a failed run.
    """
    if request is None or project is None:
        return
    try:
        request.state.audit_project_id = getattr(project, "id", None)
        owner = getattr(project, "user_id", None)
        if owner and owner not in _PLACEHOLDER_IDENTITIES:
            request.state.audit_email = owner
    except Exception:
        pass

def require_api_key(
    request: Request,
    x_api_key: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
    service: bool = Depends(service_caller),
) -> str:
    """Check who is calling a user-facing endpoint.

    There are two checks, and both are optional:
    - if ``settings.api_key`` is set, the ``X-API-Key`` header must match it;
    - if ``settings.auth_enabled`` is True, a valid session is required and
      ``X-User-Email`` is ignored.

    Returns the signed-in email, or ``"default"``, so the result also serves as
    the user id stored in ``Project.user_id``. This does the same thing as
    ``require_user``, and only exists under its own name so the call sites that
    use it do not have to change.
    """
    # The orchestrator carries neither header; its token already vouched for it.
    if service:
        return x_user_email or "system"
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Invalid or missing API key"},
        )
    if settings.auth_enabled:
        return session_email(request)
    return x_user_email or "default"

def signed_in(request: Request, x_user_email: str | None,
              x_api_key: str | None = None) -> str:
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Invalid or missing API key"},
        )
    if settings.auth_enabled:
        return session_email(request)
    return x_user_email or "default"

def require_user(
    request: Request,
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    user: str | None = Query(default=None),
    service: bool = Depends(service_caller),
) -> str:
    """Return the caller's identity.

    With sign-in on, only the session cookie counts; ``X-User-Email``,
    ``X-User-Id`` and ``?user=`` are ignored. The rest of this docstring is
    about sign-in off (development), where those values are trusted as sent.

    The ``?user=`` query parameter is the same identity by another route, for
    the requests that cannot carry a header at all. A plot, a crown thumbnail
    and a download link are fetched by the browser itself, ``<img src>`` and
    ``<a href>`` send no custom headers, so those URLs have to name the caller
    in the URL or arrive anonymous. Anonymous meant falling back to "the newest
    project owned by ``default``", and nobody owns projects as ``default`` once
    anyone has signed in, so every image on the review screen 404'd. The header
    wins when both are present; this is a fallback, not an override.

    With ``settings.auth_enabled`` False the endpoint is open and this returns
    the email if there is one, otherwise ``"default"``, which keeps development
    and tests working. With it True, a missing identity is rejected with 401.
    The email returned becomes ``Project.user_id``, which is what separates one
    user's projects from another's.
    """
    email = x_user_email or user
    # Same for the orchestrator here: no sign-in to require of it.
    if service:
        return email or "system"
    if not settings.auth_enabled:
        return email or "default"
    return session_email(request)

def require_service_token(
    x_service_token: str | None = Header(default=None),
) -> str:
    """Check the service token on the compute endpoints.

    With ``settings.compute_token`` unset the check passes, which suits a
    single-service or development setup. Returns the system identity.
    """
    if settings.compute_token and x_service_token != settings.compute_token:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Invalid or missing service token"},
        )
    return "system"

def resolve_project(
    db: Session,
    user: str,
    project_id: str | None = None,
    service: bool = False,
) -> models.Project:
    if project_id is None:
        project = (
            db.query(models.Project)
            .filter_by(user_id=user)
            .order_by(models.Project.updated_at.desc(), models.Project.created_at.desc())
            .first()
        )
        if not project:
            raise HTTPException(
                status_code=404,
                detail={"code": "PROJECT_NOT_FOUND", "message": "No project found"},
            )
        return project

    project = db.get(models.Project, project_id)
    if not project:
        raise HTTPException(
            status_code=404,
            detail={"code": "PROJECT_NOT_FOUND", "message": "Project not found",
                    "project_id": project_id},
        )
    if not service and project.user_id != user:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "Forbidden", "project_id": project_id},
        )
    return project

def get_project(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
    service: bool = Depends(service_caller),
) -> models.Project | None:
    # Airflow owns no projects, so there is no "newest of mine" to fall back on.
    # I hand back None and let the endpoint resolve from body.project_id.
    if service and project_id is None:
        return None
    return resolve_project(db, user, project_id, service=service)
