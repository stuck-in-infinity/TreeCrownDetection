"""Central logging setup.

Nothing here touches an HTTP response: it never changes a body, a header or a
status code. The module provides:

  * ContextVars for the correlation ids (request_id, job_id, project_id,
    dag_run_id, stage) and a ``ContextFilter`` that copies them onto every
    ``LogRecord``, using ``"-"`` when one is not set.
  * ``JsonFormatter``, which writes one JSON object per line including any
    exception, and a plain text formatter for readable development logs.
  * ``configure_logging(force=False)``, which sets up the root logger and is
    safe to call repeatedly: stdout, a rotating ``app.log`` at all levels, and a
    rotating ``errors.jsonl`` holding errors only, always as JSON.
  * The helpers ``get_logger``, ``with_context``, ``new_request_id`` and
    ``classify_conn_error``, used by the services and endpoints.
  * ``ERROR_CODES``, the shared list of error codes other modules import.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import logging
import logging.handlers
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from app.core.settings import settings

# India Standard Time, a fixed +05:30 offset with no daylight saving. Log output
# is written in IST for the server administrator. Only the log timestamps are
# localised; database columns keep their own convention, so nothing drifts.
IST = timezone(timedelta(hours=5, minutes=30))
_IST_OFFSET_S = 5 * 3600 + 30 * 60
_IST_DATEFMT = "%Y-%m-%d %H:%M:%S IST"


def now_ist() -> datetime:
    """Current time in IST, with timezone info, for log and ledger timestamps."""
    return datetime.now(IST)


def ist_stamp() -> str:
    """ISO-8601 IST timestamp string, e.g. 2026-07-08T11:51:25+05:30."""
    return datetime.now(IST).strftime("%Y-%m-%dT%H:%M:%S%z")


def naive_now() -> datetime:
    """IST wall-clock time as a naive datetime, replacing ``datetime.utcnow()``.

    Using it everywhere keeps the database columns and the retention cutoff on
    the same clock, so comparing them cannot drift. The result carries no
    tzinfo, matching how SQLite stores timestamps and how utcnow() was called.
    """
    return datetime.now(IST).replace(tzinfo=None)


def naive_from_ts(epoch_seconds: float) -> datetime:
    """Epoch seconds as a naive IST datetime, so filesystem modification times
    can be compared against naive_now()."""
    return datetime.fromtimestamp(epoch_seconds, IST).replace(tzinfo=None)


def _ist_converter(timestamp):
    """Converter for logging.Formatter, returning a struct_time in IST.

    Assigned to formatter instances rather than the class, so Python does not
    bind it as a method and pass self as the first argument.
    """
    base = timestamp if timestamp is not None else time.time()
    return time.gmtime(base + _IST_OFFSET_S)

# Correlation ids. They default to "-" so a LogRecord always has a value, even
# outside a request or task. ContextVars are not inherited by a plain daemon
# thread, so task bodies set these again from the Job row.
_DEFAULT = "-"
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=_DEFAULT
)
job_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "job_id", default=_DEFAULT
)
project_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "project_id", default=_DEFAULT
)
dag_run_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "dag_run_id", default=_DEFAULT
)
stage_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "stage", default=_DEFAULT
)

_CONTEXT_VARS = {
    "request_id": request_id_var,
    "job_id": job_id_var,
    "project_id": project_id_var,
    "dag_run_id": dag_run_id_var,
    "stage": stage_var,
}


class ContextFilter(logging.Filter):
    """Copy the correlation ContextVars onto every LogRecord."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CONTEXT_VARS.items():
            if not hasattr(record, name):
                try:
                    setattr(record, name, var.get())
                except LookupError:
                    setattr(record, name, _DEFAULT)
        return True


