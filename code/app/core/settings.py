"""Application settings.

Every value can be overridden by an environment variable with the ``TCP_``
prefix (e.g. ``TCP_DATABASE_URL``) or by a local ``.env`` file. See
``.env.example``.
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="TCP_", extra="ignore"
    )

    # Storage and paths
    storage_root: str = "/data/storage"  # per-project artifacts live here
    models_dir: str = "/models"          # directory holding the .pth weight files

    # Database and task queue. SQLite is the default for local development;
    # point database_url at Postgres in production.
    database_url: str = "sqlite:////data/treecrown.db"
    redis_url: str = "redis://localhost:6379/0"

    # Airflow orchestration (optional). With airflow_base_url set, the /runs/*
    # trigger endpoints start the matching Airflow DAG, which calls back into
    # /analyze and /finalize. Left blank, those endpoints run the compute in a
    # background thread instead, so the pipeline still works without Airflow.
    # The REST credentials are optional; leave them blank to call the Airflow
    # API unauthenticated.
    airflow_base_url: str = ""               # e.g. http://host.docker.internal:8080
    airflow_username: str | None = None      # Airflow REST basic-auth user
    airflow_password: str | None = None      # Airflow REST basic-auth password
    airflow_auth_token: str | None = None    # bearer token, as an alternative
    analyze_dag_id: str = "drone_analyze"
    finalize_dag_id: str = "drone_finalize"
    drone_dag_id: str = "drone_pipeline"    # combined DAG used by drone_api

    # Time limit on a single orthomosaic transfer (browser upload or Drive
    # download), counted from the first byte requested. Without it a stalled
    # transfer holds a worker and the project's UPLOADING claim forever.
    #
    # The upload path checks the limit between chunks. gdown is one blocking
    # call and cannot be checked that way, so the Drive download runs in a child
    # process that is killed when the time is up (see services/drive_download.py).
    #
    # Set to 0 for no limit.
    ortho_transfer_timeout_min: int = 45

    # Model registry. models_manifest is an external catalog of detectors and
    # backbones; mount it as a Docker volume so the catalog can change without
    # rebuilding the image. If the file is missing, models_registry uses its
    # built-in defaults.
    default_model_key: str = "urban_cambridge"
    models_manifest: str = "/code/models.yaml"

    # Uploads. The per-project limits below are checked once, before the request
    # body is accepted, as ``used >= limit`` rather than
    # ``used + incoming > limit``. An upload already running is therefore always
    # allowed to finish, and the quota can be exceeded by at most one
    # orthomosaic (bounded by max_upload_mb). Lower max_upload_mb if that
    # matters; changing the check would let a running upload fail part-way.
    max_upload_mb: int = 8192
    project_quota_gb: float = 5          # TCP_PROJECT_QUOTA_GB, 0 disables
    max_orthos_per_project: int = 10     # TCP_MAX_ORTHOS_PER_PROJECT, 0 disables

    # Auth (optional). api_key guards the frontend endpoints; clients must then
    # send ``X-API-Key: <api_key>``. compute_token is a separate credential for
    # the compute endpoints (/analyze, /finalize) and is shared only with the
    # orchestrator, which sends it as ``X-Service-Token: <compute_token>``.
    api_key: str | None = None
    compute_token: str | None = None

    # Google sign-in, used for audit and ownership only. With auth_enabled True,
    # the human endpoints require an ``X-User-Email`` header, which the frontend
    # sets after sign-in. The backend does not verify the Google token, so this
    # is only safe behind a gateway or on an internal network. google_client_id
    # is informational here: the public client id the browser uses lives in
    # frontend/config.js. The backend would need it only for server-side token
    # verification.
    auth_enabled: bool = False
    google_client_id: str | None = None

    # FileBrowser (optional). When set, each project folder gets a public share
    # at creation time, and the share URL comes back in the analyze and finalize
    # responses.
    filebrowser_base_url: str = ""        # internal URL the backend calls
    filebrowser_public_url: str = ""      # URL shown to the user, defaults to base_url
    filebrowser_username: str = "admin"
    filebrowser_password: str = ""

    # Public base URL used to make STAC asset and link hrefs absolute, e.g.
    # https://api.example.com. Leave blank to emit relative hrefs.
    public_base_url: str = ""

    # Browser origins allowed to call this API: comma-separated, or "*" for any.
    # "*" is the default so existing deployments keep working, but production
    # should name the origin the UI is served from, e.g.
    #   TCP_CORS_ORIGINS=https://www.cse.iitd.ernet.in
    # If the UI and API sit behind the same reverse proxy they share an origin,
    # the browser makes no cross-origin requests, and this value is unused.
    cors_origins: str = "*"

    # Run Celery tasks inline in this process, with no broker or worker. Handy
    # in development, but the heavy ML dependencies must still be importable.
    celery_eager: bool = False

    # Retention and cleanup. The consent-aware retention pass runs from
    # scripts/run_retention.py under system cron, not from the Celery beat task,
    # so leave cleanup_enabled False — the old beat job deletes without checking
    # consent. Consent values:
    #   0 (no)              -> folder and DB row deleted
    #   1 (yes, everything) -> kept, subject to retain_consent_all
    #   2 (unlabelled only) -> keep through Step 1, delete step2/3/4 and labels
    retention_days: int = 30       # retention window in days
    retain_consent_all: bool = True
    cleanup_enabled: bool = False  # old Celery beat task, off; the script drives it
    cleanup_hour: int = 3          # beat task only: daily run hour (UTC), 0-23
    cleanup_minute: int = 0        # beat task only: minute, 0-59

    # Logging. log_dir sits outside storage_root/projects so app.log and
    # errors.jsonl survive a retention prune or a project delete. The TCP_*
    # overrides below work automatically through env_prefix.
    log_level: str = "INFO"          # TCP_LOG_LEVEL
    log_dir: str = "/data/logs"      # TCP_LOG_DIR, keep outside storage_root/projects
    log_json: bool = False           # TCP_LOG_JSON: text for dev, JSON for prod
    log_max_bytes: int = 10_000_000  # TCP_LOG_MAX_BYTES, size cap before rotation
    log_backup_count: int = 5        # TCP_LOG_BACKUP_COUNT, rotated files kept


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
