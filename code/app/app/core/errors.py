"""Structured error envelope shared by both backends.

See API_SPECIFICATION_v4 section 8. Every 4xx/5xx response is normalised to:

    {"error": {"code": "...", "message": "...",
               "project_id": ..., "stage": ..., "details": ...}}

Call sites may either raise ``ApiError`` (precise code) or the usual
``HTTPException(status, "msg")`` (code derived from the status) - or
``HTTPException(status, {"code": ..., "message": ...})`` to set a precise code
without importing ApiError. A catch-all handler converts anything else into a
500 INTERNAL envelope so stack traces never reach clients.
"""
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.logging import _DEFAULT, get_logger, request_id_var

log = get_logger("app.errors")

# Default machine code per HTTP status (used when a call site doesn't supply one).
_STATUS_CODE = {
    400: "BAD_REQUEST",
    401: "UNAUTHENTICATED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    409: "INVALID_STATE",
    413: "UPLOAD_TOO_LARGE",
    422: "VALIDATION_ERROR",
    423: "ORTHO_LOCKED",
    425: "NOT_READY",
    429: "RATE_LIMITED",
    500: "INTERNAL",
    502: "DISPATCH_FAILED",
    503: "DEPENDENCY_MISSING",
    504: "COMPUTE_TIMEOUT",
}


class ApiError(Exception):
    """Raise for a precise error envelope with a stable machine code."""

    def __init__(self, status_code, code, message, *,
                 project_id=None, stage=None, details=None, hint=None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.project_id = project_id
        self.stage = stage
        self.details = details
        self.hint = hint
        super().__init__(message)


def _current_request_id(request=None):
    """The correlation id for this request, or None outside a request scope.

    It already goes out as the ``X-Request-Id`` header, but a header is not
    where anyone looks. Somebody debugging with curl reads the body, pastes it
    into a chat, and the id needs to be in what they pasted — otherwise the only
    way to find their failure in the log is to guess from timestamps.

    Three sources, in this order, and the order matters:

      1. ``request.state.request_id`` — set by the audit middleware. This is the
         ONLY source that works for a 500, because Starlette's
         ``ServerErrorMiddleware`` wraps *outside* user middleware: by the time
         the unhandled-exception handler runs, the ContextVar set inside that
         middleware has already been unwound. Request state survives, because
         the Request object itself is handed to the handler.
      2. the ContextVar — covers handlers that do run inside the middleware
         (4xx, validation), and any raise from a background context.
      3. the inbound header — a caller-supplied id, when nothing else bound one.
    """
    rid = None
    if request is not None:
        rid = getattr(getattr(request, "state", None), "request_id", None)
    if not rid:
        try:
            rid = request_id_var.get()
        except LookupError:
            rid = None
    if (not rid or rid == _DEFAULT) and request is not None:
        try:
            rid = request.headers.get("X-Request-Id")
        except Exception:                  # noqa: BLE001 - header access must not fail a handler
            rid = None
    return None if (not rid or rid == _DEFAULT) else rid


def _envelope(code, message, project_id=None, stage=None, details=None, hint=None,
              request=None):
    error = {
        "code": code,
        "message": message,
        "project_id": project_id,
        "stage": stage,
        "details": details,
    }
    # Optional remediation hint — only added when supplied, so the core shape
    # is unchanged for callers that don't pass one (plan §2a / §10).
    if hint is not None:
        error["hint"] = hint
    rid = _current_request_id(request)
    if rid:
        error["request_id"] = rid
    return {"error": error}


async def _api_error_handler(request, exc: ApiError):
    # Log by severity: ERROR for >=500, WARNING for 4xx.
    hint = getattr(exc, "hint", None)
    if exc.status_code >= 500:
        log.error("%s %s: %s", exc.status_code, exc.code, exc.message)
    else:
        log.warning("%s %s: %s", exc.status_code, exc.code, exc.message)
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(
            exc.code, exc.message, exc.project_id, exc.stage, exc.details, hint,
            request=request,
        ),
    )


async def _http_exception_handler(request, exc: StarletteHTTPException):
    detail = exc.detail
    headers = getattr(exc, "headers", None)
    if exc.status_code >= 500:
        log.error("%s HTTPException: %s", exc.status_code, detail)
    else:
        log.warning("%s HTTPException: %s", exc.status_code, detail)
    if isinstance(detail, dict):
        code = detail.get("code") or _STATUS_CODE.get(exc.status_code, "ERROR")
        return JSONResponse(
            status_code=exc.status_code,
            content=_envelope(
                code,
                detail.get("message", ""),
                detail.get("project_id"),
                detail.get("stage"),
                detail.get("details"),
                detail.get("hint"),
                request=request,
            ),
            headers=headers,
        )
    code = _STATUS_CODE.get(exc.status_code, "ERROR")
    return JSONResponse(
        status_code=exc.status_code,
        content=_envelope(code, str(detail), request=request),
        headers=headers,
    )


def _field_path(err: dict) -> str:
    """pydantic's loc tuple as a readable path: ("body","params","tile_size") ->
    "params.tile_size". The leading body/query/path marker is dropped — the
    caller knows where they put it."""
    loc = [str(p) for p in (err.get("loc") or [])]
    if loc and loc[0] in ("body", "query", "path", "header", "cookie"):
        loc = loc[1:]
    return ".".join(loc) or "(request body)"


async def _validation_exception_handler(request, exc: RequestValidationError):
    """422 used to say only "Request validation failed", leaving the caller to
    decode pydantic's raw error list themselves. Now the message names the
    offending fields; the raw list is still in details for programmatic use."""
    errs = exc.errors()
    named = []
    for e in errs[:5]:
        named.append(f"{_field_path(e)} — {e.get('msg', 'is invalid')}")
    more = len(errs) - len(named)
    summary = "; ".join(named) + (f"; and {more} more" if more > 0 else "")
    noun = "field" if len(errs) == 1 else "fields"
    return JSONResponse(
        status_code=422,
        content=_envelope(
            "VALIDATION_ERROR",
            f"{len(errs)} {noun} in this request could not be accepted: {summary}",
            details=jsonable_encoder(errs),
            hint="Correct the fields named above and send the request again.",
            request=request,
        ),
    )


async def _unhandled_exception_handler(request, exc: Exception):
    """A bug, not a user error. The client still gets something to act on.

    The exception TYPE and the endpoint are safe to return and are what turn
    "Internal server error" into a report someone can triage. The message text
    and traceback stay server-side, because they can carry paths, SQL and
    occasionally credentials. The request id ties the two halves together.
    """
    log.exception("unhandled exception on %s %s", request.method, request.url.path)
    rid = _current_request_id(request)
    return JSONResponse(
        status_code=500,
        content=_envelope(
            "INTERNAL",
            f"The server hit an unexpected {type(exc).__name__} handling "
            f"{request.method} {request.url.path}. This is a bug on our side, "
            f"not something wrong with your request.",
            details={"exception": type(exc).__name__,
                     "path": str(request.url.path),
                     "method": request.method},
            hint=("Retrying the same request will most likely fail the same way. "
                  + (f"Quote request id {rid} when reporting it — the full "
                     f"traceback is in the server log under that id."
                     if rid else
                     "The full traceback is in the server log.")),
            request=request,
        ),
    )


def install_error_handlers(app) -> None:
    """Register the envelope handlers on a FastAPI app (call once at startup)."""
    app.add_exception_handler(ApiError, _api_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)
