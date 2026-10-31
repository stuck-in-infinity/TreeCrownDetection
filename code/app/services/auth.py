"""Google sign-in and sessions.

The page posts a Google access token to /auth/google. We check it with
Google's tokeninfo endpoint (aud/azp must be our client id), then create a
session: a random token in an HttpOnly cookie, stored in the DB as a SHA-256
hash. identify() maps the cookie back to an email on each request.
"""
import hashlib
import json
import re
import secrets
import time
from datetime import timedelta
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from fastapi import HTTPException
from fastapi import Request as FastAPIRequest
from fastapi import Response

from app.core.logging import get_logger, naive_now
from app.core.settings import settings

log = get_logger("app.auth")

TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
COOKIE_NAME = "tcp_session"
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+\.apps\.googleusercontent\.com$")
# Cached value meaning "no valid session".
_ANON = ("", "")


def _fail(status: int, code: str, message: str, hint: str | None = None):
    detail = {"code": code, "message": message}
    if hint:
        detail["hint"] = hint
    raise HTTPException(status_code=status, detail=detail)


def _csv(value: str) -> set[str]:
    return {v.strip().lower() for v in (value or "").split(",") if v.strip()}


def _ttl() -> timedelta:
    return timedelta(hours=max(1, int(settings.session_ttl_hours)))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_config() -> None:
    """Fail at startup if sign-in is on but misconfigured."""
    if not settings.auth_enabled:
        return
    client_id = (settings.google_client_id or "").strip()
    if not _CLIENT_ID_RE.match(client_id):
        raise RuntimeError(
            "TCP_AUTH_ENABLED=true needs TCP_GOOGLE_CLIENT_ID set to a Google web "
            "client id (…apps.googleusercontent.com)."
        )
    samesite = (settings.cookie_samesite or "").lower()
    if samesite not in ("lax", "strict", "none"):
        raise RuntimeError("TCP_COOKIE_SAMESITE must be lax, strict or none.")
    if samesite == "none" and not settings.cookie_secure:
        raise RuntimeError(
            "TCP_COOKIE_SAMESITE=none requires TCP_COOKIE_SECURE=true; browsers "
            "drop a SameSite=None cookie that is not Secure."
        )
    if not (settings.cookie_path or "").startswith("/"):
        raise RuntimeError("TCP_COOKIE_PATH must start with '/'.")
    if not settings.cookie_secure:
        log.warning("TCP_COOKIE_SECURE=false: the session cookie is sent over plain "
                    "http. Use this only on localhost.")


def verify_google_token(access_token: str) -> dict:
    """Check an access token with Google and return {"sub", "email"}.

    401: rejected, wrong client id or expired. 403: email not verified.
    503: Google unreachable.
    """
    if not access_token or len(access_token) > 4096:
        _fail(401, "UNAUTHENTICATED", "No Google sign-in was sent.")

    request = Request(
        TOKENINFO_URL,
        data=urlencode({"access_token": access_token}).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10) as reply:
            info = json.loads(reply.read().decode("utf-8"))
    except HTTPError as e:
        if 400 <= e.code < 500:
            _fail(401, "GOOGLE_REJECTED", "Google did not accept this sign-in.",
                  "sign in again")
        log.warning("tokeninfo answered %s", e.code)
        _fail(503, "GOOGLE_UNAVAILABLE", "Google could not check this sign-in.",
              "try again in a minute")
    except (URLError, TimeoutError, OSError, ValueError) as e:
        log.warning("tokeninfo unreachable: %s", e)
        _fail(503, "GOOGLE_UNAVAILABLE", "The server could not reach Google to "
              "check this sign-in.", "the server needs outbound https to "
              "oauth2.googleapis.com")

    # Token must be issued for our client id.
    client_id = (settings.google_client_id or "").strip()
    if not client_id or info.get("aud") != client_id or info.get("azp") != client_id:
        _fail(401, "WRONG_AUDIENCE", "This sign-in was not issued for this site.")

    try:
        expires = int(info.get("exp") or 0)
    except (TypeError, ValueError):
        expires = 0
    if expires <= int(time.time()):
        _fail(401, "TOKEN_EXPIRED", "This sign-in has expired.", "sign in again")

    sub = str(info.get("sub") or "").strip()
    email = str(info.get("email") or "").strip().lower()
    if not sub or not email:
        _fail(401, "UNAUTHENTICATED", "Google did not return an email address "
              "for this account.", "allow the email permission when signing in")
    if str(info.get("email_verified")).lower() != "true":
        _fail(403, "EMAIL_NOT_VERIFIED", "This Google account's email address "
              "is not verified.")
    return {"sub": sub, "email": email}


