from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.core.settings import settings

_IS_SQLITE = settings.database_url.startswith("sqlite")

# ``timeout`` is SQLite's busy timeout: how long a writer waits for another
# writer's lock before giving up with "database is locked". The 5 second default
# is too short here, because a worker thread commits job progress all through a
# run while API requests are writing at the same time.
_connect_args = {"check_same_thread": False, "timeout": 30} if _IS_SQLITE else {}

engine = create_engine(settings.database_url, connect_args=_connect_args, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


if _IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        """Turn on WAL so readers and the writer do not block each other.

        With the default rollback journal, a long job's writes lock the whole
        file and status polls fail with "database is locked". WAL mode sticks to
        the file once set, but setting it on each connection costs nothing and
        covers a database that has just been created.
        """
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA busy_timeout=30000")
        finally:
            cur.close()


def get_db():
    """FastAPI dependency that yields a database session for one request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """Create the tables. Production should use Alembic migrations instead."""
    from app.db import models  # noqa: F401  - imported so the mappers register
    from app.db.base import Base

    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_add_columns()
    _migrate_sqlite_unique_job_key()


def _migrate_sqlite_add_columns() -> None:
    """Add columns that create_all will not add to an existing SQLite file.

    This keeps an older treecrown.db working without deleting it, which is
    convenient in development. Real deployments and other databases should use
    Alembic.
    """
    if not settings.database_url.startswith("sqlite"):
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

    ``create_all`` only creates missing tables, so a jobs table older than this
    constraint never gets it. Earlier double-runs may have left duplicate pairs
    behind, which would make CREATE UNIQUE INDEX fail, so the duplicates have
    their key cleared first. The rows themselves stay, since they are run
    history. The one that keeps its key is the successful attempt, or failing
    that the most recent.
    """
    if not settings.database_url.startswith("sqlite"):
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