class JsonFormatter(logging.Formatter):
    """One JSON object per line, including the exception if present."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in _CONTEXT_VARS:
            payload[name] = getattr(record, name, _DEFAULT)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        # Include any extra fields passed as logger(..., extra={...}).
        for key, value in record.__dict__.items():
            if key in payload or key in _RESERVED_RECORD_KEYS:
                continue
            try:
                json.dumps(value)
            except (TypeError, ValueError):
                value = repr(value)
            payload[key] = value
        return json.dumps(payload, ensure_ascii=False)


# Standard LogRecord attributes, skipped when collecting the extra fields above.
_RESERVED_RECORD_KEYS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
    *(_CONTEXT_VARS.keys()),
}

_TEXT_FORMAT = (
    "%(asctime)s %(levelname)-7s %(name)s "
    "[req=%(request_id)s job=%(job_id)s proj=%(project_id)s "
    "dag=%(dag_run_id)s stage=%(stage)s] %(message)s"
)

_CONFIGURED = False


def configure_logging(force: bool = False) -> None:
    """Set up the root logger. Safe to call more than once.

    ``force=True`` removes the existing handlers first. Celery's prefork pool
    needs this, because a forked worker loses the handlers the parent installed.
    """
    global _CONFIGURED
    root = logging.getLogger()

    if _CONFIGURED and not force:
        return

    if force:
        for handler in list(root.handlers):
            root.removeHandler(handler)
            with contextlib.suppress(Exception):
                handler.close()

    # Avoid adding a second set of handlers if called again without force.
    if root.handlers and not force:
        _CONFIGURED = True
        return

    level = getattr(logging, str(settings.log_level).upper(), logging.INFO)
    root.setLevel(level)

    context_filter = ContextFilter()
    text_formatter = logging.Formatter(_TEXT_FORMAT, datefmt=_IST_DATEFMT)
    json_formatter = JsonFormatter(datefmt=_IST_DATEFMT)
    # Write every log timestamp in IST. See _ist_converter for why this is set
    # on the instances rather than the class.
    text_formatter.converter = _ist_converter
    json_formatter.converter = _ist_converter
    line_formatter = json_formatter if settings.log_json else text_formatter

    # stdout, so the container runtime can collect the logs.
    import sys

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(line_formatter)
    stream.addFilter(context_filter)
    root.addHandler(stream)

    # app.log holds every level; errors.jsonl holds errors only. Both rotate.
    try:
        os.makedirs(settings.log_dir, exist_ok=True)

        app_log = logging.handlers.RotatingFileHandler(
            os.path.join(settings.log_dir, "app.log"),
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        app_log.setFormatter(line_formatter)
        app_log.addFilter(context_filter)
        root.addHandler(app_log)

        errors_log = logging.handlers.RotatingFileHandler(
            os.path.join(settings.log_dir, "errors.jsonl"),
            maxBytes=settings.log_max_bytes,
            backupCount=settings.log_backup_count,
            encoding="utf-8",
        )
        errors_log.setLevel(logging.ERROR)
        errors_log.setFormatter(json_formatter)  # errors.jsonl is always JSON
        errors_log.addFilter(context_filter)
        root.addHandler(errors_log)
    except OSError:
        # The log directory is not writable, so carry on with stdout only.
        logging.getLogger("app.logging").warning(
            "could not open log_dir %s; file logging disabled", settings.log_dir,
            exc_info=True,
        )

    logging.captureWarnings(True)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a named logger, for example ``get_logger("app.api.runs")``."""
    return logging.getLogger(name)


@contextlib.contextmanager
def with_context(**kw):
    """Set the correlation ContextVars for the body of a ``with`` block.

    Only the keys passed in are changed, and each is restored to its previous
    value on exit. Keys that are not correlation ids are ignored.
    """
    tokens = []
    for key, value in kw.items():
        var = _CONTEXT_VARS.get(key)
        if var is None:
            continue
        tokens.append((var, var.set(_DEFAULT if value is None else str(value))))
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def new_request_id() -> str:
    """A new correlation id, as uuid4 hex."""
    return uuid.uuid4().hex


