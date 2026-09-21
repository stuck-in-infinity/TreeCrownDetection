"""Database engine and session.

Postgres is production; SQLite is the development default and the fallback the
service comes up on when Postgres cannot be reached at startup
(``db_fallback_sqlite``).

That fallback shapes this module. ``SessionLocal`` is imported by name at
import time by ``workers/tasks.py``, ``workers/cleanup.py`` and
``scripts/run_retention.py``, and ``tasks.py`` is pulled in through the router
chain before the lifespan runs. Rebinding the name would leave those three
attached to the database we just gave up on, so the sessionmaker is created
unbound and re-pointed with ``SessionLocal.configure(bind=...)``, which mutates
the object they already hold.

For the same reason the SQLite pragmas attach with ``event.listen`` inside
``_make_engine``: a decorator binds to whichever engine existed at import.
"""
import time

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import sessionmaker

from app.core.settings import settings

#: Which backend is in use, and whether we got here by falling back. Read by
#: ``/readyz``, since a degraded service otherwise looks healthy.
ACTIVE_BACKEND = "sqlite"
ACTIVE_URL = ""
FELL_BACK = False

def _is_sqlite_url(url: str) -> bool:
    return url.startswith("sqlite")

def is_sqlite() -> bool:
    """Whether the active engine is SQLite.

    A function, not a constant: the answer changes if the fallback takes over.
    """
    return ACTIVE_BACKEND == "sqlite"

#: Whether the WAL outcome has already been reported. The pragma runs on every
#: new connection; the log line is worth exactly one appearance.
_wal_reported = False

def _sqlite_pragmas(dbapi_conn, _record):
    """WAL so readers never block the writer (and vice versa).

    Under the default rollback journal, a long-running job's writes lock the
    whole file and concurrent status polls fail with "database is locked".
    WAL is persistent (set once per file) but re-asserting per connection is
    harmless and covers a freshly-created DB.

    WAL is attempted rather than required, because it needs a
    memory-mapped ``-shm`` file beside the database and that does not work
    on every filesystem. The case that bites here is a bind mount from a
    Windows host into a container: ``docker-compose.yml`` mounts
    ``./data:/data``, so on a Windows checkout the database sits on a
    translated filesystem where ``PRAGMA journal_mode=WAL`` raises
    ``sqlite3.OperationalError: disk I/O error``.

    This used to be a bare ``try/finally``. The exception escaped the
    ``connect`` event, which meant EVERY connection failed, which meant the
    service could not answer a single request, including the one that would
    have told anybody why. A slower database is a far better outcome than a
    service that will not start, so a refusal here is caught, reported once,
    and the rollback journal is used instead.

    The other failure mode is quieter: on some filesystems the pragma does
    not raise, it just declines, and ``journal_mode`` comes back ``delete``.
    The mode is read back so that case is reported too rather than being
    mistaken for success.
    """
    global _wal_reported
    cur = dbapi_conn.cursor()
    try:
        mode, why = None, None
        try:
            row = cur.execute("PRAGMA journal_mode=WAL").fetchone()
            mode = (row[0] if row else "") or ""
        except Exception as exc:                       # noqa: BLE001
            why = f"{type(exc).__name__}: {exc}"

        # These two always work, and they matter MORE when WAL was refused:
        # under the rollback journal a writer locks the whole file, so the
        # busy timeout is the only thing standing between a status poll and
        # "database is locked".
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=30000")

        if not _wal_reported:
            _wal_reported = True
            from app.core.logging import get_logger
            log = get_logger("app.db")
            if (mode or "").lower() == "wal":
                log.info("sqlite journal_mode=wal")
            else:
                log.warning(
                    "sqlite could not use WAL (mode=%s%s); continuing on the "
                    "rollback journal. Readers and writers will block each "
                    "other under load. This is usually the database sitting "
                    "on a Windows bind mount or a network share — move it "
                    "onto a native Linux volume to get WAL back.",
                    mode or "unknown",
                    f"; {why}" if why else "",
                )
    finally:
        cur.close()

