# Version 1.3 — Changelog

Covers everything since the 1.2 changelog was committed (`239cab6`) up to
`df19294`.

| Section | Contents |
|---|---|
| [1. Code changes](#1-code-changes) | every code-level change, file by file |
| [2. Docker & configuration](#2-docker--configuration) | compose, nginx, `.env.example`, new settings |
| [3. Docs and repo hygiene](#3-docs-and-repo-hygiene) | what left the repository, and why |
| [4. Upgrading from 1.2](#4-upgrading-from-12) | SQLite and Postgres, separately |

---

## 1. Code changes

### 1.1 Headline — Postgres, with SQLite kept as a live fallback

Before 1.3 the only supported database was one SQLite file on a bind mount.
That file capped concurrency, left foreign keys unenforced, and gave no path
for a schema change on any other engine: the schema was whatever
`create_all` plus two hand-written `_migrate_sqlite_*` helpers produced.

In 1.3 Postgres 16 is a compose service, Alembic owns the Postgres schema, and
SQLite stays both the development default and a **runtime fallback**: if
Postgres cannot be reached at startup the service comes up on SQLite instead of
crash-looping.

The fallback is a transition crutch and is documented as one everywhere it
appears, because the two databases hold different data. A fallback boot serves
the SQLite file, every write during it is invisible to Postgres afterwards, and
there is no merge. `GET /readyz` reports which backend is live and whether
getting there was the plan; `TCP_DB_FALLBACK_SQLITE=false` refuses to start
instead, which is the correct setting once Postgres is trusted.

Nothing about this is on by default. Leave the compose profile off and the
stack behaves exactly as it did in 1.2.

### 1.2 Headline — a run cannot outlive its time limit

A run used to have no ceiling. `.apply()` runs the task body in the calling
process — a daemon thread for local dispatch, the request threadpool for the
`/compute/*` and `drone_api` callbacks — a thread cannot be killed, the
pipeline has no cancellation point to poll, and Celery's `task_time_limit`
never applies because `.apply()` never reaches a worker. A wedged or simply
enormous run held its project in `ANALYZING` until someone restarted the
service.

In 1.3 every run gets its own process, which holds its own deadline and stops
itself when the budget is spent. The API does not supervise it, because the API
does not reliably outlive it.

### 1.3 New modules

**`code/app/services/run_guard.py`** — `run_guarded()`, the only way a task body is now started.

- Spawns the run in its own interpreter (spawn, not fork: uvicorn runs threads,
  and CUDA does not survive a fork) and blocks until it ends, raising on every
  failure exactly as `task.apply().get(propagate=True)` did.
- Two stopping mechanisms, because a signal handler only runs when the
  interpreter next executes bytecode and a long call inside torch or GDAL never
  returns to it. `SIGALRM` raises `RunTimeout` inside the task body, which
  records the failure through its own `except` clause; a watchdog thread calls
  `os._exit()` after `run_kill_grace_s` if that did not land.
- Exit codes are the child's way of saying how it died: `75` timed out and
  recorded itself, `76` was hard-killed and recorded nothing, a negative code is
  a signal from outside — almost always the OOM killer, which used to leave the
  Job `RUNNING` forever. The parent writes the failure for the two cases the
  child could not.
- `budget_seconds()` reads the per-task budget from settings; `0`, or
  `run_timeout_enabled=False`, means no limit.

**`code/app/api/callbacks.py`** — which request paths are orchestrator callbacks.

- Two questions, kept separate and answered differently. `is_callback_path` is
  loose and decides whether a response must keep the shape the DAGs expect
  (matching one route too many costs a header). `is_service_callback_path` is
  exact and decides whether an anonymous caller may be accepted as the
  orchestrator (matching one route too many hands that route's ownership check
  away). The human trigger routes are deliberately not callbacks.
- Imports nothing, so both the middleware and the dependencies can use it.

**`code/alembic/`** — migrations, with `0001_baseline` as the starting point.

- `env.py` takes the URL from `settings.resolved_database_url`, not from
  `alembic.ini`, so migrations cannot target a different database than the
  service uses and no password is committed.
- `render_as_batch` on SQLite, `compare_type=True` on both.

---

### 1.4 Database

**`code/app/core/settings.py`**

- `TCP_DB_NAME` / `TCP_DB_USER` / `TCP_DB_PASSWORD` / `TCP_DB_HOST` /
  `TCP_DB_PORT`. The first three names match the other production backend; host
  and port are ours to add, because that deployment hardcodes `127.0.0.1`,
  which inside the api container would be the api container.
- `resolved_database_url` is what the engine reads, and nothing reads
  `database_url` any more. An explicit `TCP_DATABASE_URL` still wins, so every
  existing deployment and every test fixture keeps working untouched; otherwise
  `TCP_DB_NAME` assembles a Postgres URL from the parts; otherwise SQLite.
- The password is percent-encoded on the way in. Without it, a password
  containing `@` or `/` re-parses into a different host and the only symptom is
  an authentication failure that names nothing.
- A bare `postgresql://` is pinned to `postgresql+psycopg`, because the bare
  scheme resolves to psycopg2, which is not installed, and the failure is a
  `ModuleNotFoundError` raised while importing `db/session.py` — before
  anything exists that could report why.

**`code/app/db/session.py`** — rewritten around the fallback.

- `SessionLocal` is created **unbound** and re-pointed with
  `SessionLocal.configure(bind=...)`. `workers/tasks.py`, `workers/cleanup.py`
  and `scripts/run_retention.py` import it by name at import time, and
  `tasks.py` is pulled in through the router chain before the lifespan runs, so
  rebinding the module global would leave those three attached to the database
  we had just given up on. For the same reason the SQLite pragmas attach with
  `event.listen` inside `_make_engine` rather than through a decorator.
- `wait_for_db()` blocks until the database answers `SELECT 1`, which is needed
  even behind compose's `depends_on: service_healthy` because `pg_isready` goes
  green during initdb, before the listener accepts connections.
- Postgres pool: `pool_size=20`, `max_overflow=20`, `pool_recycle=1800`,
  `pool_pre_ping`. Route handlers are plain `def`, so FastAPI runs them in the
  anyio threadpool and each can hold a `get_db()` session while a run holds one
  for its whole length and the frontend polls every three seconds; the default
  `QueuePool(5, 10)` would exhaust during a run and 500 the pollers.
- `init_db()` now branches. SQLite keeps creating its own schema, because a
  fallback boot has to work on a file nobody has migrated. On Postgres nothing
  is created: `create_all` would produce a schema Alembic does not know it owns
  and silently skip columns on existing tables, so a database that is not at
  head is reported and left alone.
- `ACTIVE_BACKEND`, `ACTIVE_URL`, `FELL_BACK` and `is_sqlite()` — a function,
  not a constant, because the answer changes if the fallback takes over.

**`code/app/db/models.py`**

- Every foreign key gains an explicit `ondelete` (`CASCADE`, or `SET NULL` for
  a run's ortho) and every relationship `passive_deletes=True`. SQLite does not
  enforce foreign keys, so this was invisible until now.
- `Ortho.size_bytes` is `BigInteger`. On Postgres a 2 GiB orthomosaic overflows
  a 4-byte integer; on SQLite it never did.

**`code/app/db/base.py`** — a naming convention on the metadata, so autogenerate
cannot invent a name for an unnamed constraint and then "rename" it on the next
run. Set before the baseline was generated.

**`code/app/services/run_registry.py`** — `mirror()` and `refresh_project_state()`
now roll back unconditionally when their bookkeeping fails. On Postgres any
failed statement aborts the whole transaction, so swallowing the exception
without a rollback poisoned the caller's session and the error they finally saw
was an unrelated `InFailedSqlTransaction` later in the same request.

**`code/app/core/failures.py`** — `_build` strips NUL from the recorded message.
Postgres rejects NUL in text and json columns, and this dict is written *while*
recording a failure, so a rejected insert would replace the real error with a
database one.

**`job_claim.py`, `startup_recovery.py`** — `ORDER BY started_at DESC` becomes
`.nullslast()`. The two engines sort NULLs at opposite ends, so "the most recent
job" could be a row that was never started.

---

### 1.5 Run time limits

**`code/app/core/settings.py`** — `run_timeout_enabled` (default true),
`analyze_timeout_min` and `finalize_timeout_min` (default 10 each) and
`run_kill_grace_s` (30). The comment says plainly what the number cannot know:
detection plus DINOv2 on a large survey runs well past ten minutes, this cannot
tell wedged from busy, and a real run should be timed on the target hardware
before the default is trusted.

**`code/app/core/failures.py`** — `RunTimeout`, defined here rather than in
`run_guard` so the rule can match it by type without importing multiprocessing
into every failure path, and a `RUN_TIMEOUT` rule whose hint names the two
settings and the two things a user can do about it themselves.

**`code/app/core/logging.py`** — `RUN_TIMEOUT` added to the error-code catalog.

**Every dispatch path now goes through the guard.** `run_dispatch._run_local`'s
thread only waits; `api/v1/analyze.py`, `api/v1/finalize.py` and
`api/v1/compute.py` call `run_guarded(...)` instead of
`task.apply(...).get(propagate=True)`, and none of them import the task
functions any more.

---

### 1.6 Orchestration and identity

**`code/app/api/deps.py`**

- `service_caller` gains a second way to qualify: `trust_unauthenticated_callbacks`,
  for an Airflow we do not administer and therefore cannot give
  `DRONE_SERVICE_TOKEN`. Its callbacks arrive with neither `X-Service-Token`
  nor `X-User-Email` and fail the ownership check with 403 on every call as soon
  as real users own projects. With the flag on, a request **on a callback path**
  that **names no user** is accepted as the orchestrator — the second condition
  matters because `/projects/{id}/analyze` is reachable by both, and without it
  the flag would skip the ownership check for signed-in users too.
- Within those bounds this is unauthenticated access, so it asserts that only
  the orchestrator can reach the API. Default false, and the setting's comment
  says to leave it false wherever the port is publicly routable.
- `attribute_to_owner(request, project)` — a callback names its project in the
  request body, which the middleware cannot read without consuming the stream,
  so the endpoint leaves the resolved owner on `request.state` for the ledger to
  read after the response. Without it every DAG callback was logged as
  anonymous. Never raises: an unattributed ledger line beats a failed run.
- `signed_in()` — the sign-in check on its own, for `drone_api`.

**`code/app/main.py`** — `_is_compute_path` moves out to `api/callbacks.py`;
`_attributed_identity` reads what the endpoints left behind and wins over the
headers; the audited `project_id` prefers the resolved project over the path
parameter. New `GET /readyz`, deliberately separate from `/livez`, which must
keep answering while the database is down because the Docker healthcheck
depends on it.

**`code/app/api/v1/analyze.py`** — a `drone_api` call with no `execution_id` is a
browser call, so it must be signed in and cannot pass as the service. The
analyze payload is built for the run it actually ran, through
`build_clustering_payload(..., run=n, run_row=...)` and
`analyze_asset_fields(project, n)`, and the local `_files_url` helper is gone.

---

### 1.7 Per-run correctness

The run records landed in 1.2; several paths were still reading the project
where they meant the run.

**`code/app/api/v1/compute.py`** — every path is run-scoped. `_target_run()`
takes `req.run` and falls back to the active run, the asset ids are built from
that run's folders, and the state change goes through
`run_registry.set_run_state` instead of assigning `project.state`. Finalize now
checks the **run's** state and counts the **run's** labels: the project-level
check only rejects `UPLOADING` / `DELETING`, so a callback finalizing run 2 is
no longer refused because run 4 is the active one.

**`code/app/api/v1/finalize.py`** — the same label scoping for the human route.

**`code/app/api/v1/runs.py`** — `_fail_trigger` takes the run it failed and
records the failure on that run's row, only touching `project.error` when the
run is the active one. `POST /runs/analyze` ensures the active run has a row
before dispatch.

**`code/app/api/v1/labels.py`** — labelling an **older** run no longer throws the
labels away when the project has moved on. It reads `run_row.available_k`
rather than the project's, and when the target is not the active run it moves
that run to `LABELS_SUBMITTED` and keeps the labels; the 409 and the discard
remain for the active run, where the project state really is the run's state.

**`code/app/services/pipeline_adapter.py`** — `build_config(project, run)` takes
the run number, so the worker no longer builds paths from
`project.current_run` while computing run 2.

**`code/app/services/stac.py`** — `write_stac_item(project, chosen_k, run)`
writes the item into the run's own step4 output.

**`code/app/services/job_claim.py`** — a claim whose prior Job is `FAILED` is
reclaimed rather than rejected as a duplicate, so retrying a failed run with the
same idempotency key works.

**`code/app/services/assets.py`** — the asset payload carries `stac_spec`
alongside `stac`, same object, for the orchestrator that reads that name.

**`code/app/schemas/project.py`** — `k_list` entries are bounded (2–100) and the
list must be non-empty.

---

### 1.8 Frontend (`frontend/index.html`)

- **Analyze and finalize no longer poll Airflow from the browser.** Analyze
  posts to `/project/runs/analyze` and finalize always names its run
  (`/project/runs/{n}/finalize`); both return as soon as the run is dispatched,
  to a DAG or to a local thread, and the browser then follows the run itself.
  Nothing on the page depends on which of the two it was, and the
  `drone_status` polling loop is gone.
- **`pollRunState(n, ...)`** — new, and the reason for the above: it reads the
  **run's** row rather than the project's state, so following an older run to
  completion no longer watches the active run by mistake. It carries the same
  classified failure `pollState` does.
- **`refreshRunLists()`** re-reads the run lists that are currently expanded
  under an orthomosaic, so finishing or labelling a run updates them in place.
- **`showOpenRunReview(p)`** loads the cluster review when a project is reopened
  or restored into a reviewable state, instead of leaving step 4 empty until
  something else triggered a render.
- **The step-3 message element no longer wipes itself.** `validateParams()`
  runs from `analyze()`'s `finally` and shares `#ainfo` with the run's outcome,
  so it now only clears a message it wrote (`dataset.msg`). Before this, a run's
  result — and every failure message — was erased the moment it appeared.
- **The label picker** counts how many of the runs it lists are still waiting
  for names.
- **Guest sign-in** is documented as testing-only and uses one fixed identity
  (`guest@local.com`), which every guest therefore shares, along with their
  projects. The random per-guest identity that was commented out is gone.

---

### 1.9 Tests (`code/tests/`, new)

`conftest.py` provides `fresh_app`, which drops the `app.*` modules from
`sys.modules` before re-importing, because settings are read at import time and
cached behind `lru_cache`; the router is stubbed so `app.main` imports without
torch and detectree2.

| File | What it pins |
|---|---|
| `test_settings_url.py` | how `TCP_DATABASE_URL` and the `TCP_DB_*` parts resolve into one URL, including a password full of metacharacters and the psycopg pin. |
| `test_fallback.py` | the service comes up on SQLite when Postgres is dead, refuses to when the flag is off, `/readyz` says which, and — the one regression invisible from outside — a `SessionLocal` imported before the swap follows it. |
| `test_run_guard.py` | the child interrupts itself at the deadline, kills itself when the alarm cannot be delivered, is left alone when it finishes in time, and the parent records the hard-kill and OOM cases. |
| `test_postgres_semantics.py` | foreign keys enforced, `size_bytes` past 2 GiB, delete cascades, and a session that survives a swallowed statement error. Skipped unless `TCP_TEST_DATABASE_URL` names a Postgres database — on SQLite they would all pass for the wrong reason. |

---

## 2. Docker & configuration

- **`db` service** in both compose files: `postgres:16-alpine`, behind the
  `postgres` profile so a plain `docker compose up` stays on SQLite exactly as
  before. No `ports:` — only the api needs 5432, over the compose network — and
  a named `pgdata` volume rather than a bind mount under `./data`.
  `POSTGRES_INITDB_ARGS` sets `locale=C` to match SQLite's byte ordering, so
  `ORDER BY Ortho.stem` returns the same order on both backends; it is settable
  only at initdb.
- **`depends_on: service_healthy, required: false`** on the api, so it still
  starts with the profile off. `TCP_DB_HOST` / `TCP_DB_PORT` are set in the
  compose `environment:` block rather than `.env`, because `env_file:` passes
  values literally and `${VAR}` inside `.env` would arrive as those characters.
- **nginx** now proxies `/api/` to the api service, so the page and the API
  share one origin and one port and `config.js` can set `window.API_BASE = ""`.
  Some networks let a browser reach only the frontend's port. The upstream is
  resolved through Docker's DNS per request, so recreating the api container
  does not leave nginx pointing at its old IP; `Host` is `$http_host`, not
  `$host`, because the API builds absolute URLs from the Host it receives;
  uploads stream through with no nginx-side size cap and an hour of timeout, to
  stay above `TCP_ORTHO_TRANSFER_TIMEOUT_MIN`. Both compose files mount the file
  over the image's copy, so a config change does not wait for a rebuild.
  `index.html` and `config.js` are served `no-cache`, so a browser cannot keep
  running an old `config.js` pointed at the wrong API.
- **`code/requirements-api.txt`** — `psycopg[binary]>=3.2,<3.3` and
  `alembic>=1.13,<2` are now real dependencies, not commented-out suggestions.
  psycopg 3 rather than psycopg2: SQLAlchemy 2.x treats `postgresql+psycopg` as
  first-class, psycopg2 is maintenance-only, and the binary wheel bundles libpq
  so the slim image needs no apt packages. SQLAlchemy is pinned below 2.1.

New settings:

| Variable | Default | Meaning |
|---|---|---|
| `TCP_DB_NAME` / `TCP_DB_USER` / `TCP_DB_PASSWORD` | — | Postgres by parts. Used only while `TCP_DATABASE_URL` is at its default. |
| `TCP_DB_HOST` / `TCP_DB_PORT` | `db` / `5432` | Set by compose; the service name, not `127.0.0.1`. |
| `TCP_DB_FALLBACK_SQLITE` | `true` | Come up on SQLite when Postgres is unreachable at boot. Different data — set false once Postgres is trusted. |
| `TCP_DB_FALLBACK_URL` | the SQLite default | Which SQLite file that fallback serves. |
| `TCP_DB_CONNECT_TIMEOUT_S` | `60` | How long to wait for Postgres at startup. |
| `TCP_RUN_TIMEOUT_ENABLED` | `true` | Master switch for the wall-clock limit. |
| `TCP_ANALYZE_TIMEOUT_MIN` / `TCP_FINALIZE_TIMEOUT_MIN` | `10` / `10` | Minutes one run may take. `0` is no limit. **Time a real run on your hardware before trusting the default.** |
| `TCP_RUN_KILL_GRACE_S` | `30` | Grace after the limit before the run kills its own interpreter. |
| `TCP_TRUST_UNAUTHENTICATED_CALLBACKS` | `false` | Accept an unauthenticated callback as the orchestrator. Private networks only. |

---

## 3. Docs and repo hygiene

A pass to make the repository publishable, and to keep the internal map of the
system out of it.

- **Removed from git, kept on the workstation** (now in `.gitignore`):
  `docs/CODEBASE_MAP.md`, `REVIEW_AND_RESUME_PLAN.md`, `CLAUDE.md`, and
  `docs/internal/`, which is where `DB_SCHEMA.md` and the logging/audit layout
  now live. README §10 (logging and audit, the ledger's on-disk shape, the
  nginx logrotate recipe) went with them; the remaining sections are renumbered.
- **Placeholders for real values** in `README.md`, `docs/INTEGRATION_GUIDE.md`,
  `docs/filebrowser_*.md` and `version_1.0.md`: the internal Airflow hostname,
  `admin`/`admin`, the institutional URL, a real project UUID and a real
  FileBrowser share hash. Log paths and ledger internals were trimmed out of
  `version_1.0.md`'s module descriptions for the same reason.
- **`PostgresMigrationPlan.md`** — the design behind §1.1: what transfers from
  the reference Django deployment (the env-var shape, and nothing else), what
  the runtime fallback costs, the phase-by-phase plan, the trap that
  `SessionLocal.configure` exists to avoid, and a verification script.
- **`README.md`** gains the three-step Postgres setup, a `pg_dump` backup line
  beside the SQLite one, `/readyz`, the timeout settings with their warning, and
  a troubleshooting entry for the 403-on-every-callback case.

---

## 4. Upgrading from 1.2

**Staying on SQLite** — `docker compose pull` + `up -d`, as before. Nothing in
§1.1 activates: no `db` service, no Alembic, `init_db()` still creates and
migrates the file at boot, and the 1.2 backfill and recovery passes still run.
The one behaviour change that arrives whether you want it or not is the run time
limit: **`TCP_ANALYZE_TIMEOUT_MIN` defaults to 10 minutes**, and a large survey
will exceed it. Time a real run first, then set the two values, or set
`TCP_RUN_TIMEOUT_ENABLED=false`.

**Moving to Postgres** — a fresh database; there is no data migration.

```bash
# 1. set TCP_DB_NAME / TCP_DB_USER / TCP_DB_PASSWORD in .env
docker compose --profile postgres up -d db
# 2. create the schema. The API will NOT do this for you on Postgres.
docker compose run --rm api alembic upgrade head
docker compose --profile postgres up -d
```

Check `GET /readyz` afterwards. `"degraded": true` means Postgres was configured,
could not be reached, and the service came up on SQLite — fix the database and
restart rather than working in that state, because nothing written during it
reaches Postgres. Once it is trusted, set `TCP_DB_FALLBACK_SQLITE=false` so the
service refuses to start instead.

Schema changes from here on are Alembic's on Postgres: `pull`,
`alembic upgrade head`, `up -d`. The API warns at startup if it finds a Postgres
database with no `alembic_version` table.