def classify_conn_error(exc: Exception, timeout=None) -> str:
    """Describe a failed outbound call in one readable phrase.

    Services and endpoints use this to say why a call out to another service
    failed. It never describes the request payload itself.
    """
    if exc is None:
        return "no response received"

    reason = getattr(exc, "reason", None)
    target = reason if reason is not None else exc

    if isinstance(target, ConnectionRefusedError):
        return "connection refused"
    if isinstance(target, (socket.timeout, TimeoutError)):
        return "timed out" + (f" after {timeout}s" if timeout is not None else "")
    if isinstance(target, socket.gaierror):
        return "host not found (DNS)"

    if reason is not None:
        text = str(reason).strip()
        return text or "no response received"

    text = str(exc).strip()
    return text or "no response received"


# The shared list of error codes. Other modules import these constants so the
# same code appears where the error is raised, in the response and in the log.
ERROR_CODES = {
    "MISSING_PARAM": "MISSING_PARAM",
    "INVALID_PARAM": "INVALID_PARAM",
    "NO_ORTHO": "NO_ORTHO",
    "NO_LABELS": "NO_LABELS",
    "INVALID_STATE": "INVALID_STATE",
    "CONFLICT_BUSY": "CONFLICT_BUSY",
    "UPLOAD_TOO_LARGE": "UPLOAD_TOO_LARGE",
    "BAD_FORMAT": "BAD_FORMAT",
    "DRIVE_FETCH_FAILED": "DRIVE_FETCH_FAILED",
    "AIRFLOW_UNREACHABLE": "AIRFLOW_UNREACHABLE",
    "AIRFLOW_HTTP_ERROR": "AIRFLOW_HTTP_ERROR",
    "FILEBROWSER_UNREACHABLE": "FILEBROWSER_UNREACHABLE",
    "FILEBROWSER_AUTH": "FILEBROWSER_AUTH",
    "FILEBROWSER_SHARE_FAILED": "FILEBROWSER_SHARE_FAILED",
    "COMPUTE_FAILED": "COMPUTE_FAILED",
    "STORAGE_ERROR": "STORAGE_ERROR",
    "UNAUTHENTICATED": "UNAUTHENTICATED",
    # Codes for the orthomosaic library.
    # 423: the dataset freeze that still applies to ground truth.
    "ORTHO_LOCKED": "ORTHO_LOCKED",
    # 413: the project is at or over settings.project_quota_gb. Delete an
    # orthomosaic or start a new project.
    "PROJECT_QUOTA_EXCEEDED": "PROJECT_QUOTA_EXCEEDED",
    # 409: the project already holds settings.max_orthos_per_project files.
    "ORTHO_COUNT_EXCEEDED": "ORTHO_COUNT_EXCEEDED",
    # 404: no orthomosaic with that id on this project.
    "ORTHO_NOT_FOUND": "ORTHO_NOT_FOUND",
    # 400: the project holds several orthomosaics and the caller did not say
    # which one this run should use.
    "ORTHO_SELECTION_REQUIRED": "ORTHO_SELECTION_REQUIRED",
    # 409: the orthomosaic backs a run that already produced results, so the
    # file cannot be deleted.
    "ORTHO_IN_USE": "ORTHO_IN_USE",
    # A transfer ran past TCP_ORTHO_TRANSFER_TIMEOUT_MIN and was stopped: either
    # the upload was aborted between chunks or the Drive child was killed.
    "TRANSFER_TIMEOUT": "TRANSFER_TIMEOUT",
    # A run was in progress when the process stopped. Written at start-up by
    # services/startup_recovery.py, never by a request.
    "SERVER_RESTARTED": "SERVER_RESTARTED",
    # A run number that this project has never had. Asking for run 9 of a
    # project with five runs gets this, with the valid range in details.
    "RUN_NOT_FOUND": "RUN_NOT_FOUND",
}