def _make_engine(url: str):
    """Build an engine and attach the setup that its backend needs."""
    if _is_sqlite_url(url):
        # ``timeout`` is the SQLite busy timeout: how long a writer waits for
        # another writer's lock before raising "database is locked". The
        # default (5 s) is short for this workload, a worker thread commits
        # job progress throughout a run while API requests write concurrently.
        eng = create_engine(
            url,
            connect_args={"check_same_thread": False, "timeout": 30},
            future=True,
        )
        event.listen(eng, "connect", _sqlite_pragmas)
        return eng

    # Route handlers are plain ``def``, so FastAPI runs them in the anyio
    # threadpool (40 threads), each able to hold a get_db() session, and
    # workers/tasks.py holds one for a whole run while the frontend polls every
    # 3 s. The default QueuePool(5, 10) would exhaust during a run and 500 the
    # pollers, so pool_size must stay above the concurrent-run count.
    # pool_pre_ping replaces a stale connection after a database restart
    # instead of raising.
    return create_engine(
        url,
        future=True,
        pool_size=20,
        max_overflow=20,
        pool_timeout=30,
        pool_recycle=1800,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "application_name": "treecrown-api"},
    )

# Created unbound, then pointed at an engine, see the module docstring.
SessionLocal = sessionmaker(autoflush=False, autocommit=False, future=True)

engine = None

def _activate(url: str, *, fell_back: bool = False):
    """Point the module, and everyone already holding SessionLocal, at ``url``."""
    global engine, ACTIVE_BACKEND, ACTIVE_URL, FELL_BACK
    engine = _make_engine(url)
    ACTIVE_BACKEND = "sqlite" if _is_sqlite_url(url) else "postgres"
    ACTIVE_URL = url
    FELL_BACK = fell_back
    SessionLocal.configure(bind=engine)
    return engine

_activate(settings.resolved_database_url)

def wait_for_db(timeout_s: int | None = None) -> bool:
    """Block until the database answers ``SELECT 1``. True if it did.

    Always True for SQLite. Needed for Postgres even behind compose's
    ``depends_on: service_healthy``, because ``pg_isready`` goes green during
    initdb, before the listener accepts connections.
    """
    if is_sqlite():
        return True
    from app.core.logging import get_logger
    log = get_logger("app.db")

    budget = settings.db_connect_timeout_s if timeout_s is None else timeout_s
    deadline = time.monotonic() + budget
    attempt = 0
    while True:
        attempt += 1
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                log.info("database reachable after %d attempts", attempt)
            return True
        except OperationalError as exc:
            if time.monotonic() >= deadline:
                log.error("database unreachable after %ds: %s", budget, exc)
                return False
            if attempt == 1:
                log.warning("database not ready yet, retrying: %s", exc)
            time.sleep(min(2.0, 0.25 * attempt))

def _log_fallback_banner(url: str) -> None:
    from app.core.logging import get_logger
    get_logger("app.db").warning(
        "\n"
        "  ============================================================\n"
        "   POSTGRES UNREACHABLE - STARTING ON SQLITE INSTEAD\n"
        "   %s\n"
        "   This database holds DIFFERENT DATA. Anything written now is\n"
        "   invisible to Postgres when it comes back, and there is no merge.\n"
        "   Fix the database and restart; /readyz reports which backend is\n"
        "   live. Set TCP_DB_FALLBACK_SQLITE=false to refuse to start\n"
        "   instead, which is correct once Postgres is trusted.\n"
        "  ============================================================",
        url,
    )