def email_allowed(email: str) -> bool:
    domains = _csv(settings.auth_allowed_domains)
    emails = _csv(settings.auth_allowed_emails)
    if not domains and not emails:
        return True
    email = email.lower()
    return email in emails or email.rsplit("@", 1)[-1] in domains


def start_session(db, sub: str, email: str) -> str:
    """Create a session for a verified user and return the cookie value.

    Also checks the allow-list and disabled flag, and deletes expired sessions.
    """
    from app.db.models import AuthSession, User

    if not email_allowed(email):
        _fail(403, "ACCOUNT_NOT_ALLOWED", f"{email} is not allowed to use this site.",
              "sign in with an account the administrator has allowed")

    now = naive_now()
    user = db.query(User).filter_by(google_sub=sub).one_or_none()
    if user is None:
        user = User(google_sub=sub, email=email, created_at=now)
        db.add(user)
    elif user.disabled:
        _fail(403, "ACCOUNT_DISABLED", "This account has been disabled.")
    else:
        user.email = email
    user.last_login_at = now

    db.query(AuthSession).filter(AuthSession.expires_at < now).delete(
        synchronize_session=False)

    token = secrets.token_urlsafe(32)
    db.flush()
    db.add(AuthSession(token_hash=hash_token(token), user_id=user.id,
                       created_at=now, expires_at=now + _ttl()))
    db.commit()
    return token


def end_session(db, token: str | None) -> None:
    from app.db.models import AuthSession

    if token:
        db.query(AuthSession).filter_by(token_hash=hash_token(token)).delete(
            synchronize_session=False)
        db.commit()


def identify(request: FastAPIRequest) -> tuple[str, str] | None:
    """Return (email, google_sub) for this request's cookie, or None.

    Cached on request.state. If less than half the TTL is left, the expiry is
    extended and request.state.auth_refresh is set so the cookie is re-sent.
    """
    cached = getattr(request.state, "auth_identity", None)
    if cached is not None:
        return None if cached == _ANON else cached

    token = request.cookies.get(COOKIE_NAME)
    result = None
    if token and len(token) <= 256:
        from app.db.models import AuthSession, User
        from app.db.session import SessionLocal

        db = SessionLocal()
        try:
            row = (db.query(AuthSession, User)
                   .join(User, User.id == AuthSession.user_id)
                   .filter(AuthSession.token_hash == hash_token(token))
                   .one_or_none())
            now = naive_now()
            if row is not None:
                session, user = row
                if session.expires_at > now and not user.disabled:
                    result = (user.email, user.google_sub)
                    if session.expires_at - now < _ttl() / 2:
                        session.expires_at = now + _ttl()
                        db.commit()
                        request.state.auth_refresh = token
        finally:
            db.close()
    request.state.auth_identity = result or _ANON
    return result


def set_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME, token,
        max_age=int(_ttl().total_seconds()),
        path=settings.cookie_path or "/",
        secure=bool(settings.cookie_secure),
        httponly=True,
        samesite=(settings.cookie_samesite or "lax").lower(),
    )


def clear_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path=settings.cookie_path or "/",
        secure=bool(settings.cookie_secure),
        httponly=True,
        samesite=(settings.cookie_samesite or "lax").lower(),
    )
