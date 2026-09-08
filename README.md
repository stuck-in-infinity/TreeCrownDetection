# Tree-Crown Species Pipeline

Detect individual tree crowns from a drone orthomosaic, cluster them by
appearance, label clusters with species, and export a georeferenced **KMZ +
CSVs**. Runs as a **FastAPI backend + static web UI** (Docker), with **optional
Airflow** orchestration and **optional FileBrowser** output sharing.

Docker Hub images (Python env + deps only; app code, `data/`, and model weights
are bind-mounted from this folder at run time — keep the folder together):

```
uavforaliens/treecrown-workstation : cu128    (backend API, CUDA build)
uavforaliens/treecrown-frontend    : latest   (web UI)
```

Default ports: **8123** backend API, **8200** web UI, **8098** FileBrowser.
A CUDA-capable NVIDIA GPU is expected; see §3c to run on CPU.

---

## 1. What you need on the machine

- **Docker Engine + `docker compose` v2** (or Docker Desktop).
- **An NVIDIA GPU**, with the driver and the NVIDIA Container Toolkit installed.
  Both compose files reserve one (`driver: nvidia`), and the published API image
  is a CUDA build (`:cu128`). To run without a GPU see §3c.
- **This folder**, kept together — the images carry only the Python environment;
  `code/`, `data/`, `frontend/` and the model weights are bind-mounted from here
  at run time. `data/hf-cache/` matters as much as the rest: the DINOv2 feature
  model is already cached there and `.env` sets `HF_HUB_OFFLINE=1`, so a run
  needs no HuggingFace download. Copy `data/` along with the folder, or clear
  `HF_HUB_OFFLINE` and let it fetch the model once.
- **The detector weight files** (`.pth`), from the maintainer — they are not in
  any image. `code/models.yaml` is the catalog; as shipped it names five:
  ```
  urban_trees_Cambridge_20230630.pth     key: urban_cambridge   (default)
  220723_withParacouUAV.pth              key: paracou
  230103_randresize_full.pth             key: randresize
  250312_flexi.pth                       key: flexi
  250711_tropical_closed_canopy.pth      key: tropical_closed
  ```
  You only need the ones you intend to use; a project picks one by `model_key`.
- **Internet once**, to pull the two images. After that the stack runs offline:
  the detector weights are local files and the DINOv2 model is pre-cached.

---

## 2. Configure — the two files you create yourself

Both are git-ignored, so every deployment sets its own.

### a. `.env` — backend and compose settings

```bash
cp .env.example .env
```

The template runs as-is on a machine with a GPU. The one value to check is the
weights folder:

```bash
# Host folder holding the .pth files, mounted read-only at /models.
# Compose refuses to start if this is empty.
HOST_MODELS_DIR=./models
```

Leave `IMAGE_API` / `IMAGE_FRONTEND` commented out unless you are pinning a
specific tag — each compose file then picks its own correct default (§3).

Other settings worth knowing (all documented inline in `.env.example`):

| Setting | Meaning |
|---|---|
| `TCP_API_PORT`, `TCP_FRONTEND_PORT` | Host ports. Honoured by `docker-compose.yml` only — the hub file hardcodes 8123/8200. |
| `TCP_AUTH_ENABLED` | `true` requires an identity on every call — the `X-User-Email` header, or `?user=` for the URLs the browser fetches by itself (§5). Leave `false` for local use. |
| `TCP_THUMBS_PER_CLUSTER` | Crowns per cluster given a thumbnail during analysis (default 5). `0` renders them on demand instead. |
| `TCP_AIRFLOW_BASE_URL` | Blank runs the pipeline in this process. **`.env.example` ships this set, so a fresh copy has Airflow ON** — blank it unless you have read §6. |
| `TCP_ANALYZE_DAG_ID`, `TCP_FINALIZE_DAG_ID`, `TCP_DRONE_DAG_ID` | Which DAG each trigger starts (§6b). |
| `TCP_COMPUTE_TOKEN` | Shared secret on the `/compute/*` callbacks; must equal `DRONE_SERVICE_TOKEN` on the Airflow worker (§6). |
| `TCP_FILEBROWSER_*` | Optional output sharing (§7). |
| `TCP_STARTUP_RECOVERY_ENABLED` | On boot, releases runs a restart killed. Leave `true`. |

