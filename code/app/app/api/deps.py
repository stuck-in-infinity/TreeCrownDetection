from fastapi import Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db import models
from app.db.session import get_db


def require_api_key(
    x_api_key: str | None = Header(default=None),
    x_user_email: str | None = Header(default=None, alias="X-User-Email"),
) -> str:
    """Identity gate for human endpoints.

    Two independent, both-optional checks:
    - ``settings.api_key`` set → the ``X-API-Key`` header must match (legacy).
    - ``settings.auth_enabled`` True → the Google-sign-in ``X-User-Email`` header
      must be present (GIS audit-only flow, see require_user).

    Returns the signed-in email when present, else ``"default"`` — so it doubles
    as the user id (``Project.user_id``). Behaves identically to ``require_user``;
    kept as a separate name so existing call sites don't churn.
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
) -> str:
    """Human identity from the Google-sign-in headers set by the frontend.

    GIS client-side token flow, audit-only: the frontend resolves the user's
    email from Google's /userinfo and sends it as ``X-User-Email`` (+ optional
    ``X-User-Id``). We do NOT verify the token here — safe only behind a gateway
    / internal network (see docs/OAUTH_GIS_INTEGRATION_PLAN.md §4).

    - ``settings.auth_enabled`` False → open; returns the email if present else
      ``"default"`` (keeps dev/tests working).
    - True → a missing ``X-User-Email`` is rejected 401. The returned email
      becomes ``Project.user_id`` (per-user isolation + audit).
    """
    if not settings.auth_enabled:
        return x_user_email or "default"
    if not x_user_email:
        raise HTTPException(
            status_code=401,
            detail={"code": "UNAUTHENTICATED", "message": "Google sign-in required"},
        )
    return x_user_email


def require_service_token(
    x_service_token: str | None = Header(default=None),
) -> str:
    """Compute-API gate (v4 §9.2). If ``settings.compute_token`` is unset the
    check is open (single-service/dev). Returns the system identity."""
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
