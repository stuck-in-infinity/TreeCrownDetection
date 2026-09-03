# Tree-Crown Species Pipeline

Detect individual tree crowns from a drone orthomosaic, cluster them by
appearance, label clusters with species, and export a georeferenced **KMZ +
CSVs**. Runs as a **FastAPI backend + static web UI** (Docker), with **optional
Airflow** orchestration and **optional FileBrowser** output sharing.

Docker Hub images (Python env + deps only; app code, `data/`, and model weights
are bind-mounted from this folder at run time — keep the folder together):

```
uavforaliens/treecrown-workstation : latest   (backend API)
uavforaliens/treecrown-frontend    : latest   (web UI)
```

Default ports: **8123** backend API, **8200** web UI.

---

## 1. What you need on the machine

- Docker Engine + `docker compose` v2 (or Docker Desktop).
- This folder, containing: `docker-compose.hub.yml`, `.env.example`,
  `code/`, `data/` (empty; DB + outputs written here), `models/` (you add the
  detector weights), `airflow/dags/` (only if using Airflow), `frontend/`.
- The three detector weight files (`.pth`) from the maintainer — NOT on Docker
  Hub:
  ```
  urban_trees_Cambridge_20230630.pth
  220723_withParacouUAV.pth
  230103_randresize_full.pth
  ```
- Internet the first time (pull images + download the DINOv2 feature model once
  on the first Analyze).

---

## 2. Configure — files you must add yourself

These are **git-ignored** and per-deployment; create them from the templates.

### `.env` (backend) — from `.env.example`
```
cp .env.example .env
```
Key settings:
```
IMAGE_API=uavforaliens/treecrown-workstation:latest
IMAGE_FRONTEND=uavforaliens/treecrown-frontend:latest

# Storage + DB (persist on the mounted volume)
TCP_STORAGE_ROOT=/data/storage
TCP_DATABASE_URL=sqlite:////data/treecrown.db

# Logging — IST timestamps; lives OUTSIDE storage_root so it survives retention
TCP_LOG_DIR=/data/logs
TCP_LOG_JSON=true          # JSON in prod, text (false) for dev

# Google sign-in gate (see §5). Leave false to keep the API open (dev).
TCP_AUTH_ENABLED=true

# Airflow (optional, §6) — leave blank to run the pipeline in-process
TCP_AIRFLOW_BASE_URL=
TCP_AIRFLOW_USERNAME=
TCP_AIRFLOW_PASSWORD=

# FileBrowser (optional, §7) — output share links
TCP_FILEBROWSER_BASE_URL=
TCP_FILEBROWSER_PUBLIC_URL=
TCP_FILEBROWSER_PASSWORD=
```

### `frontend/config.js` (frontend) — from `frontend/config.js.example`
```
cp frontend/config.js.example frontend/config.js
```
```js
window.GOOGLE_CLIENT_ID = "xxxx.apps.googleusercontent.com";  // §5
window.API_BASE = "";   // "" = same origin; split-port dev: "http://localhost:8123"
```
The client ID is **public** (safe in the browser). The backend needs no secret
for sign-in.

### `models/*.pth`
Copy the three weight files into `models/`.

**Summary of files to add per workstation:** `.env`, `frontend/config.js`,
`models/*.pth`. Everything else ships with the repo. Ensure `data/` and
`/data/logs` are writable.

---

## 3. Start

```
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d
docker compose -f docker-compose.hub.yml ps
```

Health: `curl http://localhost:8123/livez` → `{"status":"ok"}`
Weights visible: `... exec api curl -s http://localhost:8123/api/v1/detectors`

---

## 4. Use it

Open **http://localhost:8200**. If Google sign-in is configured you get the
landing page → **Sign in with Google** → the pipeline. Then:

**Create project → Upload GeoTIFF → Configure & Analyze → Label clusters →
Finalize & export.**

- After Analyze, the **Review clusters** panel shows the k-selection + per-k
  t-SNE plots and per-k assignment CSV (pulled from the FileBrowser share when
  enabled; degrades to links if not).
- Labelling is reactive: set **Chosen k** (prefilled with the recommended value)
  + species names → the cluster table fills in automatically → **Submit labels**.
- After Finalize: KMZ + CSV downloads, distribution summary, **Data-sharing
  consent** box (see §8), and **Re-run** / **New analysis** buttons.

There is no "API base URL" field anymore — it's set once in `frontend/config.js`.

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
For a public-facing API, switch to server-side token verification (see
`docs/OAUTH_GIS_INTEGRATION_PLAN.md` §4b).

---

## 6. Airflow (optional — "full loop")

On the Airflow machine:
1. Copy `airflow/dags/drone_analyze_dag.py` + `drone_finalize_dag.py` into your
   Airflow `dags/`.
2. Set on the Airflow worker: `DRONE_API_BASE=http://<this-PC-ip>:8123`
   (port 8123 reachable from Airflow).
3. In `.env`, set `TCP_AIRFLOW_BASE_URL` + username/password, restart.

Leave `TCP_AIRFLOW_BASE_URL` blank to run the pipeline in-process (no Airflow).
FileBrowser being down does **not** fail the pipeline.

---

## 7. FileBrowser (optional — output sharing)

When `TCP_FILEBROWSER_*` is set, each project folder gets a public share; the UI
shows a "Browse output files" link and can inline plots/CSVs from the share raw
endpoint `.../api/public/dl/{hash}/{path}`. Entirely optional — the pipeline and
backend `/results/*` downloads work without it.

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
sqlite DB is safe — it's recreated empty on startup (`init_db()`), and the new
`Job.request_id` column auto-migrates on SQLite (on Postgres run
`ALTER TABLE jobs ADD COLUMN request_id VARCHAR;` once).

---

## 12. Troubleshooting

- **Detector "weights missing" / Analyze fails** → `.pth` files not in `models/`.
- **`DISPATCH_FAILED` on Analyze** → wrong/absent `TCP_AIRFLOW_*` in `.env`, or
  Airflow down. Leave the URL blank to run without Airflow. The error message now
  classifies the cause (connection refused / timed out / DNS / no response).
- **Sign-in popup rejected** → the browser origin isn't in Authorized JavaScript
  origins, or the consent screen isn't published / you're not a test user.
- **API 401 everywhere** → `TCP_AUTH_ENABLED=true` but no `X-User-Email`
  (frontend client id not set, or a non-browser client). Set the client id, or
  turn auth off for dev.
- **Airflow DAG task fails calling back** → `DRONE_API_BASE` must be
  `http://<this-PC-ip>:8123` and reachable.
- **Port already in use** → change the host side of `8123:8000` / `8200:80` in
  `docker-compose.hub.yml`.
- **Reach Airflow from container** →
  `... exec api curl http://host.docker.internal:8080/health`.

---

## Docs
- `docs/OAUTH_GIS_INTEGRATION_PLAN.md` — Google sign-in design + security.
- `docs/RETENTION_CONSENT_CLEANUP_PLAN.md` — retention/consent cleanup.
- `docs/ERROR_LOGGING_PLAN.md` — error logging + audit trail.
- `docs/INTEGRATION_GUIDE.md` — architecture + Airflow integration deep-dive.
- `CODEBASE_MAP.md` — file/endpoint/keyword index.
