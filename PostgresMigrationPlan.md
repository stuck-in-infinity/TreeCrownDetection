# SQLite → PostgreSQL, with SQLite kept as a live fallback

## Context

The backend stores everything in one SQLite file (`/data/treecrown.db`,
`TCP_DATABASE_URL`), created at startup by `Base.metadata.create_all` plus two
hand-written `_migrate_sqlite_*` helpers. That file sits on the `./data` bind
mount, which is what forced the WAL / `busy_timeout=30000` work documented in
`code/app/db/session.py:18-87`. A single file also caps concurrency, leaves
foreign keys unenforced, and gives no path for schema changes on any other
engine.

**What the reference system gives us.** `core-stack-org/core-stack-backend` is
Django, so none of its connection *code* transfers — its entire database config
is:

```python
DATABASES = {"default": {
    "ENGINE": "django.db.backends.postgresql",
    "NAME": DB_NAME, "USER": DB_USER, "PASSWORD": DB_PASSWORD,
    "HOST": "127.0.0.1", "PORT": "",
}}
```

read through `django-environ` from a `.env` holding `DB_NAME`, `DB_USER`,
`DB_PASSWORD`. No pooling, no `CONN_MAX_AGE`, no `OPTIONS`, no SSL. What *does*
transfer is the **env-var shape** — an operator who has configured their box
should recognise ours — and the idea of Postgres as a provisioned service.
Everything about pooling we have to decide ourselves, because SQLAlchemy pools
where Django (in that config) opens a connection per request.

**Outcome.** Postgres 16 as a compose service, Alembic owning the Postgres
schema, and SQLite retained not merely as the dev backend but as a **runtime
fallback**: if Postgres cannot be reached at startup, the service comes up on
SQLite instead of crash-looping.

Decisions taken: fresh database, no data migration · Postgres as a `db` service
in compose · adopt Alembic now · SQLite remains the dev default.

---

## The fallback, and its price

"Backward support in case Postgres doesn't work" can mean two things, and they
are not the same feature:

1. **Config-level** — SQLite stays a supported backend, selected by
   configuration. Low risk, and we get it for free by keeping both branches.
2. **Runtime** — Postgres is unreachable at boot and the service silently
   continues on SQLite.

(2) is what is being asked for, and it is worth being explicit about what it
costs, once, here: **the two databases hold different data.** A fallback boot
serves whatever is in the SQLite file — not what is in Postgres — and every
write during that window lands in SQLite and is invisible to Postgres when it
comes back. There is no merge afterwards; someone reconciles by hand or accepts
the loss.

That is acceptable as a transition crutch, which is what "for now" means, and it
is dangerous as a permanent arrangement. So the design is bounded:

- Fallback happens **only at startup**, never mid-request. A Postgres that dies
  under load produces errors, not a silent store swap — a swap there would split
  a single run's writes across two databases.
- Never after a successful Postgres connection in this process. Once Postgres
  has answered, this process is committed to it.
- Gated on `TCP_DB_FALLBACK_SQLITE` (default `true` for the transition; flip it
  to `false` the moment Postgres is trusted, and the service crash-loops on a
  bad database instead — which is the correct production behaviour).
- Loud: a multi-line `WARNING` banner at boot, and a new `/readyz` that reports
  the backend actually in use, so "we are degraded" is answerable without
  reading logs.
- Only after the retry budget is spent, so a slow-starting Postgres does not
  trip it.

Default fallback target is the existing `sqlite:////data/treecrown.db` — i.e.
exactly today's behaviour, which is the point of the exercise during the
transition. `TCP_DB_FALLBACK_URL` overrides it; pointing it at a *separate* file
makes a degraded boot obvious (an empty project list) rather than plausible
(stale data that looks real). Pick per deployment.

---

## Phase 1 — Settings and dependencies

### `code/requirements-api.txt`

Replace the commented block at L13-15 with real pins, and bound SQLAlchemy at
L6:

- `psycopg[binary]>=3.2,<3.3` — psycopg 3, not psycopg2. SQLAlchemy 2.x treats
  `postgresql+psycopg` as first-class, psycopg2 is maintenance-only, and the
  binary wheel bundles libpq so the `python:3.10-slim` Dockerfile needs no apt
  changes.
- `alembic>=1.13,<2`
- `SQLAlchemy>=2.0` → `SQLAlchemy>=2.0,<2.1`

### `code/app/core/settings.py`

Mirror the reference system's names, prefixed like everything else here:

```python
db_name: str | None = None        # TCP_DB_NAME     ─┐ core-stack's three
db_user: str | None = None        # TCP_DB_USER      │ names, so an operator
db_password: str | None = None    # TCP_DB_PASSWORD ─┘ recognises them
db_host: str = "db"               # TCP_DB_HOST — compose service, not 127.0.0.1
db_port: int = 5432               # TCP_DB_PORT
database_url: str = "sqlite:////data/treecrown.db"   # unchanged, still wins
db_fallback_sqlite: bool = True                       # see above
db_fallback_url: str = "sqlite:////data/treecrown.db"
```

`db_host` defaults to `db` rather than core-stack's `127.0.0.1` because our
Postgres is a compose service; `127.0.0.1` inside the api container is the api
container.

Resolution, as a computed property `resolved_database_url`:

1. `TCP_DATABASE_URL` set to anything other than the SQLite default → use it
   verbatim. Every existing deployment keeps working untouched, and a single URL
   remains available for anyone who prefers it.
2. else `TCP_DB_NAME` set → compose
   `postgresql+psycopg://{user}:{quote_plus(password)}@{host}:{port}/{name}`.
3. else the SQLite default.

`quote_plus` on the password is why this is a property and not string
concatenation at the call site: a password containing `@`, `/`, `#` or `:`
otherwise produces a URL that parses into the wrong host, and the error is an
authentication failure that names nothing useful. It also removes the old plan's
"generate an alphanumeric password" constraint — any password works.

Also normalise a bare `postgresql://` or `postgres://` to `postgresql+psycopg://`
in the same property. Without it the bare form resolves to psycopg2 and raises
`ModuleNotFoundError` at import of `session.py`, before anything can report why.

Everything downstream keeps reading one value, so this is the only place that
knows the URL has parts.

---

## Phase 2 — Engine, session, and the swap (`code/app/db/session.py`)

This is the phase with a real trap in it.

### The trap

`SessionLocal` is imported **by name at module import time** in three places:

- `code/app/workers/tasks.py:33`
- `code/app/workers/cleanup.py:22`
- `code/scripts/run_retention.py:56`

`tasks.py` is pulled in through the router chain when the app module is
imported, which is **before** the lifespan function runs. So the obvious
implementation — reassign the module global `SessionLocal` when falling back —
leaves the pipeline tasks holding a sessionmaker bound to the dead Postgres
engine. The API would serve fine from SQLite while every analyze run failed on
connection errors, which is a worse outcome than not falling back at all.

### The shape that works

Create the sessionmaker **unbound** and bind it through `configure()`, which
mutates the object every importer already holds:

```python
SessionLocal = sessionmaker(autoflush=False, autocommit=False, future=True)

def _make_engine(url: str):
    """Build an engine and attach the per-backend setup for it."""
    if url.startswith("sqlite"):
        eng = create_engine(url, connect_args={"check_same_thread": False,
                                               "timeout": 30}, future=True)
        event.listen(eng, "connect", _sqlite_pragmas)   # see below
        return eng
    return create_engine(
        url, future=True,
        pool_size=20, max_overflow=20, pool_timeout=30,
        pool_recycle=1800, pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "application_name": "treecrown-api"},
    )

engine = _make_engine(settings.resolved_database_url)
SessionLocal.configure(bind=engine)
```

`_sqlite_pragmas` moves from a decorated inner function (L18-87 today) to module
scope, registered via `event.listen` inside the factory — a decorator binds to
one engine object at import, and after a swap that is the wrong engine. Its body
is unchanged; the WAL-refusal handling stays exactly as written.

`_IS_SQLITE` (L6) stops being a module constant, because the answer can change.
It becomes a function over the *active* engine, and the two `_migrate_sqlite_*`
guards (L114, L152) read that instead of `settings.database_url` — they are
currently correct only because the URL can never change under them.

### Pool sizing

`pool_size=20, max_overflow=20` because route handlers are plain `def`, so
FastAPI runs them in the anyio threadpool (40 threads), each able to hold a
`get_db()` session; and `workers/tasks.py:226` and `:362` hold a session for a
whole pipeline run — minutes to hours — while the frontend polls every 3 s. The
default `QueuePool(5, 10)` would exhaust during a run and 500 the pollers.
`pool_pre_ping` is what stops a `db` restart from killing an in-flight run.
`pool_size` must exceed the concurrent-run count for the same reason.

### `wait_for_db` and the fallback

```python
def wait_for_db(timeout_s: int = 60) -> bool:
    """Retry SELECT 1 until it answers. True if it did."""
```

No-op returning True on SQLite; on Postgres, retry against `OperationalError`
with a short backoff. Needed even with `depends_on: service_healthy`, because
`pg_isready` goes green during initdb before the listener is up.

