"""Sign-in routes: config, Google sign-in, current user, sign-out.

Not behind require_user. The POST routes still need X-Requested-With (CSRF).
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.settings import settings
from app.db.session import get_db
from app.services import auth

router = APIRouter(prefix="/auth")


class GoogleSignIn(BaseModel):
    access_token: str


@router.get("/config")
def auth_config():
    """Whether sign-in is on, and the public client id."""
    return {
        "enabled": bool(settings.auth_enabled),
        "google_client_id": (settings.google_client_id or "").strip() or None,
    }


@router.post("/google")
def google_sign_in(body: GoogleSignIn, request: Request, response: Response,
                   db: Session = Depends(get_db)):
    """Verify a Google access token and set the session cookie. The token is not stored."""
    if not settings.auth_enabled:
        raise HTTPException(404, {"code": "AUTH_DISABLED",
                                  "message": "Sign-in is not enabled on this server."})
    who = auth.verify_google_token(body.access_token)
    token = auth.start_session(db, who["sub"], who["email"])
    auth.set_cookie(response, token)
    # So the activity log records who signed in.
    request.state.audit_email = who["email"]
    request.state.audit_user_id = who["sub"]
    return {"email": who["email"]}


@router.get("/me")
def me(request: Request):
    """Current user's email, or 401."""
    if not settings.auth_enabled:
        raise HTTPException(404, {"code": "AUTH_DISABLED",
                                  "message": "Sign-in is not enabled on this server."})
    who = auth.identify(request)
    if not who:
        raise HTTPException(401, {"code": "UNAUTHENTICATED",
                                  "message": "Not signed in."})
    return {"email": who[0]}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    """Delete the session and clear the cookie."""
    who = auth.identify(request) if settings.auth_enabled else None
    if who:
        request.state.audit_email, request.state.audit_user_id = who
    auth.end_session(db, request.cookies.get(auth.COOKIE_NAME))
    auth.clear_cookie(response)
    # Don't re-send the cookie we just cleared.
    request.state.auth_refresh = None
    return {"ok": True}
