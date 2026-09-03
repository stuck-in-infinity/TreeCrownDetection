# Tree-Crown Species Pipeline

Detect individual tree crowns from a drone orthomosaic, cluster them by
appearance, label clusters with species, and export a georeferenced **KMZ +
CSVs**. Runs as a **FastAPI backend + static web UI** (Docker), with **optional
Airflow** orchestration and **optional FileBrowser** output sharing.

Docker Hub images (Python env + deps only; app code, `data/`, and model weights
are bind-mounted from this folder at run time — keep the folder together):

```
uavforaliens/treecrown-workstation : cu128    (backend API, CUDA build)
anunay12/treecrown-frontend        : latest   (web UI)
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
| `TCP_AUTH_ENABLED` | `true` requires an `X-User-Email` on every call (§5). Leave `false` for local use. |
| `TCP_THUMBS_PER_CLUSTER` | Crowns per cluster given a thumbnail during analysis (default 5). `0` renders them on demand instead. |
| `TCP_AIRFLOW_BASE_URL` | Blank runs the pipeline in this process (§6). |
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

## 6. Airflow (optional — "full loop")

On the Airflow machine:
1. Copy `airflow/dags/drone_analyze_dag.py` and
   `airflow/dags/drone_finalize_dag.py` into your
   Airflow `dags/`.
2. Set on the Airflow worker: `DRONE_API_BASE=http://<this-PC-ip>:8123`
   (port 8123 reachable from Airflow).
3. In `.env`, set `TCP_AIRFLOW_BASE_URL` + username/password, restart.

Leave `TCP_AIRFLOW_BASE_URL` blank to run the pipeline in-process (no Airflow).
FileBrowser being down does **not** fail the pipeline.

---

## 7. FileBrowser (optional — output sharing)

`docker-compose.hub.yml` runs FileBrowser on **:8098** over
`data/storage/projects`. With `TCP_FILEBROWSER_*` set, each project folder also
gets a public share, and the UI offers "Browse output files" and a per-run
"Open run folder" link.

This is for browsing raw outputs only. The review panel used to pull its plots
from the FileBrowser share, which broke whenever FileBrowser was off or served
from another origin; it now reads everything from the API, so the whole
labelling flow works with FileBrowser disabled.

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
  request.
- Every error record carries `request_id`, `user_email`, `project_id`,
  `dag_run_id`, stage, and IST timestamps, so an auditor can trace who ran what,
  when it started/failed, and why.

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
- **`DISPATCH_FAILED` on Analyze** → wrong or unreachable `TCP_AIRFLOW_*`. Leave
  `TCP_AIRFLOW_BASE_URL` blank to run in-process. The message classifies the
  cause (connection refused / timed out / DNS / no response).
- **Airflow DAG fails calling back** → `DRONE_API_BASE` on the Airflow worker
  must be `http://<this-PC-ip>:8123` and reachable. Check from the container
  with `docker compose exec api curl http://host.docker.internal:8080/health`.
- **A project is stuck in `ANALYZING` after a restart** → it is released on the
  next boot and marked failed so it can be re-run. If it persists, check that
  `TCP_STARTUP_RECOVERY_ENABLED` is not `false`.
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
- `docs/docker_instruction.md` — image build and publish notes.
- `project_outline.md` — architecture with diagrams.
- `DB_SCHEMA.md` — tables and columns.