`init_db()` becomes:

```python
def init_db() -> None:
    global engine
    if not wait_for_db():
        if not settings.db_fallback_sqlite:
            raise RuntimeError(...)          # crash-loop, correctly
        _log_fallback_banner()
        engine = _make_engine(settings.db_fallback_url)
        SessionLocal.configure(bind=engine)  # the three importers follow
        FELL_BACK = True
    if _is_sqlite(engine):
        Base.metadata.create_all(bind=engine)
        _migrate_sqlite_add_columns()
        _migrate_sqlite_unique_job_key()
    else:
        _warn_if_alembic_not_at_head()       # schema belongs to Alembic
```

A fallback boot therefore lands on the SQLite path and self-creates its schema,
which is what makes the degraded mode actually work rather than merely start.

Module-level `ACTIVE_BACKEND` (`"postgres"` / `"sqlite"`) and `FELL_BACK` back
the `/readyz` endpoint (Phase 6).

---

## Phase 3 — Models (`code/app/db/models.py`)

Required:

- `Ortho.size_bytes` `Integer` → `BigInteger` (L98). `max_upload_mb` defaults to
  8192 (8 GiB), `int4` stops at ~2 GiB, and the value is assigned at
  `api/v1/projects.py:866` **after** the whole upload is on disk — so today's
  overflow would 500 the user at the end of a 6 GB upload. Renders as `INTEGER`
  on SQLite, so the fallback path is unaffected.

Same change window, since Postgres enforces foreign keys for the first time (no
`PRAGMA foreign_keys=ON` exists anywhere, so SQLite has never enforced them):

- `ondelete="CASCADE"` on the four `*.project_id` FKs (L91, L115, L163, L200),
  plus `passive_deletes=True` on the four `Project` relationships (L72-84).
- `ondelete="SET NULL"` on `runs.ortho_id` (L169-171) — matches the existing
  "NULL = not recorded" meaning. `ondelete="CASCADE"` on `cluster_labels.run_id`
  (L205-207).
- Add a `naming_convention` to `Base.metadata` in `code/app/db/base.py`
  **before** generating the baseline, or a later addition produces a spurious
  rename revision.
- Strip NUL bytes in `core/failures.classify` before serializing. Postgres
  rejects `NUL` in `text`/`json`, and this fires while recording a failure,
  which loses the original error.

Add `.nullslast()` to the three `ORDER BY started_at DESC ... .first()` queries —
`services/job_claim.py:67`, `:81`, `services/startup_recovery.py:184`. Postgres
puts NULLs first where SQLite puts them last; with one NULL `started_at`,
`startup_recovery` can pick the wrong Job and mark a live Airflow run FAILED.
Compiles identically on SQLite.

Deliberately unchanged: generic `JSON` (nothing queries inside it; `JSONB`
reorders keys), naive `DateTime` + `naive_now()` IST wall-clock (making it
tz-aware would shift every row by 5 h 30 m and change which projects retention
deletes), unlengthed `String`, both `UniqueConstraint`s (NULLs compare distinct
on Postgres too).

Every item here is chosen to render identically on SQLite, so the fallback path
runs the same models.

---

## Phase 4 — Transaction handling (`code/app/services/run_registry.py`)

Postgres aborts the whole transaction on any statement error, so a swallowed
exception poisons the caller's session and the *reported* error becomes an
unrelated `InFailedSqlTransaction`.

- `refresh_project_state` L167-170: catches everything with no rollback. Add
  one, in the shape already used by `set_run_state` L197-200 and
  `run_backfill.py:118-121`.
- `mirror` L128-136: rolls back only when `commit=True`. Make it unconditional.

Verified portable, leave alone: `services/state.py:33-48` `transition_if`
(conditional UPDATE + rowcount), `services/job_claim.py:100-116` (INSERT →
`except IntegrityError` → `rollback` → `find_prior`, already in the right
order).

---

## Phase 5 — Alembic

Layout: `code/alembic.ini`, `code/alembic/env.py`, `script.py.mako`,
`code/alembic/versions/0001_baseline.py`.

`env.py` reads the URL from `settings.resolved_database_url` at runtime — **no
`sqlalchemy.url` in `alembic.ini`**, that file is committed and the URL now
carries a password. Set `compare_type=True`, and `render_as_batch=True` when the
URL is SQLite.

One baseline describing the **final** shape (BigInteger and the ondelete rules
included), since there is no existing Postgres database to upgrade. Generate
with `--autogenerate` against an empty database, then verify against the models:
five tables, both unique constraints, all nine indexes.

