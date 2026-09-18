"""Shared fixtures.

``app.core.settings`` reads the environment at import time and caches it behind
``lru_cache``, so changing a TCP_* variable does nothing unless the ``app.*``
modules are dropped from ``sys.modules`` first. ``fresh_app`` does that.
"""
import os
import sys

import pytest


def _stub_router() -> None:
    """Let ``app.main`` import without the pipeline stack.

    ``app.api.v1.router`` pulls in workers/tasks.py and from there torch and
    detectree2, which are not needed to test the app's own wiring.
    """
    import types

    from fastapi import APIRouter

    stub = types.ModuleType("app.api.v1.router")
    stub.api_router = APIRouter()
    sys.modules["app.api.v1.router"] = stub


def _purge_app_modules() -> None:
    for name in [m for m in sys.modules if m == "app" or m.startswith("app.")]:
        del sys.modules[name]


@pytest.fixture
def fresh_app(monkeypatch, tmp_path):
    """Import the app with a given TCP_* environment, isolated per test.

    Usage::

        mods = fresh_app({"TCP_DB_NAME": "x"})
        mods.session.ACTIVE_BACKEND

    Everything not named by the caller points into ``tmp_path``, so no test
    touches the real database, storage root or log directory.
    """
    def _load(env: dict | None = None):
        _purge_app_modules()
        base = {
            "TCP_DATABASE_URL": f"sqlite:///{tmp_path}/t.db",
            "TCP_STORAGE_ROOT": str(tmp_path / "storage"),
            "TCP_LOG_DIR": str(tmp_path / "logs"),
        }
        base.update(env or {})
        for key in [k for k in os.environ if k.startswith("TCP_")]:
            monkeypatch.delenv(key, raising=False)
        for key, value in base.items():
            monkeypatch.setenv(key, str(value))

        import types

        _stub_router()
        from app.core import settings as settings_mod
        from app.db import session as session_mod

        ns = types.SimpleNamespace(settings=settings_mod.settings,
                                   session=session_mod)
        return ns

    yield _load
    _purge_app_modules()


def test_db_url(tmp_path) -> str:
    """The database the Postgres-semantics tests run against.

    Set ``TCP_TEST_DATABASE_URL`` to a Postgres URL to run them; otherwise they
    skip and everything else runs on a throwaway SQLite file.
    """
    return os.environ.get("TCP_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/t.db"
