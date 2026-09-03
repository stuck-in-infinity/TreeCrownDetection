"""Application settings.

All values can be overridden via environment variables with the ``TCP_`` prefix
(e.g. ``TCP_DATABASE_URL``) or a local ``.env`` file. See ``.env.example``.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="TCP_", extra="ignore"
    )

    # ── storage / paths ────────────────────────────────────────────────
    storage_root: str = "/data/storage"  # per-project artifacts live here
    models_dir: str = "/models"          # directory containing the .pth weight files

    # ── database / task queue ──────────────────────────────────────────
    # SQLite by default for easy local dev; point at Postgres in production.
    database_url: str = "sqlite:////data/treecrown.db"
    redis_url: str = "redis://localhost:6379/0"

    # ── Airflow orchestration (optional) ───────────────────────────────
    # When airflow_base_url is set, the /runs/* trigger endpoints kick off the
    # corresponding Airflow DAG (which calls back into /analyze and /finalize).
    # When it is blank, the trigger endpoints fall back to running the compute
    # in a background thread in-process — so the full pipeline works locally
    # without an Airflow stack. REST auth is OPTIONAL: leave the credentials
    # blank to call the Airflow API unauthenticated.
    airflow_base_url: str = ""               # e.g. http://host.docker.internal:8080
    airflow_username: str | None = None      # Airflow REST basic-auth user (optional)
    airflow_password: str | None = None      # Airflow REST basic-auth password (optional)
    airflow_auth_token: str | None = None    # OR a bearer token for the Airflow REST API
    analyze_dag_id: str = "drone_analyze"
    finalize_dag_id: str = "drone_finalize"
    drone_dag_id: str = "drone_pipeline"    # unified DAG used by drone_api

    # ── orthomosaic transfer budget ────────────────────────────────────
    # Wall-clock ceiling on ONE orthomosaic transfer, browser upload or Drive
    # download, measured from the moment the first byte is asked for. It exists
    # to protect the server: a stalled transfer otherwise holds a worker and the
    # project's UPLOADING claim indefinitely.
    #
    # The upload path checks it between chunks. The Drive path cannot — gdown is
    # one blocking call — so that download runs in a child process which is sent
    # SIGKILL when the budget expires (see services/drive_download.py).
    #
    # Set to 0 to disable the ceiling entirely.
    ortho_transfer_timeout_min: int = 45

    # ── model registry ─────────────────────────────────────────────────
    default_model_key: str = "urban_cambridge"
    # External model catalog (detectors + backbones). Mount this as a volume in
    # Docker so the catalog can change without rebuilding the image. If the file
    # is absent, models_registry falls back to built-in defaults.
    models_manifest: str = "/code/models.yaml"

    # ── uploads ────────────────────────────────────────────────────────
    max_upload_mb: int = 8192
    # Per-project limits for the multi-ortho library (plan v2 §7.1).
    # Admission semantics, deliberately: an upload already in flight is allowed
    # to COMPLETE; any FURTHER upload is rejected. The predicate is therefore
    # ``used >= limit`` evaluated once, before the body is accepted — NOT
    # ``used + incoming > limit``. Consequence: the quota can be overshot by at
    # most one ortho, bounded by max_upload_mb. Lower max_upload_mb if that
    # overshoot matters; do not change the predicate.
    project_quota_gb: float = 5          # TCP_PROJECT_QUOTA_GB — 0 disables
    max_orthos_per_project: int = 10     # TCP_MAX_ORTHOS_PER_PROJECT — 0 disables

    # ── auth (optional) ────────────────────────────────────────────────
    # If set, clients must send the header ``X-API-Key: <api_key>`` (frontend).
    api_key: str | None = None
    # Separate service credential for the Compute API (/analyze, /finalize),
    # shared only with the orchestrator. If set, those endpoints require the
    # header ``X-Service-Token: <compute_token>`` (v4 §9.2).
    compute_token: str | None = None

    # ── Google sign-in (GIS client-side token flow, audit-only) ────────
    # When auth_enabled is True, human endpoints require the ``X-User-Email``
    # header (set by the frontend after Google sign-in). Identity drives
    # per-user audit logging + project ownership; the backend does NOT verify
    # the Google token (safe only behind a gateway / internal network — see
    # docs/OAUTH_GIS_INTEGRATION_PLAN.md §4). Default False keeps dev/tests open.
    # google_client_id is FYI only: the PUBLIC client id actually lives in the
    # frontend (frontend/config.js). The backend needs it only if we later move
    # to server-side token verification (option (b)).
    auth_enabled: bool = False
    google_client_id: str | None = None

    # ── FileBrowser (optional) ─────────────────────────────────────────
    # When set, a public share is created for each project folder at creation
    # time and the share URL is returned in analyze / finalize responses.
    filebrowser_base_url: str = ""        # internal URL the backend uses to call FileBrowser API
    filebrowser_public_url: str = ""      # URL shown to the user in the browser (defaults to base_url)
    filebrowser_username: str = "admin"
    filebrowser_password: str = ""

    # ── STAC ───────────────────────────────────────────────────────────
    # Public base URL used to make STAC Item asset/link hrefs absolute
    # (e.g. https://api.example.com). Leave blank to emit relative hrefs.
    public_base_url: str = ""

    # ── start-up recovery ──────────────────────────────────────────────
    # On boot, release runs that a restart killed: a project left in ANALYZING
    # or FINALIZING with no live worker is marked FAILED so it can be re-run.
    # Only runs whose work was in THIS process are touched; a run handed to
    # Airflow is left alone because the DAG may still be going.
    #
    # Turn off only if you are debugging a stuck project and want its state
    # preserved across a restart.
    startup_recovery_enabled: bool = True

    # ── CORS ───────────────────────────────────────────────────────────
    # Which browser origins may call this API. Comma-separated, or "*" for any.
    #
    # "*" is the default because it is what the code did before this became
    # configurable, and silently tightening it would break every existing
    # deployment on upgrade. It is NOT what you want in production: set it to
    # the origin the UI is actually served from, e.g.
    #   TCP_CORS_ORIGINS=https://www.cse.iitd.ernet.in
    #
    # If the UI and API are behind the same reverse proxy (same origin), the
    # browser sends no cross-origin requests at all and this value is unused —
    # which is the simplest and safest arrangement.
    cors_origins: str = "*"

    # ── dev convenience ────────────────────────────────────────────────
    # Run Celery tasks inline in-process (no broker/worker needed). Note the
    # heavy ML deps must still be importable for an inline run to succeed.
    celery_eager: bool = False

    # ── retention / cleanup ────────────────────────────────────────────
    # Consent-aware retention runs via the standalone script scripts/run_retention.py
    # (system cron), NOT the Celery beat task — keep cleanup_enabled False so the
    # old blanket beat job can't wipe consented data. See
    # docs/RETENTION_CONSENT_CLEANUP_PLAN.md.
    #   consent 0 (No)        -> whole folder + DB row deleted
    #   consent 1 (Yes, all)  -> retained (retain_consent_all)
    #   consent 2 (unlabelled)-> keep through Step 1; delete step2/3/4 + labels
    retention_days: int = 30       # configurable retention window (days)
    retain_consent_all: bool = True
    cleanup_enabled: bool = False  # legacy Celery beat task — OFF (script drives it)
    cleanup_hour: int = 3          # (legacy beat) daily run time (UTC), 0-23
    cleanup_minute: int = 0        # (legacy beat) 0-59

    # ── logging (side-channel; see docs/ERROR_LOGGING_PLAN.md §6) ───────
    # Central logging config. log_dir defaults OUTSIDE storage_root/projects so
    # app.log + errors.jsonl survive retention prune/delete. All TCP_* overrides
    # (e.g. TCP_LOG_LEVEL, TCP_LOG_JSON) work automatically via env_prefix.
    log_level: str = "INFO"          # TCP_LOG_LEVEL
    log_dir: str = "/data/logs"      # TCP_LOG_DIR — OUTSIDE storage_root/projects
    log_json: bool = False           # TCP_LOG_JSON — text (dev) / JSON (prod)
    log_max_bytes: int = 10_000_000  # TCP_LOG_MAX_BYTES — RotatingFileHandler cap
    log_backup_count: int = 5        # TCP_LOG_BACKUP_COUNT — rotated files kept


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