Run migrations out of band — `docker compose run --rm api alembic upgrade head`
— never from the lifespan; that breaks the moment there is a second worker or
replica.

**Alembic does not own the SQLite path.** Dev and fallback boots keep using
`create_all` plus the two `_migrate_sqlite_*` helpers, exactly as today. Two
schema mechanisms for two backends is deliberate: the fallback has to work
without anyone having run a migration against it, including on a file that has
never existed before.

---

## Phase 6 — docker-compose and `/readyz`

Add a `db` service to `docker-compose.yml` and `docker-compose.hub.yml`:
`postgres:16-alpine`, `POSTGRES_INITDB_ARGS: "--encoding=UTF8 --locale=C"`
(matches SQLite's byte ordering, so the `ORDER BY Ortho.stem` at
`projects.py:530` does not change between backends), `pgdata` named volume,
healthcheck `pg_isready -U … -d …` with `start_period: 30s`, and **no `ports:`**
— nothing outside the compose network needs 5432. Add a top-level
`volumes: pgdata:` (the main file has no `volumes:` key at all today; the hub
file already has one for `filebrowser_db`). Add `depends_on: db:
condition: service_healthy` to `api`.

`./data:/data` stays — storage, logs, the HF cache and the fallback database
still live there.

Credentials go in the service's `environment:` block, not in `.env`:
`env_file:` (docker-compose.yml:37) passes values **literally**, so
`${POSTGRES_PASSWORD}` written inside `.env` arrives as those nine characters and
fails as an auth error with no hint at the cause.

New `GET /readyz` in `main.py`, beside `/livez`:

```json
{"status": "ok", "database": "postgres", "degraded": false}
```

`/livez` stays exactly as it is — the Docker healthcheck depends on it and it
must keep answering while the database is down. `/readyz` is the one that tells
you the service is up *on the wrong database*, which is the failure this whole
fallback design creates and therefore has to make visible.

---

## Phase 7 — Tests

**`code/tests/` does not exist.** This phase is "write the first test suite",
not "update the existing one" — budget accordingly, and nothing in the earlier
phases may be justified by a test file that is not there.

Minimum worth having, given this change can corrupt or silently relocate the
entire datastore:

- `conftest.py` with `test_db_url(tmp_path)` → `TCP_TEST_DATABASE_URL` if set,
  else `sqlite:///{tmp_path}/t.db`. Every fixture must purge `sys.modules` for
  `app.*`, because settings are read at import time.
- `test_settings_url.py` — the resolution order and `quote_plus`: a password
  containing `@` and `/` composes to a URL that parses back to the right host,
  and `postgres://` normalises to `postgresql+psycopg://`.
- `test_fallback.py` — the important one. Point `TCP_DB_NAME` at a dead port,
  run `init_db()`, and assert: it returns rather than raising; `ACTIVE_BACKEND`
  is `"sqlite"`; the schema was created; **and a `SessionLocal` imported before
  `init_db()` ran now yields a working session** — that is the `configure()`
  behaviour from Phase 2, and it is the one regression that would otherwise ship
  looking fine. Then the same with `db_fallback_sqlite=False` and assert it
  raises.
- `test_postgres_semantics.py`, skipped unless `TCP_TEST_DATABASE_URL` names a
  Postgres URL: FK violation raises `IntegrityError`; `size_bytes = 3*1024**3`
  round-trips; deleting a project empties all five tables with no
  `ForeignKeyViolation`; a session stays usable after `refresh_project_state`
  swallows a statement error.

CI: `alembic upgrade head` on an empty database, then an autogenerate diff — a
model change that never got a revision fails the build.

---

## Phase 8 — Docs

Only files that exist (`DB_SCHEMA.md` and `docs/CODEBASE_GUIDE.md` do **not**;
neither does `code/tests/`):

- **`README.md`** — L163 ("On first boot the API creates the SQLite database"),
  L563 ("deleting the sqlite DB is safe — it is recreated empty on startup"),
  L565 ("Schema changes apply themselves on SQLite at boot"), L574 (the WAL
  backup note). The middle two become actively wrong for Postgres. Replace the
  backup note with
  `docker compose exec -T db pg_dump -U <user> -Fc <db> > backup-$(date +%F).dump`.
  Add the `TCP_DB_*` rows to the env table at L76-81 and a line on `/readyz`.
- **`docs/CODEBASE_MAP.md`** — the storage/DB rows in §2 and §3, the WAL
  invariant, and a new invariant: *the fallback swaps the engine via
  `SessionLocal.configure()`, never by rebinding the name, because three modules
  import it before the lifespan runs.* That is exactly the kind of thing the map
  exists to stop someone re-deriving.
