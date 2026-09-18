"""Behaviour that differs between SQLite and Postgres.

Skipped unless ``TCP_TEST_DATABASE_URL`` names a Postgres database:

    TCP_TEST_DATABASE_URL=postgresql+psycopg://tc:pw@localhost/treecrown_test \\
        pytest code/tests/test_postgres_semantics.py

These pass trivially on SQLite for the wrong reason: it does not enforce
foreign keys (no ``PRAGMA foreign_keys=ON`` anywhere here), has no 2 GiB
integer ceiling, and does not abort a transaction on a failed statement.
"""
import os

import pytest
from sqlalchemy.exc import IntegrityError

PG_URL = os.environ.get("TCP_TEST_DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    "postgres" not in PG_URL,
    reason="set TCP_TEST_DATABASE_URL to a Postgres URL to run these",
)


@pytest.fixture
def pg(fresh_app):
    mods = fresh_app({"TCP_DATABASE_URL": PG_URL})
    from app.db.base import Base
    Base.metadata.drop_all(bind=mods.session.engine)
    Base.metadata.create_all(bind=mods.session.engine)
    yield mods
    Base.metadata.drop_all(bind=mods.session.engine)


def test_foreign_keys_are_enforced(pg):
    from app.db import models
    db = pg.session.SessionLocal()
    try:
        db.add(models.ClusterLabel(project_id="does-not-exist", chosen_k=3,
                                   cluster_id=0, species="x"))
        with pytest.raises(IntegrityError):
            db.commit()
    finally:
        db.rollback()
        db.close()


def test_size_bytes_holds_more_than_two_gibibytes(pg):
    """max_upload_mb defaults to 8192 and the value is assigned after the whole
    upload lands, so an int4 column 500s at the end of a 6 GB transfer."""
    from app.db import models
    big = 6 * 1024 ** 3
    db = pg.session.SessionLocal()
    try:
        project = models.Project(user_id="a@b.com", state="CREATED")
        db.add(project)
        db.flush()
        db.add(models.Ortho(project_id=project.id, stem="big", filename="big.tif",
                            size_bytes=big))
        db.commit()
        assert db.query(models.Ortho).one().size_bytes == big
    finally:
        db.close()


def test_deleting_a_project_cascades(pg):
    from app.db import models
    db = pg.session.SessionLocal()
    try:
        project = models.Project(user_id="a@b.com", state="CREATED")
        db.add(project)
        db.flush()
        ortho = models.Ortho(project_id=project.id, stem="o", filename="o.tif")
        db.add(ortho)
        db.flush()
        run = models.Run(project_id=project.id, number=1, ortho_id=ortho.id)
        db.add(run)
        db.flush()
        db.add(models.Job(project_id=project.id, type="analyze"))
        db.add(models.ClusterLabel(project_id=project.id, run_id=run.id,
                                   chosen_k=3, cluster_id=0, species="x"))
        db.commit()

        db.delete(db.query(models.Project).one())
        db.commit()

        for model in (models.Project, models.Ortho, models.Run,
                      models.Job, models.ClusterLabel):
            assert db.query(model).count() == 0, model.__name__
    finally:
        db.close()


def test_session_survives_a_swallowed_statement_error(pg):
    """Postgres aborts the whole transaction on any statement error. Without
    the rollback in run_registry the caller's next query fails with an
    unrelated InFailedSqlTransaction."""
    from app.db import models
    from app.services import run_registry

    db = pg.session.SessionLocal()
    try:
        project = models.Project(user_id="a@b.com", state="CREATED")
        db.add(project)
        db.commit()

        # No run rows exist, so the mirror/refresh path has nothing to write;
        # force a statement error underneath it instead.
        db.execute.__self__  # noqa: B018  (keep the session referenced)
        try:
            db.execute(__import__("sqlalchemy").text("SELECT nonexistent_fn()"))
        except Exception:
            pass
        run_registry.refresh_project_state(db, project)

        # The session must still be usable.
        assert db.query(models.Project).count() == 1
    finally:
        db.close()
