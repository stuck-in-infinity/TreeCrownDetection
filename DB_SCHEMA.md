# Database schema

SQLite, one file at `data/treecrown.db` (`TCP_DATABASE_URL`). Five tables. Every
primary key is a UUID string, not an autoincrement integer, so an id is unique
across projects and safe to put in a URL.

Written from `code/app/db/models.py`. `Base.metadata.create_all` builds it on
first boot; `_migrate_sqlite_add_columns` in `code/app/db/session.py` adds
columns to a database that already exists.

---

## Overview

```
                    projects
                   (1 survey)
                        |
      +---------+-------+--------+------------+
      |         |                |            |
   orthos     runs             jobs      cluster_labels
 (library)  (attempts)      (compute)      (species)
      |         |                              |
      +----->---+                              |
      ortho_id  +--------------->--------------+
                              run_id
```

* A **project** holds a library of orthomosaics and a history of runs.
* A **run** is one pass of the pipeline over **one** orthomosaic.
* **cluster_labels** are the species names the user assigned, and they belong to
  a run, not to the project.
* **jobs** record a compute attempt and double as the idempotency claim.

---

## projects

The survey. Also holds two things that are not really project-level, and the
comments say why.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | VARCHAR | no | PK, uuid |
| `user_id` | VARCHAR | no | indexed. The `X-User-Email` header, or `"default"`. **Unverified** — see Caveats |
| `name` | VARCHAR | no | user's label for the survey |
| `model_key` | VARCHAR | no | mirror of the active run's detector |
| `state` | VARCHAR | no | indexed. Two jobs: the busy **lock**, and a **mirror** of the active run |
| `source_epsg` | INTEGER | yes | set only when the GeoTIFF carries no CRS |
| `params` | JSON | no | mirror of the active run's parameters |
| `recommended_k` | INTEGER | yes | mirror |
| `available_k` | JSON | yes | mirror |
| `current_run` | INTEGER | no | which `work/run_<n>` a new analyze writes into |
| `run_name` | VARCHAR | yes | mirror |
| `runs` | JSON | yes | **legacy** run history. The `runs` table is the record now; this is kept so older readers do not break |
| `share_hash` | VARCHAR | yes | FileBrowser share id for the project folder |
| `consent` | INTEGER | no | 0 none, 1 all data, 2 step-1 crowns only |
| `consent_at` | DATETIME | yes | |
| `pruned_at` | DATETIME | yes | set by `scripts/run_retention.py` once a consent=2 project is pruned |
| `error` | TEXT | yes | JSON from `core.failures.classify` |
| `created_at` | DATETIME | no | |
| `updated_at` | DATETIME | no | `onupdate` — bumped by **any** write, including a state change |

Indexes: `ix_projects_user_id`, `ix_projects_state`.

**On `state` doing two jobs.** It is the mutual-exclusion lock — one computing
run per project, enforced by the conditional UPDATE in
`services/state.transition_if` — and a derived mirror of whichever run is
current. That compromise is what let the runs table ship in stages. The honest
end state is a `busy_run_id` column and no project-level state at all.

---

## orthos

The orthomosaic library. **Append-only**: there is no route that deletes one, by
product decision. The only path that removes ortho files is deleting the whole
project.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | VARCHAR | no | PK, uuid |
| `project_id` | VARCHAR | no | indexed, FK -> `projects.id` |
| `stem` | VARCHAR | no | name on disk, suffixed if it collided |
| `filename` | VARCHAR | no | name the user uploaded |
| `width`, `height` | INTEGER | yes | pixels, read from the raster |
| `crs` | VARCHAR | yes | as read from the file |
| `bands` | INTEGER | yes | only the first three reach the detector |
| `size_bytes` | INTEGER | yes | counts against the project quota |

Index: `ix_orthos_project_id`.

---

## runs

One analysis run and everything that belongs to it alone.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | VARCHAR | no | PK, uuid. What the API and frontend address |
| `project_id` | VARCHAR | no | indexed, FK -> `projects.id` |
| `number` | INTEGER | no | the `n` in `work/run_<n>`. What the pipeline, the DAG and `project_paths()` use |
| `ortho_id` | VARCHAR | yes | indexed, FK -> `orthos.id`. NULL = input not recorded |
| `name` | VARCHAR | yes | the user's run name |
| `state` | VARCHAR | no | indexed. **The truth** about this run |
| `model_key` | VARCHAR | yes | detector weights used |
| `params` | JSON | no | this run's parameters, including `ortho_id` |
| `recommended_k` | INTEGER | yes | what the app suggested |
| `available_k` | JSON | yes | the k values tried |
| `chosen_k` | INTEGER | yes | what the user picked |
| `error` | TEXT | yes | classified failure JSON |
| `created_at` | DATETIME | no | |
| `started_at` | DATETIME | yes | set when the run enters a heavy stage |
| `finished_at` | DATETIME | yes | set on COMPLETED / FAILED / AWAITING_LABELS |
| `updated_at` | DATETIME | no | `onupdate` |

