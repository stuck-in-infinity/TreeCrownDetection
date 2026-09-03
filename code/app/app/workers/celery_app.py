"""Celery application.

Two logical queues:
  * ``gpu`` — detection (Step 0) + DINOv2 feature extraction; concurrency capped
    to the number of GPUs. Must run with the *prefork* pool (one task per
    process) because the detection patch uses ``os.chdir`` for CWD isolation.
  * ``cpu`` — cropping, clustering, t-SNE, assign, validate, KMZ export.

Run workers, e.g.:
    celery -A app.workers.celery_app:celery_app worker -Q gpu -c 1 --pool=prefork
    celery -A app.workers.celery_app:celery_app worker -Q cpu -c 4

Run the periodic cleanup scheduler:
    celery -A app.workers.celery_app:celery_app beat
"""
from celery import Celery
from celery.schedules import crontab

from app.core.logging import configure_logging
from app.core.settings import settings

# Side-channel logging: configure at import for the main process. Prefork forks
# drop handlers, so also re-configure per worker process below.
configure_logging()

try:
    from celery.signals import worker_process_init

    @worker_process_init.connect
    def _init_worker_logging(**_kw):
        configure_logging(force=True)
except Exception:  # pragma: no cover - signals import shouldn't fail
    pass

celery_app = Celery(
    "treecrown",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_always_eager=settings.celery_eager,
    task_eager_propagates=settings.celery_eager,
    task_track_started=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,   # long tasks: don't hoard the queue
    timezone="UTC",
    task_routes={
        "app.workers.tasks.job_a_analyze": {"queue": "gpu"},
        "app.workers.tasks.job_b_finalize": {"queue": "cpu"},
        "app.workers.cleanup.cleanup_expired_projects": {"queue": "cpu"},
    },
)

# Periodic retention cleanup (run by `celery beat`). Deletes projects + folders
# idle beyond settings.retention_days. Disable via TCP_CLEANUP_ENABLED=false.
if settings.cleanup_enabled:
    celery_app.conf.beat_schedule = {
        "cleanup-expired-projects": {
            "task": "app.workers.cleanup.cleanup_expired_projects",
            "schedule": crontab(
                hour=settings.cleanup_hour, minute=settings.cleanup_minute
            ),
        },
    }

# Register tasks (import after the app object exists to avoid a circular import).
import app.workers.tasks    # noqa: E402,F401
import app.workers.cleanup  # noqa: E402,F401