- **`docs/INTEGRATION_GUIDE.md`** — env blocks, env table, volume table,
  quickstart gains `alembic upgrade head`.
- **`.env.example`** — the `TCP_DB_*` block, the `env_file:` non-interpolation
  warning, and the fallback's data-divergence note in the words used above.
- **`project_outline.md:23`**, `code/app/core/settings.py:21-23`,
  `code/app/core/logging.py:57`, `docker-compose.yml:48`.

---

## Verification (Linux / WSL Ubuntu)

Put the repo on the WSL filesystem, not `/mnt/e`.

1. `docker compose up -d db`; `docker compose exec db pg_isready -U … -d …` and
   `psql -c '\l+'` to confirm `Collate=C`.
2. `docker compose run --rm api alembic upgrade head`; `\dt` shows five tables
   plus `alembic_version`; `\d+ orthos` shows `size_bytes | bigint`;
   `\d cluster_labels` shows the FKs carry `ON DELETE`.
3. **Password composition:** set `TCP_DB_PASSWORD` to something containing `@`
   and `/`, and confirm the service connects.
4. **FK enforcement:** the same `INSERT` into `cluster_labels` with a
   nonexistent `project_id` succeeds on SQLite and fails on Postgres with
   SQLSTATE 23503.
5. **size_bytes:** insert `3221225472`, confirm it reads back whole, and that
   `GET /api/v1/projects/{id}` and the quota total (`projects.py:726-738`)
   report it untruncated.
6. **Cascade:** a project with an ortho, a run, two labels and a job → `DELETE`
   the project → all five tables empty, nothing in the log.
7. **Pool:** during a real analyze run, fire 60 concurrent
   `GET /api/v1/projects/mine`; `pg_stat_activity` stays ≤ 40 and no
   `QueuePool limit ... reached` appears.
8. **Pre-ping:** `docker compose restart db` mid-run — the next request
   recovers.
9. **Fallback, the whole point:** `docker compose stop db`, then restart `api`.
   It must come up; the banner must be in the log; `/readyz` must report
   `{"database": "sqlite", "degraded": true}`; and **a full analyze run must
   complete**, which is what proves `workers/tasks.py` followed the swap. Then
   `docker compose start db`, restart `api`, and confirm it is back on Postgres
   and `/readyz` is clean.
10. **Fallback disabled:** `TCP_DB_FALLBACK_SQLITE=false` with `db` stopped →
    the api exits with the explicit error rather than serving from SQLite.
11. **Idempotency:** two simultaneous `POST /api/v1/compute/analyze` with one
    `Idempotency-Key` → exactly one WON, one DUPLICATE/REPLAY, exercising
    `uq_jobs_project_task` on Postgres.

No Docker in WSL? `sudo apt install postgresql-16`, `createuser --pwprompt`,
`createdb -O …`, then the same steps with plain `psql` and `TCP_DB_HOST=127.0.0.1`.

---

## Things that fail silently if skipped

1. **Rebinding `SessionLocal` instead of `configure()`-ing it** — the API serves
   from SQLite while every pipeline run still tries the dead Postgres. Phase 2.
2. `create_all` never adds a column to an existing table and does not error — a
   model change deploys clean and the column is missing until the first INSERT.
   This is why Alembic is in.
3. Swallowed exceptions without rollback poison the session; the reported error
   is an unrelated one further down the request. Phase 4.
4. NULLs sort first on Postgres, last on SQLite — `startup_recovery` can fail a
   live run. Phase 3.
5. `env_file:` does not interpolate `${...}`. Phase 6.
6. NUL bytes in error text raise while recording a failure, losing the original.
   Phase 3.
7. A worker session pins a pooled connection for the whole run, so `pool_size`
   must exceed the concurrent-run count. Phase 2.
8. An unquoted password containing `@` or `/` parses into a different host and
   reports only "authentication failed". Phase 1.
9. **A fallback nobody notices** — the service is up, the data is a week old,
   and writes are going somewhere Postgres will never see. `/readyz` and the
   banner exist for this; the flag going to `false` after the transition is the
   real fix.

---

## Order and cost

Phases 1-2 are the substance and are worth doing together — settings, engine,
fallback and `wait_for_db` are one coherent change and splitting them leaves a
half-wired engine. 3-4 are small and mechanical but must land before any
Postgres data exists. 5-6 are infrastructure. 7 is the first test suite and is
the largest single piece; 9 of the 11 verification steps can be done by hand
first if credits are tight, with `test_fallback.py` the one that should not be
skipped, because it covers the trap in Phase 2.