Constraint: `UNIQUE(project_id, number)` — `uq_runs_project_number`. Two rows
with the same number would be two rows claiming the same directory on disk.

Indexes: `ix_runs_project_id`, `ix_runs_ortho_id`, `ix_runs_state`.

**Two identities on purpose.** `number` stays the on-disk key so nothing in the
pipeline had to change. `id` is what the API uses, because a uuid cannot be
confused with a different project's run 2.

**Why `ortho_id` may be NULL.** A run from before the ortho library existed has
no pinned input. It is left NULL and shown as "orthomosaic not recorded", and
`GET /project/runs?ortho_id=` returns it under **no** filter rather than all of
them. Filing somebody's run under the wrong survey is worse than admitting the
gap.

---

## cluster_labels

The species name the user assigned to each cluster. One row per cluster.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | VARCHAR | no | PK, uuid |
| `project_id` | VARCHAR | no | indexed, FK -> `projects.id` |
| `run_id` | VARCHAR | yes | indexed, FK -> `runs.id`. Nullable only so the ALTER could land; every new row sets it |
| `chosen_k` | INTEGER | no | how many clusters this mapping covers |
| `cluster_id` | INTEGER | no | 0 .. chosen_k-1 |
| `species` | VARCHAR | no | normalised: `Non Acacia`, `non-acacia`, `non_acacia` all become `non_acacia` |
| `notes` | TEXT | yes | free text |

Indexes: `ix_cluster_labels_project_id`, `ix_cluster_labels_run_id`.

**These used to be deleted.** `archive_current_run` ran
`db.query(ClusterLabel).filter_by(project_id=...).delete()` every time a new run
opened, which threw away the most expensive thing the user produces and is the
single reason an earlier run could never be picked up and finished later.
Labels now carry `run_id` and are kept.

---

## jobs

One compute attempt. Also the idempotency claim for the `/compute/*` callbacks.

| Column | Type | Null | Notes |
|---|---|---|---|
| `id` | VARCHAR | no | PK, uuid |
| `project_id` | VARCHAR | no | indexed, FK -> `projects.id` |
| `type` | VARCHAR | no | `analyze` or `finalize` |
| `state` | VARCHAR | no | `QUEUED` / `RUNNING` / `SUCCEEDED` / `FAILED` |
| `current_stage` | VARCHAR | yes | e.g. `detecting`, `extracting_features` |
| `progress` | FLOAT | no | 0.0 .. 1.0 |
| `error` | TEXT | yes | traceback |
| `celery_task_id` | VARCHAR | yes | see below |
| `request_id` | VARCHAR | yes | correlation id from the HTTP request |
| `log_path` | VARCHAR | yes | path to that run's log file |
| `started_at`, `finished_at` | DATETIME | yes | |

Constraint: `UNIQUE(project_id, celery_task_id)` — `uq_jobs_project_task`.
Index: `ix_jobs_project_id`.

**`celery_task_id` carries three different things**, and telling them apart is
load-bearing:

| Value | Meaning |
|---|---|
| `local:<job_id>` | ran as a daemon thread in this process. A restart killed it |
| `compute:<key>` | an inline `/compute/*` claim, namespaced by `services/job_claim.py` |
| anything else | an Airflow `dag_run_id` |

`startup_recovery` uses that prefix to decide what a restart may safely release.

**The unique index is a lock, not a nicety.** Three endpoints run the pipeline
in-process and each wipes `work/run_<n>/` before starting, so two at once
destroy each other's output. The project state machine cannot exclude them (the
trigger has already moved the project into ANALYZING, so it is an allowed source
state for every caller). Instead the INSERT itself decides: the loser gets an
`IntegrityError` and backs off. A `SELECT`-then-`INSERT` would let both through.

---

## Relations

| From | To | Kind | Notes |
|---|---|---|---|
| `orthos.project_id` | `projects.id` | many-to-one | cascade delete |
| `runs.project_id` | `projects.id` | many-to-one | cascade delete |
| `runs.ortho_id` | `orthos.id` | many-to-one | nullable |
| `jobs.project_id` | `projects.id` | many-to-one | cascade delete |
| `cluster_labels.project_id` | `projects.id` | many-to-one | cascade delete |
| `cluster_labels.run_id` | `runs.id` | many-to-one | nullable |

Deleting a project removes its orthos, runs, jobs and labels
(`cascade="all, delete-orphan"` on the ORM side).

