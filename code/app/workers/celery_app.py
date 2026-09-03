"""Celery application.

There are two queues:
  * ``gpu`` runs Step 0 detection and DINOv2 feature extraction. Its concurrency
    should not exceed the number of GPUs, and it needs the prefork pool, one
    task per process, because the detection code calls ``os.chdir`` to keep each
    task's working directory separate.
  * ``cpu`` runs cropping, clustering, t-SNE, species assignment, validation and
    KMZ export.

Start the workers with, for example:
    celery -A app.workers.celery_app:celery_app worker -Q gpu -c 1 --pool=prefork
    celery -A app.workers.celery_app:celery_app worker -Q cpu -c 4

Run the periodic cleanup scheduler:
    celery -A app.workers.celery_app:celery_app beat
"""
from celery import Celery
from celery.schedules import crontab

from app.core.logging import configure_logging
from app.core.settings import settings

# Set up logging on import for the main process. A forked prefork worker loses
# those handlers, so it is set up again per worker process below.
configure_logging()

try:
    from celery.signals import worker_process_init

    @worker_process_init.connect
    def _init_worker_logging(**_kw):
        configure_logging(force=True)
except Exception:  # pragma: no cover - importing the signal should not fail
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
    worker_prefetch_multiplier=1,   # tasks are long, so take one at a time
    timezone="UTC",
    task_routes={
        "app.workers.tasks.job_a_analyze": {"queue": "gpu"},
        "app.workers.tasks.job_b_finalize": {"queue": "cpu"},
        "app.workers.cleanup.cleanup_expired_projects": {"queue": "cpu"},
    },
)

# Scheduled retention cleanup, run by `celery beat`. It deletes projects and
# their folders once they have been idle longer than settings.retention_days.
# Turn it off with TCP_CLEANUP_ENABLED=false.
if settings.cleanup_enabled:
    celery_app.conf.beat_schedule = {
        "cleanup-expired-projects": {
            "task": "app.workers.cleanup.cleanup_expired_projects",
            "schedule": crontab(
                hour=settings.cleanup_hour, minute=settings.cleanup_minute
            ),
        },
    }

# Register the tasks. This import comes after the app object exists, otherwise
# the imports would be circular.
import app.workers.tasks    # noqa: E402,F401
import app.workers.cleanup  # noqa: E402,F401