### b. `frontend/config.js` — what the browser talks to

```bash
cp frontend/config.js.example frontend/config.js
```

```js
window.GOOGLE_CLIENT_ID = "xxxx.apps.googleusercontent.com";  // §5; public value
window.API_BASE = "http://localhost:8123";
```

**`API_BASE` is the setting people get wrong.** The UI container serves static
files and does **not** proxy to the API, so with the default ports the page is
on `:8200` and the API on `:8123` — two different origins. The template ships
`window.API_BASE = ""` (same origin), which is correct **only** when you have
put the UI and `/api/` behind one reverse proxy. For a plain
`docker compose up`, set it to the API's address:

- same machine → `http://localhost:8123`
- another machine on the LAN → `http://<host-ip>:8123` (not `localhost`, which
  would mean the viewer's own computer)

Leave `GOOGLE_CLIENT_ID` at its placeholder to skip sign-in during setup — the
gate bypasses itself, so the pipeline stays usable.

---

## 3. Start

### a. Pull and run the published images (normal case)

```bash
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d
docker compose -f docker-compose.hub.yml ps
```

Starts three containers: **api** (`:8123`), **frontend** (`:8200`) and
**filebrowser** (`:8098`). Ports are fixed in this file; edit it to change them.

### b. Build locally instead

Use this when you have changed the Dockerfile or need a CPU build:

```bash
docker compose build
docker compose up -d
```

This file honours `TCP_API_PORT` / `TCP_FRONTEND_PORT` from `.env`, and has no
FileBrowser service.

### c. Running without an NVIDIA GPU

Both compose files request a GPU, and the published image is a CUDA build. For a
CPU-only machine, build locally with the CPU wheels and drop the GPU
reservation:

```bash
# in .env
TORCH_INDEX=https://download.pytorch.org/whl/cpu
```

then delete the `deploy: resources: reservations: devices:` block from the `api`
service in `docker-compose.yml` and run `docker compose build`. Detection and
feature extraction are considerably slower but the pipeline is unchanged.

### d. Check it came up

```bash
curl http://localhost:8123/livez                       # {"status":"ok"}
curl http://localhost:8123/api/v1/detectors            # the weights it can see
docker compose -f docker-compose.hub.yml logs -f api   # follow the log
```

`/api/v1/detectors` lists the whole catalog from `code/models.yaml` either way;
what matters is the `"available"` flag on each entry. All `false` means
`HOST_MODELS_DIR` is pointing somewhere without the `.pth` files.

On first boot the API creates the SQLite database and its tables under `data/`,
so nothing else needs preparing.

### e. What is switched on by default

Both optional services are **off as far as the backend is concerned** in
`settings.py`, and the full pipeline works that way — upload, analyze, review,
label, finalize, download. Neither adds a capability the pipeline needs; they
add orchestration and file browsing.

| | Airflow (§6) | FileBrowser (§7) |
|---|---|---|
| Enabled by | `TCP_AIRFLOW_BASE_URL` non-blank | `TCP_FILEBROWSER_BASE_URL` non-blank |
| Code default | blank — compute runs in the API process, in a background thread | blank — no shares are created |
| **What `.env.example` ships** | **set** to `http://host.docker.internal:8080` — so a fresh `cp` turns Airflow **on** | blank |
| Container in `docker-compose.hub.yml` | none, it is external | yes, `:8098` — but the backend ignores it until the `TCP_FILEBROWSER_*` values are set |
| Container in `docker-compose.yml` | none | none |
| If it breaks mid-run | the run stalls; releasing it at next boot needs Airflow to answer first (§6g) | nothing fails; links are simply absent |

Two things to settle before your first run:

- **`.env.example` enables Airflow.** Copy it and `TCP_AIRFLOW_BASE_URL` is
  already pointing at `host.docker.internal:8080`. If nothing is listening there
  every Analyze fails with `DISPATCH_FAILED`. Comment the line out for a plain
  single-machine install.
- **The web UI's buttons need a DAG this repo does not ship.** Even with Airflow
  running correctly, *Run analysis* and *Finalize* trigger `drone_pipeline`,
  which is not here. See §6b.

---

## 4. Use it

Open **http://localhost:8200** (or `http://<host-ip>:8200`). With sign-in
configured you get the landing page → **Sign in with Google**; otherwise you
land straight on the pipeline.

**Create project → upload orthomosaic → configure and analyze → review clusters
→ name the groups → finalize and export.**

A project holds a **library of orthomosaics** and a history of **runs**. One run
uses one orthomosaic, so you can add a second survey later and analyze it
without disturbing the first.

**Review clusters**, after Analyze, shows for the run you are looking at:
the k-selection plot, a button per k, and for the chosen k its t-SNE plot plus a
row per cluster with the crowns nearest that cluster's centre — the most typical
ones, not the first few by filename. Click any crown to see it full size. All of
it is served by the API, so it works whether or not FileBrowser is enabled.

**Naming the groups:** set **Chosen k** (prefilled with the recommendation) and
type your species names; the table fills in as you go → **Submit labels**.

**Past runs:** every orthomosaic in step 2 lists the runs made from it. Opening
one puts that run's settings back into step 3, ticks its orthomosaic, and shows
that run's clusters. From there, **Run analysis** starts a *new* run from those
settings, and the run list records where it came from ("from run 3"). An older
run can be labelled and exported at any time, including while a newer one is
still analysing.

After Finalize: KMZ and CSV downloads, the distribution summary, and the
**data-sharing consent** box (§8).

---

## 5. Google sign-in (SSO, audit-only)

Client-side Google Identity Services (GIS) token flow. The frontend gets the
user's email and sends it as `X-User-Email`; the backend logs **who triggered
what** and scopes projects per user. The token is **not** verified server-side
(audit-only) — safe only **behind a gateway / internal network**.

Setup:
1. Google Cloud Console → **OAuth Client ID** (type: Web application).
   **Authorized JavaScript origins** = your site origin (scheme+host, **no path**),
   e.g. `https://www.cse.iitd.ernet.in`. Leave **redirect URIs** empty.
2. Configure the OAuth consent screen (Internal if Workspace-only).
3. Put the client ID in `frontend/config.js`.
4. Set `TCP_AUTH_ENABLED=true` and restart. (Set both together — client id alone
   gates the UI but not the API; `auth_enabled` alone 401s every call.)

With the placeholder client id, the gate auto-bypasses so dev stays usable.
Deploy behind HTTPS + a same-origin reverse proxy (UI + `/api/`) to avoid CORS.
For a public-facing API, switch to server-side token verification: the backend
already carries `TCP_GOOGLE_CLIENT_ID` for it, and the frontend already holds
the ID token — what is missing is the verification step in `code/app/api/deps.py`.

---

## 6. Airflow (optional — orchestration)

Airflow **performs no computation**. It is a scheduler that calls back into this
API, which does all the work. Everything below is about who *starts* a run, not
who runs it.

### a. Without Airflow — the simplest working setup

```env
TCP_AIRFLOW_BASE_URL=
```

A trigger returns immediately and the pipeline runs in a background thread
inside the API container. The UI polls `GET /api/v1/project/runs/status` for the
stage and percentage. This is the right setting for a single workstation and for
any first install.

**You have to do this explicitly.** `.env.example` ships the line *set* to
`http://host.docker.internal:8080`, so a fresh `cp .env.example .env` has Airflow
on. Comment it out or blank it.

### b. With Airflow — pick a route first

There are two ways in, and they use different DAGs. Decide before you copy
anything.

| Route | DAG | Triggered by | In this repo? |
|---|---|---|---|
| **A — the shipped DAGs** | `drone_analyze`, `drone_finalize` | `POST /api/v1/project/runs/analyze`, `POST /api/v1/project/runs/{n}/finalize` | **yes**, `airflow/dags/` |
| **B — the combined DAG** | `drone_pipeline` (`TCP_DRONE_DAG_ID`) | `POST /api/v1/project/drone_api` — **what the web UI's buttons call** | **no**, you write it |

**The web UI uses route B.** Enable Airflow without supplying `drone_pipeline`
and *Run analysis* and *Finalize* fail with `502 AIRFLOW_TRIGGER_FAILED`,
because Airflow 404s on an unknown DAG id. The shipped DAGs do not cover the UI.

So pick one:

- **Keep Airflow off** if you want the UI to work as shipped. Recommended unless
  you specifically need external orchestration.
- **Route A** — drive the API directly (curl, a script, your own DAG) against
  `/runs/analyze` and `/runs/{n}/finalize`. The shipped DAGs serve exactly these.
- **Route B** — write the combined DAG, and the UI works end to end. Contract in
  §6c.

### c. Route B — what `drone_pipeline` must do

Full spec in `docs/INTEGRATION_GUIDE.md` §7 ("Airflow DAG requirements"). The
essentials:

1. **Read `conf`.** The backend sends:

   | Key | Always? | Meaning |
   |---|---|---|
   | `project_id` | yes | the project |
   | `action` | yes | `"analyze"` or `"finalize"` |
   | `execution_type` | yes | always `"fullexec"` |
   | `ortho_id` | analyze only | which orthomosaic in the project's library |
   | `params`, `model_key`, `source_epsg`, `run_name` | when set | the run's settings |

   There is deliberately **no `run`** in the conf: the run number is not decided
   until the callback runs `_apply_run_config`, so the callback derives it.
   `ortho_id` is also pinned onto the project before the DAG starts, so a DAG
   that ignores that key still works.

2. **Call back** to `POST {DRONE_API_BASE}/api/v1/project/drone_api` with
   `execution_id` set to a fresh uuid, passing `project_id` and `action`
   through.

   `execution_id` is the loop-breaker. `drone_api` triggers a DAG only when
   `execution_id` is **absent**; a callback carrying one runs the pipeline
   inline instead of scheduling itself again. Omit it and the DAG triggers
   itself forever.

The two shipped DAGs are a different, older shape — they post to `/compute/*`
and never read `action` — so they are a poor template for the flow. Copy their
request/response handling, not their structure.

### d. Setting it up

On the **Airflow** machine:

1. Copy the DAG files you need into its `dags/`. Both shipped DAGs are
   `schedule_interval=None` (trigger-only), 1 retry, 2-minute retry delay, and a
   7200 s request timeout — a large survey takes a while.
2. Set on the **worker**:
   ```env
   DRONE_API_BASE=http://<this-host-ip>:8123    # how Airflow reaches THIS API
   DRONE_SERVICE_TOKEN=<same as TCP_COMPUTE_TOKEN>   # optional
   ```
   Not `localhost` — that would be Airflow's own container. The default if unset
   is `http://host.docker.internal:8123`. The token is sent as `X-Service-Token`
   and must match `TCP_COMPUTE_TOKEN` here, or the callback gets a 401.

On **this** machine, in `.env`:

```env
TCP_AIRFLOW_BASE_URL=http://host.docker.internal:8080
TCP_AIRFLOW_USERNAME=admin
TCP_AIRFLOW_PASSWORD=...
# TCP_AIRFLOW_AUTH_TOKEN=...     # bearer token instead of basic auth
# TCP_ANALYZE_DAG_ID=drone_analyze
# TCP_FINALIZE_DAG_ID=drone_finalize
# TCP_DRONE_DAG_ID=drone_pipeline
```

`host.docker.internal` resolves because both compose files map it
(`extra_hosts`). Use a real hostname if Airflow is elsewhere. Then restart:
`docker compose -f docker-compose.hub.yml up -d api`.

**Traffic goes both ways**, which is the part that is easy to get wrong: this
API must reach Airflow on 8080, and the Airflow worker must reach this API on
8123. Check both directions before blaming the DAG:

```bash
# this API -> Airflow
docker compose -f docker-compose.hub.yml exec api \
  curl -sf http://host.docker.internal:8080/health && echo OK

# does the DAG the UI needs actually exist?
curl -su admin:<pw> http://localhost:8080/api/v1/dags/drone_pipeline | head -c 200

# Airflow worker -> this API   (run on the Airflow machine)
curl -sf http://<this-host-ip>:8123/livez && echo OK
```

### e. Running the flow with Airflow on

Route B, the UI flow, once `drone_pipeline` is in place:

1. Open the UI, create a project, upload an orthomosaic.
2. **Run analysis.** The button posts to `/project/drone_api`, which triggers
   the DAG and returns a `dag_run_id` instead of `local:<job_id>`. The UI then
   polls `GET /api/v1/project/drone_status/{dag_run_id}` and shows
   `DAG status: running (poll N)`.
3. Your DAG calls back with `execution_id`; the pipeline runs inside the API
   container. Watch it there, not in Airflow — Airflow only holds an open
   request:
   ```bash
   docker compose -f docker-compose.hub.yml logs -f api
   ```
4. The project reaches `AWAITING_LABELS`. Review clusters, name the groups,
   **Submit labels** — this is a plain API call and never involves Airflow.
5. **Finalize** goes back through the DAG the same way, with `action:"finalize"`.
6. Download the KMZ and CSVs.

Route A is the same, minus the UI: `POST /api/v1/project/runs/analyze` with
`{"project_id": "...", "ortho_id": ...}`, then label, then
`POST /api/v1/project/runs/{n}/finalize`.

### f. The callback status contract

The shipped DAGs decide what happened from the HTTP status, and additionally
require `status == "success"` in the JSON body:

| Status | DAG result |
|---|---|
| `200` + `status:"success"` | success; `asset_id` is pushed to XCom |
| `400` / `404` | `AirflowSkipException` — a graceful skip, not a failure |
| anything else | task fails, and the DAG run fails |

A callback that loses the concurrency claim gets a `400`, i.e. a skip — correct,
because the caller that won is producing the asset.

Retries are safe: the DAG sends `dag_run_id` as `Idempotency-Key`, so a repeat
of a run that already succeeded replays the stored result instead of recomputing
it.

### g. What else changes once Airflow is on

**Start-up recovery behaves differently.** A run killed by a restart is released
only after Airflow is *asked* whether the DAG run is still going. If Airflow is
unreachable the run is deliberately left alone rather than wrongly declared dead
— so a project can sit in `ANALYZING` until Airflow answers. With Airflow off,
such runs are released immediately at boot.

### h. Turning it back off

Blank `TCP_AIRFLOW_BASE_URL` and restart the API. Runs already handed to Airflow
keep going and still call back; only new triggers change path. If a project is
stuck in `ANALYZING` from a DAG run that no longer exists, the next restart
releases it (§6g) — with Airflow unconfigured there is nothing that could still
be running it.

---

## 7. FileBrowser (optional — output sharing)

FileBrowser gives users a plain file-tree view of a project's output folder. It
is **not** used to display anything inside the app: every plot, thumbnail and
download in the review and results screens is served by this API. That was not
always true, and the change is why the labelling flow now works with FileBrowser
switched off.

### a. Without FileBrowser — the default

```env
TCP_FILEBROWSER_BASE_URL=
```

Blank, and nothing else is needed. No shares are created, `files_url` comes back
as `null`, and the UI omits the "Browse output files" and "Open run folder"
links rather than showing dead ones. Every result is still downloadable from the
results panel.

Note that `docker-compose.hub.yml` **starts a FileBrowser container anyway**, on
`:8098`, over `data/storage/projects`. With the settings blank the backend does
not know about it; it is just a file browser you can open yourself.
`docker-compose.yml` has no such service.

### b. Turning it on

1. Start the stack with the hub compose file, which already includes the
   service, and open `http://localhost:8098`. Log in with FileBrowser's default
   `admin` / `admin` and **change the password** — the backend signs in with
   these same credentials.
2. In `.env`:
   ```env
   TCP_FILEBROWSER_BASE_URL=http://filebrowser:80
   TCP_FILEBROWSER_PUBLIC_URL=http://localhost:8098
   TCP_FILEBROWSER_USERNAME=admin
   TCP_FILEBROWSER_PASSWORD=<what you just set>
   ```
   `filebrowser` is the compose service name, resolvable from the api container
   on the compose network. If you run FileBrowser outside this stack, use
   `http://host.docker.internal:8098` instead.
3. Restart the API: `docker compose -f docker-compose.hub.yml up -d api`.

**The two URLs differ on purpose, and this is the setting people get wrong.**
`BASE_URL` is what the *backend container* uses to call FileBrowser's API, so it
is a name that resolves inside Docker. `PUBLIC_URL` is what goes into the link
the *browser* opens, so it must be an address the user's machine can reach. Set
only `BASE_URL` and every share link points at a hostname that exists solely
inside Docker. On a LAN, use `http://<host-ip>:8098`.

### c. Running the flow with FileBrowser on

1. **Create the project first, with FileBrowser already configured.** The share
   is created at project-creation time, via `POST /api/share/<project_id>`
   against FileBrowser. Projects created *before* you enabled it have no share
   and do not get one retroactively — make a new project to test.
2. Upload, analyze, label and finalize exactly as normal. Nothing in the flow
   changes; FileBrowser is never in the path of a run.
3. The project row now carries a `share_hash`, and the UI shows **Browse output
   files**. `share_url` is `<public>/share/<hash>`; the per-run **Open run
   folder** link appends `/work/run_<n>`.
4. If share creation fails, the failure is logged and **the project is still
   created** — you get a project with no link, not a failed request. FileBrowser
   being down never fails a run.

> Per-run deep links are unverified against every FileBrowser build. Open one
> once and confirm it lands inside that run's folder rather than at the share
> root. If your build rejects the subpath, the fallback is one share per run.

### d. Turning it back off

Blank `TCP_FILEBROWSER_BASE_URL` and restart. Existing `share_hash` values stay
on the project rows and start working again if you re-enable it; meanwhile the
links are omitted. You can also stop just that container:

```bash
docker compose -f docker-compose.hub.yml stop filebrowser
```

---

## 8. Data-sharing consent

After Finalize the user records consent (stored on the project):
- **0 No** — data kept for a fixed policy period only, not public.
- **1 Yes, all** — all data public for training/viewing.
- **2 Yes, unlabelled only** — only the Step-1 unlabelled crown data public.
Consent is one-time — the box disappears once saved.

---

## 9. Retention cleanup (cron, consent-aware)

`code/scripts/run_retention.py` deletes/prunes projects older than
`TCP_RETENTION_DAYS` (default 30) by consent:
- **0** → delete the whole folder **and** DB row.
- **1** → retained.
- **2** → keep everything through Step 1; delete labelled outputs (step2/3/4) +
  label rows; mark `PRUNED`.

Standalone, no Celery — file-locked, idempotent, per-project commit (safe if
interrupted). Cron:
```
0 3 * * *  cd /code && python scripts/run_retention.py >> /data/storage/retention.log 2>&1
```
Dry-run first: `python scripts/run_retention.py --dry-run`. The legacy Celery
beat cleanup is OFF (`cleanup_enabled=false`).

---

## 10. Logging & audit (IST)

- Central: `/data/logs/app.log` (all levels) + `/data/logs/errors.jsonl`
  (ERROR-only, JSON) — **outside** `storage_root` so retention can't erase them.
- Per-run pipeline logs: `data/storage/projects/<id>/work/run_<n>/logs/*.log`
  (include the failure traceback).
- Audit ledger: `data/storage/activity/activity-YYYY-MM-DD.jsonl` — who/when per
  request. It records **every write** (POST/PUT/PATCH/DELETE) and the **reads
  that hand data back**: the results summary, the KMZ, both CSVs, the confusion
  matrix, the STAC item, `runs/<n>/results/<asset>`, the clustering data and its
  k-selection and t-SNE plots, individual crown images and the detection
  overlay. Ordinary polling is deliberately left out — the review screen asks
  `/project` and `/project/runs/status` every three seconds for the length of a
  run, and logging that would bury the records that matter.
- Every error record carries `request_id`, `user_email`, `project_id`,
  `dag_run_id`, stage, and IST timestamps, so an auditor can trace who ran what,
  when it started/failed, and why. `user_email` is bound by the request
  middleware and, for the two pipeline jobs, from the project's owner — so a run
  that fails deep inside the pipeline still names whose run it was.

**Identity on downloads and images.** A download link and a crown image are
fetched by the browser itself, through `<a href>` and `<img src>`, which send no
custom headers — so those URLs carry `?user=<email>` instead of `X-User-Email`.
The logging and the ledger accept either, which is what keeps audited reads from
all showing up as `anonymous`. Neither is verified here; the identity is only as
trustworthy as the gateway or network in front of the API.

**Docker logs.** Both compose files cap container stdout at three 10 MB files
per service (`json-file`, `max-size: 10m`, `max-file: 3`), so an unattended
deployment cannot fill the disk with logs. nginx access and error logs are
written to `./data/logs/nginx/` on the host as well as to stdout, so replacing
the frontend container does not lose them.

Those two nginx files are the one log in the stack nothing rotates: `app.log`
and `errors.jsonl` rotate in Python, container stdout rotates in Docker, but
nginx writes plain files and the image has no logrotate. On a busy deployment,
give the host one:

```
# /etc/logrotate.d/treecrown-nginx   (adjust the path to your checkout)
/path/to/drone_docker/data/logs/nginx/*.log {
    weekly
    rotate 8
    compress
    missingok
    notifempty
    copytruncate
}
```

`copytruncate` avoids having to signal nginx inside the container.

**Timezone:** all **logs** render in **IST**. DB timestamps are also stored in
IST (`naive_now()`), and retention compares IST-vs-IST → no drift.

---

## 11. Manage

```
Logs:          docker compose -f docker-compose.hub.yml logs -f api
Stop:          docker compose -f docker-compose.hub.yml down
Update images: docker compose -f docker-compose.hub.yml pull && \
               docker compose -f docker-compose.hub.yml up -d
```
Data (projects, DB, outputs) persists in `data/` across restarts. Deleting the
sqlite DB is safe — it is recreated empty on startup.

**Schema changes apply themselves on SQLite at boot**, so upgrading is just
`pull` + `up -d`: `init_db()` creates any missing table (including `runs`) and
adds missing columns (`jobs.request_id`, `cluster_labels.run_id`). Two start-up
passes then run, both safe to repeat and neither able to stop the service
booting: projects that predate the `runs` table get a row per run they have had,
and any run left mid-flight by a restart is released so it can be re-run.

On Postgres these are not automatic — apply the equivalent DDL once.

The database uses SQLite WAL, so `data/treecrown.db` is nearly empty on its own
and the live contents sit in `treecrown.db-wal`. **Copy all three of
`treecrown.db`, `-wal` and `-shm` together**, or the backup will look blank.

---

## 12. Troubleshooting

**Startup**

- **`required variable HOST_MODELS_DIR is missing a value`** → set it in `.env`
  to the folder holding the `.pth` files. Compose refuses to start without it.
- **`pull access denied` / `manifest unknown` on pull** → `IMAGE_API` or
  `IMAGE_FRONTEND` in `.env` is pointing at a locally-built tag. Comment both
  out and the hub file uses the published images.
- **`could not select device driver "nvidia"`** → the NVIDIA Container Toolkit
  is not installed, or there is no GPU. See §3c for the CPU route.
- **Port already in use** → with `docker-compose.yml`, set `TCP_API_PORT` /
  `TCP_FRONTEND_PORT` in `.env`. `docker-compose.hub.yml` hardcodes its ports;
  edit that file.

**The page loads but nothing works**

- **Every request fails, or the UI sits at "loading"** → `window.API_BASE` in
  `frontend/config.js` is wrong. It must be the API's address as the *browser*
  sees it (`http://<host-ip>:8123`), and `""` only works behind a reverse proxy
  that serves the UI and `/api/` from one origin. The failure message names the
  URL it tried.
- **Mixed content blocked** → the page is on HTTPS and `API_BASE` is HTTP.
  Browsers refuse this. Put both behind the same HTTPS origin.
- **API 401 everywhere** → `TCP_AUTH_ENABLED=true` but no `X-User-Email` is
  being sent (client id not set, or a non-browser client). Set the client id, or
  turn auth off for local use.
- **Sign-in popup rejected** → the browser origin is not in the OAuth client's
  Authorized JavaScript origins, or the consent screen is unpublished and you
  are not a test user.

**Running a pipeline**

- **Detector "weights missing" / Analyze fails immediately** → the `.pth` files
  are not where `HOST_MODELS_DIR` points. Check with
  `curl http://localhost:8123/api/v1/detectors` and look at `"available"` on the
  key your project uses.
- **`502 AIRFLOW_TRIGGER_FAILED` the moment you enable Airflow** → almost always
  the missing combined DAG. The UI's buttons trigger `drone_pipeline`
  (`TCP_DRONE_DAG_ID`), which this repo does not ship — the two DAGs in
  `airflow/dags/` are `drone_analyze` and `drone_finalize`, and they serve the
  `/runs/*` endpoints instead. See §6b: supply that DAG, drive `/runs/*`
  directly, or leave Airflow off. Confirm with
  `curl -su admin:<pw> <airflow>/api/v1/dags/drone_pipeline` — a 404 is this.
- **`DISPATCH_FAILED` on Analyze, on a machine with no Airflow** → `.env.example`
  ships `TCP_AIRFLOW_BASE_URL` **set**, so a fresh copy points at
  `host.docker.internal:8080` with nothing listening. Comment the line out to run
  in-process (§6a). The message classifies the cause (connection refused / timed
  out / DNS / no response).
- **Airflow DAG fails calling back** → `DRONE_API_BASE` on the Airflow worker
  must be `http://<this-PC-ip>:8123` and reachable — not `localhost`. Check both
  directions with the three curls in §6d. A `401` on the callback means
  `DRONE_SERVICE_TOKEN` does not match `TCP_COMPUTE_TOKEN`.
- **A project is stuck in `ANALYZING` after a restart** → with Airflow off it is
  released on the next boot and marked failed so it can be re-run. With Airflow
  on, the run is released only once Airflow confirms the DAG run has finished;
  while Airflow is unreachable it is deliberately left alone rather than being
  wrongly declared dead (§6g). If it persists either way, check that
  `TCP_STARTUP_RECOVERY_ENABLED` is not `false`.
- **No "Browse output files" link** → either `TCP_FILEBROWSER_BASE_URL` is blank,
  or the project was created *before* FileBrowser was configured. Shares are made
  at project-creation time and never backfilled (§7c) — create a new project.
- **The share link opens a hostname the browser cannot resolve** →
  `TCP_FILEBROWSER_PUBLIC_URL` is unset, so the link fell back to the internal
  `BASE_URL`. Set it to an address the user's machine can reach (§7b).
- **A crown image shows "not available"** → that one GeoTIFF could not be
  rendered. The API logs the path at ERROR; the rest of the review is unaffected.
- **First Analyze fails trying to reach huggingface.co** → `data/hf-cache/` is
  missing or empty while `HF_HUB_OFFLINE=1`. Restore the folder, or clear that
  variable in `.env` and allow one download.

**Where to look**

`docker compose logs -f api`, then `data/logs/app.log` and
`data/logs/errors.jsonl`, then the per-run log under
`data/storage/projects/<id>/work/run_<n>/logs/`. Every error carries a
`request_id` that ties the three together.

---

## Docs
- `docs/CODEBASE_MAP.md` — where to find things in the code. Read this first
  before going looking.
- `docs/INTEGRATION_GUIDE.md` — architecture and the Airflow integration.
- `docs/FRONTEND_BACKEND_FLOW.md` — what the UI calls, in order.
- `docs/PIPELINE_WALKTHROUGH.md` — the pipeline end to end.
- `docs/filebrowser_*.md` — FileBrowser setup and public shares.
- `docs/docker_instruction.md` and its copy `docs/README.md` — a shorter
  pull-and-run sheet. §1–§3 here supersede both; `publish.sh` is what builds and
  pushes the images.
- `project_outline.md` — architecture with diagrams.
- `DB_SCHEMA.md` — tables and columns.
