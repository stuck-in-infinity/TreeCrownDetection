"""How TCP_DATABASE_URL and the TCP_DB_* parts resolve into one URL."""
from urllib.parse import unquote, urlsplit


def test_defaults_to_sqlite(fresh_app):
    s = fresh_app({"TCP_DATABASE_URL": ""}).settings
    assert s.resolved_database_url.startswith("sqlite")


def test_explicit_url_wins_over_parts(fresh_app):
    s = fresh_app({
        "TCP_DATABASE_URL": "sqlite:////tmp/explicit.db",
        "TCP_DB_NAME": "ignored",
    }).settings
    assert s.resolved_database_url == "sqlite:////tmp/explicit.db"


def test_parts_compose_a_postgres_url(fresh_app):
    s = fresh_app({
        "TCP_DATABASE_URL": "",
        "TCP_DB_NAME": "treecrown",
        "TCP_DB_USER": "tc",
        "TCP_DB_PASSWORD": "simple",
        "TCP_DB_HOST": "db",
        "TCP_DB_PORT": "5432",
    }).settings
    assert s.resolved_database_url == (
        "postgresql+psycopg://tc:simple@db:5432/treecrown"
    )


def test_password_with_url_metacharacters(fresh_app):
    """An unquoted '@' re-parses into a different host, and the only symptom is
    "authentication failed"."""
    s = fresh_app({
        "TCP_DATABASE_URL": "",
        "TCP_DB_NAME": "treecrown",
        "TCP_DB_USER": "tc",
        "TCP_DB_PASSWORD": "p@ss/w:rd#1",
        "TCP_DB_HOST": "db",
    }).settings
    parts = urlsplit(s.resolved_database_url)
    assert parts.hostname == "db"
    assert parts.port == 5432
    assert parts.path == "/treecrown"
    assert parts.username == "tc"
    # urlsplit does not decode, so the encoding is visible here; what matters
    # is that it round-trips and the host above survived.
    assert parts.password == "p%40ss%2Fw%3Ard%231"
    assert unquote(parts.password) == "p@ss/w:rd#1"


def test_bare_postgres_scheme_is_pinned_to_psycopg(fresh_app):
    """A bare postgresql:// resolves to psycopg2, which is not installed; the
    ModuleNotFoundError is raised while importing db/session.py."""
    for bare in ("postgresql://u:p@h:5432/d", "postgres://u:p@h:5432/d"):
        s = fresh_app({"TCP_DATABASE_URL": bare}).settings
        assert s.resolved_database_url.startswith("postgresql+psycopg://")
        assert s.resolved_database_url.endswith("u:p@h:5432/d")