def get_db():
    """FastAPI dependency: yields a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def init_db() -> None:
    """Make the configured database usable, falling back to SQLite if allowed.

    Postgres: the schema belongs to Alembic (``alembic upgrade head``, run out
    of band, never from the lifespan). Nothing is created here; a database that
    is not at head is reported and left alone.

    SQLite keeps creating its own schema with ``create_all`` plus the two
    ``_migrate_sqlite_*`` helpers, because a fallback boot has to work on a file
    nobody has migrated, possibly one that does not exist yet.
    """
    from app.db import models  # noqa: F401 (register mappers)
    from app.db.base import Base
    from app.core.logging import get_logger

    log = get_logger("app.db")

    if not wait_for_db():
        if not settings.db_fallback_sqlite:
            raise RuntimeError(
                f"database at {ACTIVE_URL.split('@')[-1]} is unreachable and "
                "TCP_DB_FALLBACK_SQLITE is false, so there is nothing to serve "
                "from. Start the database, or set the flag to come up on SQLite."
            )
        _log_fallback_banner(settings.db_fallback_url)
        _activate(settings.db_fallback_url, fell_back=True)

    if is_sqlite():
        Base.metadata.create_all(bind=engine)
        _migrate_sqlite_add_columns()
        _migrate_sqlite_unique_job_key()
        return

    # Warn rather than create: create_all would produce a schema Alembic does
    # not know it owns, and it silently skips columns on existing tables.
    try:
        with engine.connect() as conn:
            at_head = conn.exec_driver_sql(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        if not at_head:
            log.warning("alembic_version is empty - run 'alembic upgrade head'")
    except Exception:                                   # noqa: BLE001
        log.warning(
            "no alembic_version table on this database - the schema has not "
            "been migrated. Run 'docker compose run --rm api alembic upgrade "
            "head' before serving traffic."
        )

def _migrate_sqlite_add_columns() -> None:
    """Dev-convenience migration: add columns create_all won't add to an existing
    SQLite file. Keeps older treecrown.db files working without a wipe.
    Use Alembic for real migrations / non-SQLite backends.
    """
    if not is_sqlite():
        return
    wanted = {
        "projects": {
            "current_run": "INTEGER DEFAULT 1",
            "runs": "JSON",
            "run_name": "TEXT",
            "consent": "INTEGER DEFAULT 0",
            "consent_at": "DATETIME",
            "pruned_at": "DATETIME",
        },
        "jobs": {
            "request_id": "TEXT",
        },
        # Per-run labels. Nullable on purpose: an existing row predates runs
        # and is backfilled by `run_backfill`, not by this ALTER.
        "cluster_labels": {
            "run_id": "TEXT",
        },
    }
    with engine.begin() as conn:
        for table, cols in wanted.items():
            rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
            existing = {row[1] for row in rows}
            for name, ddl in cols.items():
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

def _migrate_sqlite_unique_job_key() -> None:
    """Add the jobs (project_id, celery_task_id) unique index to an existing DB.

    ``create_all`` only creates missing *tables*, so a table that predates the
    constraint never gets it. Historic double-runs may already hold duplicate
    pairs, which would make CREATE UNIQUE INDEX fail, those losers get their
    key cleared first (the row itself is kept: it is run history). The keeper is
    the successful attempt, else the most recent.
    """
    if not is_sqlite():
        return
    with engine.begin() as conn:
        idx = conn.exec_driver_sql("PRAGMA index_list(jobs)").fetchall()
        if any(row[1] == "uq_jobs_project_task" for row in idx):
            return
        conn.exec_driver_sql(
            """
            UPDATE jobs SET celery_task_id = NULL
             WHERE celery_task_id IS NOT NULL
               AND rowid NOT IN (
                   SELECT rowid FROM (
                       SELECT rowid, ROW_NUMBER() OVER (
                                  PARTITION BY project_id, celery_task_id
                                  ORDER BY (state = 'SUCCEEDED') DESC,
                                           started_at DESC, rowid DESC
                              ) AS rn
                         FROM jobs
                        WHERE celery_task_id IS NOT NULL
                   ) WHERE rn = 1
               )
            """
        )
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX uq_jobs_project_task "
            "ON jobs (project_id, celery_task_id)"
        )
