"""Application settings.

Every value can be overridden by an environment variable with the ``TCP_``
prefix (e.g. ``TCP_DATABASE_URL``) or by a local ``.env`` file. See
``.env.example``.
"""
from functools import lru_cache
from urllib.parse import quote_plus

from pydantic_settings import BaseSettings, SettingsConfigDict

_SQLITE_DEFAULT = "sqlite:////data/treecrown.db"

def _normalize_pg_scheme(url: str) -> str:
    """Pin Postgres URLs to the psycopg 3 driver.

    A bare ``postgresql://`` resolves to psycopg2, which is not installed ,
    and the failure is a ``ModuleNotFoundError`` raised while importing
    ``db/session.py``, i.e. before anything exists that could report why.
    """
    for bare in ("postgresql://", "postgres://"):
        if url.startswith(bare):
            return "postgresql+psycopg://" + url[len(bare):]
    return url

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="TCP_", extra="ignore"
    )

    # Storage and paths
    storage_root: str = "/data/storage"  # per-project artifacts live here
    models_dir: str = "/models"          # directory holding the .pth weight files

    # Database and task queue. SQLite is the default for local development;
    # point the database at Postgres in production, either with a full
    # ``TCP_DATABASE_URL`` or with the ``TCP_DB_*`` parts below.
    database_url: str = _SQLITE_DEFAULT
    redis_url: str = "redis://localhost:6379/0"

    # Postgres by parts. The first three names match core-stack-backend's
    # DB_NAME / DB_USER / DB_PASSWORD. Host and port are ours to add: that
    # deployment hardcodes 127.0.0.1, which inside the api container would be
    # the api container. Used only when database_url is left at its default;
    # ``resolved_database_url`` is what the engine reads.
    db_name: str | None = None       # TCP_DB_NAME
    db_user: str | None = None       # TCP_DB_USER
    db_password: str | None = None   # TCP_DB_PASSWORD
    db_host: str = "db"              # TCP_DB_HOST, the compose service name
    db_port: int = 5432              # TCP_DB_PORT

    # Come up on SQLite when Postgres is unreachable at startup, instead of
    # crash-looping. The two databases hold different data: a fallback boot
    # serves the SQLite file, and writes made during it are invisible to
    # Postgres afterwards, with no merge. Set False once Postgres is trusted.
    # /readyz reports which backend is in use.
    db_fallback_sqlite: bool = True   # TCP_DB_FALLBACK_SQLITE
    db_fallback_url: str = _SQLITE_DEFAULT  # TCP_DB_FALLBACK_URL

    # How long to wait for Postgres at startup before giving up. pg_isready
    # goes green during initdb, before the listener accepts connections, so
    # waiting is needed even with compose's `depends_on: service_healthy`.
    db_connect_timeout_s: int = 60    # TCP_DB_CONNECT_TIMEOUT_S

    @property
    def resolved_database_url(self) -> str:
        """The URL the engine connects with. Read this, never ``database_url``.

        1. An explicit ``TCP_DATABASE_URL`` wins, so every existing deployment
           and every test fixture keeps working untouched.
        2. Otherwise ``TCP_DB_NAME`` assembles a Postgres URL from the parts.
        3. Otherwise SQLite, as before.

        The password is percent-encoded on the way in. Without that, a password
        containing ``@`` or ``/`` silently re-parses into a different host and
        the only symptom is an authentication failure that names nothing.
        """
        url = (self.database_url or "").strip()
        if url and url != _SQLITE_DEFAULT:
            return _normalize_pg_scheme(url)
        if self.db_name:
            user = quote_plus(self.db_user or "")
            auth = f"{user}:{quote_plus(self.db_password)}@" if self.db_password else (
                f"{user}@" if user else ""
            )
            return (f"postgresql+psycopg://{auth}"
                    f"{self.db_host}:{self.db_port}/{self.db_name}")
        return url or _SQLITE_DEFAULT

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

    # Wall-clock limit on one pipeline run, in minutes, per operation. The run
    # gets its own process, which stops itself when the budget is spent: first
    # by raising inside the task, then by killing its own interpreter after
    # run_kill_grace_s if it is stuck in a C call. Self-timing rather than
    # supervised by the API, which does not reliably outlive the run.
    #
    # A stopped run is recorded FAILED with code RUN_TIMEOUT, so the project
    # does not stay stuck in ANALYZING.
    #
    # Sizing: detection + DINOv2 on a large survey can run well past ten
    # minutes, and this cannot tell wedged from busy. Time a real run on the
    # target hardware first. 0, or run_timeout_enabled=False, means no limit.
    run_timeout_enabled: bool = True   # TCP_RUN_TIMEOUT_ENABLED
    analyze_timeout_min: int = 10      # TCP_ANALYZE_TIMEOUT_MIN, 0 = no limit
    finalize_timeout_min: int = 10     # TCP_FINALIZE_TIMEOUT_MIN, 0 = no limit
    run_kill_grace_s: int = 30         # grace before the run kills its own process

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

    # Trust callbacks that carry no credential. Our Airflow is administered by
    # another team, so DRONE_SERVICE_TOKEN cannot be set on their worker and its
    # callbacks arrive with neither X-Service-Token nor X-User-Email, failing
    # the ownership check in resolve_project with 403. With this True, a request
    # on a callback path (api/callbacks.py) that names no user is accepted as
    # the orchestrator.
    #
    # Anyone who can reach this port can then drive any project, so this asserts
    # that only the orchestrator can reach the API. Leave False wherever it is
    # publicly routable. Goes back to False once Airflow sends a JWT.
    trust_unauthenticated_callbacks: bool = False

    # Google sign-in, used for audit and ownership only. With auth_enabled True,
    # the human endpoints require an ``X-User-Email`` header, which the frontend
    # sets after sign-in. The backend does not verify the Google token, so this
    # is only safe behind a gateway or on an internal network. google_client_id
    # is the PUBLIC OAuth client id the browser signs in with; it reaches the
    # page through the generated /config.js (app/main.py), not a file.
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

    # Where the static web UI lives; the API serves it at "/" from the same port.
    # Blank means the repo's frontend/ next to code/, which is /frontend in the
    # image (baked in, and bind-mounted over by compose). If the folder does not
    # exist the API runs without a UI.
    frontend_dir: str = ""                # TCP_FRONTEND_DIR

    # The API origin the page calls, handed to it as window.API_BASE through the
    # generated /config.js. Blank = same origin, which is right whenever this
    # process serves the page. Set only for a UI hosted somewhere else.
    frontend_api_base: str = ""           # TCP_FRONTEND_API_BASE

    # Browser origins allowed to call this API: comma-separated, or "*" for any.
    # "*" is the default so existing deployments keep working, but production
    # should name the origin the UI is served from, e.g.
    # TCP_CORS_ORIGINS=https://www.cse.iitd.ernet.in
    # If the UI and API sit behind the same reverse proxy they share an origin,
    # the browser makes no cross-origin requests, and this value is unused.
    cors_origins: str = "*"

    # Run Celery tasks inline in this process, with no broker or worker. Handy
    # in development, but the heavy ML dependencies must still be importable.
    celery_eager: bool = False

    # How many crowns per cluster get a thumbnail rendered during analysis, for
    # the review screen to show. Nearest the cluster centre first. Set to 0 to
    # render none and let the API convert every crown on demand instead.
    thumbs_per_cluster: int = 5

    # start-up recovery
    # On boot, release runs that a restart killed: a project left in ANALYZING
    # or FINALIZING with no live worker is marked FAILED so it can be re-run.
    # Only runs whose work was in THIS process are touched; a run handed to
    # Airflow is left alone because the DAG may still be going.
    #
    # Turn off only if you are debugging a stuck project and want its state
    # preserved across a restart.
    startup_recovery_enabled: bool = True

    # Retention and cleanup. The consent-aware retention pass runs from
    # scripts/run_retention.py under system cron, not from the Celery beat task,
    # so leave cleanup_enabled False, the old beat job deletes without checking
    # consent. Consent values:
    # 0 (no): folder and DB row deleted
    # 1 (yes, everything): kept, subject to retain_consent_all
    # 2 (unlabelled only): keep through Step 1, delete step2/3/4 and labels
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
