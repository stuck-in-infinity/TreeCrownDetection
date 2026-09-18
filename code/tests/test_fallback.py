"""The SQLite fallback: does the service come up, and does everything follow it.

``test_preimported_sessionmaker_follows_the_swap`` covers the one regression
that would not be visible from outside. ``workers/tasks.py``,
``workers/cleanup.py`` and ``scripts/run_retention.py`` import ``SessionLocal``
by name at import time, and ``tasks.py`` loads through the router chain before
the lifespan runs, so rebinding the module global instead of calling
``SessionLocal.configure`` would leave those three on the dead database.
"""
import pytest

# A port nothing listens on, so connecting fails fast.
DEAD_PG = {
    "TCP_DATABASE_URL": "",
    "TCP_DB_NAME": "treecrown",
    "TCP_DB_USER": "tc",
    "TCP_DB_PASSWORD": "pw",
    "TCP_DB_HOST": "127.0.0.1",
    "TCP_DB_PORT": "1",
    "TCP_DB_CONNECT_TIMEOUT_S": "1",
}


def _dead_pg(tmp_path, **extra):
    env = dict(DEAD_PG)
    env["TCP_DB_FALLBACK_URL"] = f"sqlite:///{tmp_path}/fallback.db"
    env.update(extra)
    return env


def test_starts_on_postgres_config_before_connecting(fresh_app, tmp_path):
    """Import alone must not connect — only init_db() does."""
    mods = fresh_app(_dead_pg(tmp_path))
    assert mods.session.ACTIVE_BACKEND == "postgres"
    assert mods.session.FELL_BACK is False


def test_falls_back_to_sqlite(fresh_app, tmp_path):
    mods = fresh_app(_dead_pg(tmp_path))
    mods.session.init_db()

    assert mods.session.ACTIVE_BACKEND == "sqlite"
    assert mods.session.FELL_BACK is True
    # and it is usable, not merely selected: the schema was created
    from app.db import models
    db = mods.session.SessionLocal()
    try:
        assert db.query(models.Project).count() == 0
    finally:
        db.close()


def test_preimported_sessionmaker_follows_the_swap(fresh_app, tmp_path):
    """Hold SessionLocal from before the fallback, exactly as tasks.py does."""
    mods = fresh_app(_dead_pg(tmp_path))
    # This is the `from app.db.session import SessionLocal` that tasks.py runs
    # at import time, before init_db() is ever called.
    preimported = mods.session.SessionLocal
    dead_engine = mods.session.engine

    mods.session.init_db()

    assert mods.session.engine is not dead_engine, "engine should have been replaced"
    assert preimported is mods.session.SessionLocal, "the object must be reused"

    from app.db import models
    db = preimported()          # the pipeline's session, via the stale reference
    try:
        project = models.Project(user_id="a@b.com", state="CREATED")
        db.add(project)
        db.commit()
        assert db.query(models.Project).count() == 1
    finally:
        db.close()


def test_readyz_reports_the_degradation(fresh_app, tmp_path):
    mods = fresh_app(_dead_pg(tmp_path))
    mods.session.init_db()

    from app.main import readyz
    body = readyz()
    assert body == {"status": "degraded", "database": "sqlite", "degraded": True}


def test_readyz_clean_on_a_healthy_backend(fresh_app, tmp_path):
    mods = fresh_app({"TCP_DATABASE_URL": f"sqlite:///{tmp_path}/ok.db"})
    mods.session.init_db()

    from app.main import readyz
    assert readyz() == {"status": "ok", "database": "sqlite", "degraded": False}


def test_refuses_to_start_when_fallback_is_disabled(fresh_app, tmp_path):
    """Once Postgres is trusted: refuse to start rather than serve other data."""
    mods = fresh_app(_dead_pg(tmp_path, TCP_DB_FALLBACK_SQLITE="false"))
    with pytest.raises(RuntimeError, match="unreachable"):
        mods.session.init_db()
    assert mods.session.ACTIVE_BACKEND == "postgres"
    assert mods.session.FELL_BACK is False


def test_no_fallback_when_the_database_is_fine(fresh_app, tmp_path):
    mods = fresh_app({"TCP_DATABASE_URL": f"sqlite:///{tmp_path}/ok.db"})
    mods.session.init_db()
    assert mods.session.FELL_BACK is False
    assert mods.session.ACTIVE_BACKEND == "sqlite"