**`jobs` has no link to `runs`.** A job knows its project, not its run. Which
run it was for is passed as a dispatch argument and never stored. Fine today —
nothing queries job history per run — but it is the obvious next column if you
ever want that.

---

## Caveats

**No users table.** `projects.user_id` is a bare string taken from the
`X-User-Email` header, which is accepted unverified by explicit decision.
Anyone who can reach the API can claim to be anyone.

**Deriving the disk path.** Run outputs live at
`<storage>/<project_id>/work/run_<number>/`, and the FileBrowser link is
`<share_url>/work/run_<number>`. Neither path is stored in the database — both
are computed from `project.id` and `runs.number`.

**`projects.runs` (JSON) and the `runs` table overlap.** The table is the
record. The JSON is written alongside it so anything reading the old shape
keeps working, and `GET /project/runs` falls back to it only for a project with
no run rows yet.

**`updated_at` means last activity, not creation.** Any write bumps it,
including a state transition, so a project that had a run started today sorts to
the top of `/projects/mine` regardless of when it was created.

---

## Full DDL

Exactly what `create_all` produces, dumped from a fresh database:

```sql
CREATE TABLE cluster_labels (
	id VARCHAR NOT NULL, 
	project_id VARCHAR NOT NULL, 
	run_id VARCHAR, 
	chosen_k INTEGER NOT NULL, 
	cluster_id INTEGER NOT NULL, 
	species VARCHAR NOT NULL, 
	notes TEXT, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id), 
	FOREIGN KEY(run_id) REFERENCES runs (id)
);

CREATE TABLE jobs (
	id VARCHAR NOT NULL, 
	project_id VARCHAR NOT NULL, 
	type VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	current_stage VARCHAR, 
	progress FLOAT NOT NULL, 
	error TEXT, 
	celery_task_id VARCHAR, 
	request_id VARCHAR, 
	log_path VARCHAR, 
	started_at DATETIME, 
	finished_at DATETIME, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_jobs_project_task UNIQUE (project_id, celery_task_id), 
	FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE TABLE orthos (
	id VARCHAR NOT NULL, 
	project_id VARCHAR NOT NULL, 
	stem VARCHAR NOT NULL, 
	filename VARCHAR NOT NULL, 
	width INTEGER, 
	height INTEGER, 
	crs VARCHAR, 
	bands INTEGER, 
	size_bytes INTEGER, 
	PRIMARY KEY (id), 
	FOREIGN KEY(project_id) REFERENCES projects (id)
);

CREATE TABLE projects (
	id VARCHAR NOT NULL, 
	user_id VARCHAR NOT NULL, 
	name VARCHAR NOT NULL, 
	model_key VARCHAR NOT NULL, 
	state VARCHAR NOT NULL, 
	source_epsg INTEGER, 
	params JSON NOT NULL, 
	recommended_k INTEGER, 
	available_k JSON, 
	current_run INTEGER NOT NULL, 
	run_name VARCHAR, 
	runs JSON, 
	share_hash VARCHAR, 
	consent INTEGER NOT NULL, 
	consent_at DATETIME, 
	pruned_at DATETIME, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE runs (
	id VARCHAR NOT NULL, 
	project_id VARCHAR NOT NULL, 
	number INTEGER NOT NULL, 
	ortho_id VARCHAR, 
	name VARCHAR, 
	state VARCHAR NOT NULL, 
	model_key VARCHAR, 
	params JSON NOT NULL, 
	recommended_k INTEGER, 
	available_k JSON, 
	chosen_k INTEGER, 
	error TEXT, 
	created_at DATETIME NOT NULL, 
	started_at DATETIME, 
	finished_at DATETIME, 
	updated_at DATETIME NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_runs_project_number UNIQUE (project_id, number), 
	FOREIGN KEY(project_id) REFERENCES projects (id), 
	FOREIGN KEY(ortho_id) REFERENCES orthos (id)
);

CREATE INDEX ix_cluster_labels_project_id ON cluster_labels (project_id);

CREATE INDEX ix_cluster_labels_run_id ON cluster_labels (run_id);

CREATE INDEX ix_jobs_project_id ON jobs (project_id);

CREATE INDEX ix_orthos_project_id ON orthos (project_id);

CREATE INDEX ix_projects_state ON projects (state);

CREATE INDEX ix_projects_user_id ON projects (user_id);

CREATE INDEX ix_runs_ortho_id ON runs (ortho_id);

CREATE INDEX ix_runs_project_id ON runs (project_id);

CREATE INDEX ix_runs_state ON runs (state);

CREATE UNIQUE INDEX uq_jobs_project_task ON jobs (project_id, celery_task_id);
```
