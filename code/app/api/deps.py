from fastapi import Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db import models
from app.db.session import get_db


def require_api_key(
    x_api_key: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> str:
    """Check who is calling a user-facing endpoint.

    There are two checks, and both are optional:
    - if ``settings.api_key`` is set, the ``X-API-Key`` header must match it;
    - if ``settings.auth_enabled`` is True, the ``X-User-Email`` header the
      frontend sets after Google sign-in must be present.

    Returns the signed-in email, or ``"default"``, so the result also serves as
    the user id stored in ``Project.user_id``. This does the same thing as
    ``require_user``, and only exists under its own name so the call sites that
    use it do not have to change.
    """
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Invalid or missing API key"},
        )
    if settings.auth_enabled and not x_user_email:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Google sign-in required"},
        )
    return x_user_email or "default"


def require_user(
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    user: str | None = Query(default=None),
) -> str:
    """Read the user's identity from the headers the frontend sets.

    The frontend looks up the user's email through Google's /userinfo after
    sign-in and sends it as ``X-User-Email``, with an optional ``X-User-Id``.
    The token is not verified here, so this is only safe behind a gateway or on
    an internal network.

    The ``?user=`` query parameter is the same identity by another route, for
    the requests that cannot carry a header at all. A plot, a crown thumbnail
    and a download link are fetched by the browser itself — ``<img src>`` and
    ``<a href>`` send no custom headers — so those URLs have to name the caller
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
    if not settings.auth_enabled:
        return email or "default"
    if not email:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Google sign-in required"},
        )
    return email


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
    if project.user_id != user:
        raise HTTPException(
            status_code=403,
            detail={"code": "FORBIDDEN", "message": "Forbidden", "project_id": project_id},
        )
    return project


def get_project(
    project_id: str | None = None,
    db: Session = Depends(get_db),
    user: str = Depends(require_user),
) -> models.Project:
    return resolve_project(db, user, project_id)
