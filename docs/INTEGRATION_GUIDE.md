# Tree-Crown Pipeline — Integration Guide

Complete guide covering project architecture, important files, Airflow integration, and API call graph.

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Architecture](#2-architecture)
3. [Directory Structure](#3-directory-structure)
4. [Important Files](#4-important-files)
5. [Environment Variables](#5-environment-variables)
6. [Docker Setup](#6-docker-setup)
7. [Airflow Integration](#7-airflow-integration)
8. [API Reference & Call Graph](#8-api-reference--call-graph)
9. [Project State Machine](#9-project-state-machine)
10. [Running Locally (Without Airflow)](#10-running-locally-without-airflow)
11. [Running With Airflow](#11-running-with-airflow)
12. [Troubleshooting](#12-troubleshooting)

---

## 1. Project Overview

Tree-Crown Pipeline is a FastAPI backend + static frontend that:
- Accepts drone orthomosaic uploads
- Runs crown detection (Detectree2) + feature extraction (DINOv2) + clustering (KMeans)
- Lets users label clusters with species names
- Runs finalization (species assignment + KMZ export)

Compute is optionally orchestrated via **Apache Airflow**. When Airflow is not configured, everything runs in-process.

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     Browser (port 8200)                  │
│                  frontend/index.html                     │
│              Served by nginx container                   │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP (via nginx proxy_pass)
                         ▼
┌─────────────────────────────────────────────────────────┐
│              FastAPI Backend (port 8123)                 │
│              code/app/  — volume mounted                 │
│                                                          │
│   POST /project/drone_api  ──► triggers Airflow DAG     │
│   GET  /project/drone_status/{id}  ──► polls Airflow    │
│   POST /project/drone_api (with execution_id) ──► runs  │
│                                  pipeline directly       │
└──────────────┬──────────────────────────────────────────┘
               │ urllib REST calls
               ▼
┌─────────────────────────────────────────────────────────┐
│               Apache Airflow (external)                  │
│         http://corestac-stacd-airflow:8080               │
│                                                          │
│   DAG: drone_pipeline                                    │
│     └─► Drone_Algo task                                  │
│           └─► POST /project/drone_api  (callback)        │
│                   with execution_id set                  │
└─────────────────────────────────────────────────────────┘
```

---

## 3. Directory Structure

```
drone_docker/
├── code/                          # Backend (FastAPI) — volume mounted
│   └── app/
│       ├── main.py                # FastAPI app entry point
│       ├── api/
│       │   └── v1/
│       │       ├── analyze.py     # ★ drone_api + drone_status endpoints
│       │       ├── finalize.py    # finalize pipeline logic
│       │       ├── clustering.py  # clustering review endpoints
│       │       ├── compute.py     # legacy compute callback endpoints
│       │       ├── labels.py      # label submission
│       │       ├── projects.py    # project CRUD + ortho upload
│       │       ├── results.py     # download results
│       │       └── runs.py        # legacy runs endpoints
│       ├── core/
│       │   └── settings.py        # ★ All env var config (TCP_ prefix)
│       ├── schemas/
│       │   └── project.py         # ★ AnalyzeTrigger schema (execution_id field)
│       ├── services/
│       │   └── airflow_client.py  # ★ Airflow REST client (trigger + poll)
│       └── workers/
│           └── tasks.py           # Celery tasks (ML pipeline)
├── frontend/
│   └── index.html                 # ★ Single-page UI — volume mounted
├── airflow/
│   └── dags/
│       ├── drone_analyze_dag.py   # Legacy analyze DAG
│       └── drone_finalize_dag.py  # Legacy finalize DAG
├── docker-compose.yml             # Build-from-source compose
├── docker-compose.hub.yml         # ★ Pull-from-DockerHub compose (production)
├── .env                           # ★ All config (not committed to git)
└── Dockerfile / Dockerfile.frontend
```

---

## 4. Important Files

### `code/app/api/v1/analyze.py` ★ Most Important

Contains the two unified endpoints:

**`POST /project/drone_api`** — Entry point for analyze and finalize.

```python
# Decision logic:
if airflow_enabled() and not body.execution_id:
    # Airflow configured + NOT a callback → trigger Airflow DAG
    dag_run_id = trigger_drone_dag(conf)
    return {"status": "started", "dag_run_id": dag_run_id, ...}
else:
    # No Airflow OR Airflow is calling us back with execution_id
    # → run pipeline directly in-process
    run_analyze(...) / run_finalize(...)
```

**`GET /project/drone_status/{dag_run_id}`** — Called by frontend every 5s.

```python
state = get_dag_run_state(dag_run_id)   # one Airflow check per call
if state == "success":
    db.expire_all()                     # force fresh DB read
    project = resolve_project(...)
    return full_payload_with_results
return {"state": state, "dag_run_id": dag_run_id}
```

---

### `code/app/services/airflow_client.py` ★

Pure stdlib (`urllib`) — no extra dependencies.

```python
def airflow_enabled() -> bool:
    return bool((settings.airflow_base_url or "").strip())

def trigger_drone_dag(conf: dict) -> str:
    # POST {AIRFLOW_BASE_URL}/api/v1/dags/drone_pipeline/dagRuns
    # Returns dag_run_id

def get_dag_run_state(dag_run_id: str) -> str:
    # GET {AIRFLOW_BASE_URL}/api/v1/dags/drone_pipeline/dagRuns/{dag_run_id}
    # Returns: "queued" | "running" | "success" | "failed"
```

Auth supports: Basic auth (username/password) or Bearer token.

---

### `code/app/core/settings.py` ★

All config via environment variables with `TCP_` prefix:

```python
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TCP_")

    airflow_base_url: str = ""           # TCP_AIRFLOW_BASE_URL
    airflow_username: str | None = None  # TCP_AIRFLOW_USERNAME
    airflow_password: str | None = None  # TCP_AIRFLOW_PASSWORD
    airflow_auth_token: str | None = None
    drone_dag_id: str = "drone_pipeline" # TCP_DRONE_DAG_ID
```

---

### `code/app/schemas/project.py` ★

`execution_id` field is the key to preventing circular Airflow loops:

```python
class AnalyzeTrigger(BaseModel):
    action: str | None = None
    project_id: str | None = None
    model_key: str | None = None
    source_epsg: int | None = None
    params: dict | None = None
    execution_id: str | None = None   # ★ Set by Airflow callback only
                                       #    When present → skip Airflow, run directly
```

---

### `frontend/index.html` ★

Key polling logic in `analyze()` function:

```javascript
// Step 1: Trigger
const triggered = await api("/api/v1/project/drone_api", {
    method: "POST",
    body: JSON.stringify({ project_id: PID, action: "analyze", params: {...} })
});

// Step 2: Poll if Airflow path
if (triggered.dag_run_id) {
    let c = null;
    while (!c) {
        await new Promise(r => setTimeout(r, 5000));  // wait 5s
        const s = await api(`/api/v1/project/drone_status/${dag_run_id}?action=analyze&project_id=${PID}`);
        $("astage").textContent = `DAG status: ${s.state} (poll ${pollCount})`;
        if (s.state === "success") { c = rememberProject(s); }
        else if (s.state === "failed") { throw new Error("DAG failed"); }
    }
    // Show clustering results from c.available_k, c.recommended_k
}
```

---

### `.env`

Not committed to git. Must be created manually on each machine:

```bash
# Required for Airflow integration
TCP_AIRFLOW_BASE_URL=http://corestac-stacd-airflow:8080
TCP_AIRFLOW_USERNAME=admin
TCP_AIRFLOW_PASSWORD=admin
TCP_DRONE_DAG_ID=drone_pipeline

# Storage
TCP_STORAGE_ROOT=/data/storage
TCP_DATABASE_URL=sqlite:////data/treecrown.db

# Models
HOST_MODELS_DIR=./models
TCP_MODELS_DIR=/models
TCP_DEFAULT_MODEL_KEY=urban_cambridge

# HuggingFace (prevent async client crash in sync context)
HF_HUB_OFFLINE=1

# Docker Hub images (for docker-compose.hub.yml)
IMAGE_API=uavforaliens/treecrown-workstation:latest
IMAGE_FRONTEND=anunay12/treecrown-frontend:latest
```

---

## 5. Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TCP_AIRFLOW_BASE_URL` | For Airflow | `""` | Airflow REST API URL. **Empty = local mode** |
| `TCP_AIRFLOW_USERNAME` | For Airflow | `None` | Airflow basic auth username |
| `TCP_AIRFLOW_PASSWORD` | For Airflow | `None` | Airflow basic auth password |
| `TCP_AIRFLOW_AUTH_TOKEN` | Optional | `None` | Bearer token (overrides basic auth) |
| `TCP_DRONE_DAG_ID` | No | `drone_pipeline` | Name of the Airflow DAG to trigger |
| `TCP_STORAGE_ROOT` | Yes | `/data/storage` | Where project files are stored |
| `TCP_DATABASE_URL` | Yes | SQLite in /data | SQLAlchemy DB URL |
| `TCP_MODELS_DIR` | Yes | `/models` | Path to detector weight files |
| `TCP_DEFAULT_MODEL_KEY` | No | `urban_cambridge` | Default detector model |
| `HF_HUB_OFFLINE` | Yes | — | Set to `1` to prevent HuggingFace network calls |
| `HOST_MODELS_DIR` | Yes | — | Host path to models folder (for Docker volume) |
| `IMAGE_API` | For hub | — | Docker Hub image for backend |
| `IMAGE_FRONTEND` | For hub | — | Docker Hub image for frontend |

---

## 6. Docker Setup

### Option A — Build from source (local dev)

```bash
cp .env.example .env   # edit as needed
docker compose up -d --build
```

### Option B — Pull from Docker Hub (production)

```bash
cp .env.example .env   # set IMAGE_API, IMAGE_FRONTEND, etc.
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d
```

### Port mapping

| Host port | Container | Service |
|-----------|-----------|---------|
| `8123` | `8000` | FastAPI backend (Airflow calls back here) |
| `8200` | `80` | nginx frontend |

### Volume mounts

| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `./code` | `/code` | Backend source — changes live without rebuild |
| `./data` | `/data` | SQLite DB + project storage |
| `./frontend` | `/usr/share/nginx/html` | Frontend HTML — changes live without rebuild |
| `$HOST_MODELS_DIR` | `/models` | Detector weights (read-only) |

---

## 7. Airflow Integration

### How it works

The backend integrates with Airflow via its REST API. The Airflow DAG (`drone_pipeline`) runs the compute and calls back to the backend to execute the ML pipeline.

### The `execution_id` trick — preventing circular loops

Without `execution_id`, a circular loop happens:
```
Frontend → drone_api → triggers Airflow
Airflow Drone_Algo → drone_api → triggers Airflow again  ← LOOP!
```

With `execution_id`:
```
Frontend → drone_api (no execution_id) → triggers Airflow
Airflow Drone_Algo → drone_api (WITH execution_id) → runs pipeline directly
```

The Airflow DAG sends `execution_id` in the callback body. When the backend sees `execution_id`, it skips Airflow and runs the ML pipeline directly.

### Airflow DAG requirements

The `drone_pipeline` DAG on the Airflow side must:

1. Accept `conf` with: `project_id`, `action`, `execution_type`, optional `params`, `model_key`, `source_epsg`, `run_name`
2. Call back to the backend at:
   ```
   POST http://{DRONE_API_BASE}/api/v1/project/drone_api
   Body: {
       "execution_id": "<uuid>",   ← REQUIRED to break circular loop
       "project_id": "<from conf>",
       "action": "<from conf>",
       ...other conf fields
   }
   ```
3. Set env var `DRONE_API_BASE` on the Airflow worker (e.g. `http://192.168.90.179:8123`)

### Legacy DAGs (drone_analyze / drone_finalize)

These older DAGs call separate endpoints:
- `drone_analyze_dag.py` → `POST /api/v1/compute/analyze`
- `drone_finalize_dag.py` → `POST /api/v1/compute/finalize`

These still work but are not used by the new unified `drone_api` flow.

---

## 8. API Reference & Call Graph

### Full call graph

```
USER CLICKS "RUN ANALYSIS"
│
▼
[Browser]
POST /api/v1/project/drone_api
{ project_id, action: "analyze", params: {...} }
│
▼
[Backend — drone_api endpoint]
Is TCP_AIRFLOW_BASE_URL set?
│
├─ NO → run_analyze() in-process → return clustering payload
│
└─ YES (and no execution_id)
    │
    ▼
    [Backend → Airflow]
    POST http://airflow:8080/api/v1/dags/drone_pipeline/dagRuns
    { conf: { project_id, action, execution_type, params, ... } }
    ← { dag_run_id: "manual__2026-06-22T..." }
    │
    ▼
    [Backend → Browser]
    { status: "started", dag_run_id: "manual__2026...", project_id, action }

═══════════ FRONTEND STARTS POLLING (every 5s) ═══════════

[Browser]
GET /api/v1/project/drone_status/{dag_run_id}?action=analyze&project_id={PID}
│
▼
[Backend → Airflow]
GET http://airflow:8080/api/v1/dags/drone_pipeline/dagRuns/{dag_run_id}
← { state: "running" }
│
▼
[Backend → Browser]
{ state: "running", dag_run_id: "..." }

... (repeats every 5s) ...

═══════════ MEANWHILE IN AIRFLOW ═══════════

[Airflow Drone_Algo task]
POST http://backend:8123/api/v1/project/drone_api
{
    execution_id: "<uuid4>",   ← KEY: tells backend to run directly
    project_id: "...",
    action: "analyze",
    params: {...}
}
│
▼
[Backend — drone_api sees execution_id → skips Airflow]
run_analyze() → ML pipeline runs here
→ updates project state to AWAITING_LABELS
→ saves available_k, recommended_k to DB
← 200 OK (returns clustering payload to Airflow)

[Airflow DAG state → "success"]

═══════════ NEXT FRONTEND POLL ═══════════

[Browser]
GET /api/v1/project/drone_status/{dag_run_id}?action=analyze&project_id={PID}
│
▼
[Backend → Airflow]
GET .../dagRuns/{dag_run_id}
← { state: "success" }
│
▼
[Backend]
db.expire_all()  ← force fresh read from DB
project = resolve_project(db, user, project_id)
payload = build_clustering_payload(project)  ← has available_k, recommended_k
payload["state"] = "success"
│
▼
[Browser]
{ state: "success", available_k: [2,4,6,8,10], recommended_k: 4, ... }
UI updates: "clustering ready · k available: 2,4,6,8,10 · recommended: 4"
```

### All API endpoints

#### New (Airflow integration)

| Method | Path | Called by | Returns |
|--------|------|-----------|---------|
| `POST` | `/api/v1/project/drone_api` | Frontend, Airflow | `{dag_run_id}` (Airflow path) or full payload (direct) |
| `GET` | `/api/v1/project/drone_status/{dag_run_id}` | Frontend | `{state}` or full payload on success |

#### Project management

| Method | Path | Called by | Returns |
|--------|------|-----------|---------|
| `POST` | `/api/v1/projects` | Frontend | `{project_id, state, ...}` |
| `GET` | `/api/v1/project` | Frontend | Current project state |
| `GET` | `/api/v1/projects/{id}` | Frontend/Postman | Project by ID |
| `POST` | `/api/v1/project/orthomosaic` | Frontend | Ortho upload confirmation |
| `POST` | `/api/v1/project/labels` | Frontend | Label submission result |
| `PATCH` | `/api/v1/project` | Frontend | Update params |
| `DELETE` | `/api/v1/project` | Frontend | — |

#### Clustering review

| Method | Path | Called by | Returns |
|--------|------|-----------|---------|
| `GET` | `/api/v1/project/clustering` | Frontend | Clustering overview + k metrics |
| `GET` | `/api/v1/project/clustering/k-selection.png` | Frontend | Elbow/silhouette plot |
| `GET` | `/api/v1/project/clustering/{k}/tsne.png` | Frontend | t-SNE plot for k |
| `GET` | `/api/v1/project/clustering/{k}/clusters` | Frontend | Cluster thumbnails |
| `GET` | `/api/v1/project/crowns/{image}` | Frontend | Crown image |

#### Results & downloads

| Method | Path | Called by | Returns |
|--------|------|-----------|---------|
| `GET` | `/api/v1/project/results` | Frontend | Download links |
| `GET` | `/api/v1/project/results/kmz` | Browser | KMZ file |
| `GET` | `/api/v1/project/results/crown-master.csv` | Browser | Crown CSV |
| `GET` | `/api/v1/project/results/polygon-species.csv` | Browser | Polygon CSV |

#### Airflow callbacks (legacy DAGs)

| Method | Path | Called by | Returns |
|--------|------|-----------|---------|
| `POST` | `/api/v1/compute/analyze` | `drone_analyze` DAG | Clustering payload |
| `POST` | `/api/v1/compute/finalize` | `drone_finalize` DAG | Results payload |

#### Meta

| Method | Path | Called by | Returns |
|--------|------|-----------|---------|
| `GET` | `/livez` | Docker healthcheck, frontend | `{status: "ok"}` |

---

## 9. Project State Machine

```
CREATED
   │
   │ POST /orthomosaic (upload GeoTIFF)
   ▼
UPLOADED
   │
   │ POST /drone_api {action: "analyze"}
   ▼
ANALYZING
   │
   │ (pipeline runs via Airflow or in-process)
   ▼
AWAITING_LABELS  ← available_k and recommended_k now set
   │
   │ POST /labels (submit cluster species CSV)
   ▼
LABELS_SUBMITTED
   │
   │ POST /drone_api {action: "finalize"}
   ▼
FINALIZING
   │
   │ (species assignment, KMZ export)
   ▼
COMPLETED  ← download links available
```

Error at any stage → `FAILED` (can retry analyze from FAILED state)

---

## 10. Running Locally (Without Airflow)

Leave `TCP_AIRFLOW_BASE_URL` empty in `.env`. Backend runs everything in-process.

```bash
# 1. Clone repo
git clone <repo-url>
cd drone_docker

# 2. Create .env
cat > .env << 'EOF'
TCP_STORAGE_ROOT=/data/storage
TCP_DATABASE_URL=sqlite:////data/treecrown.db
HOST_MODELS_DIR=./models
TCP_MODELS_DIR=/models
TCP_DEFAULT_MODEL_KEY=urban_cambridge
HF_HUB_OFFLINE=1
TCP_MAX_UPLOAD_MB=8192
# TCP_AIRFLOW_BASE_URL=   ← leave empty for local mode
EOF

# 3. Start
docker compose up -d --build

# 4. Open
# Frontend: http://localhost:8200
# API docs: http://localhost:8123/docs
```

---

## 11. Running With Airflow

```bash
# 1. Set Airflow env vars in .env
cat >> .env << 'EOF'
TCP_AIRFLOW_BASE_URL=http://corestac-stacd-airflow:8080
TCP_AIRFLOW_USERNAME=admin
TCP_AIRFLOW_PASSWORD=admin
TCP_DRONE_DAG_ID=drone_pipeline
EOF

# 2. Start with hub compose (production)
docker compose -f docker-compose.hub.yml pull
docker compose -f docker-compose.hub.yml up -d

# 3. Verify Airflow connection from inside container
docker exec drone_docker-api-1 env | grep TCP_AIRFLOW

# 4. Check backend can reach Airflow
docker exec drone_docker-api-1 curl -s \
  -u admin:admin \
  http://corestac-stacd-airflow:8080/api/v1/dags \
  | python3 -m json.tool | head -20

# 5. Verify new code is inside container
docker exec drone_docker-api-1 grep -n "dag_run_id" /code/app/api/v1/analyze.py
```

### On the Airflow side

The `drone_pipeline` DAG worker must have:
```bash
DRONE_API_BASE=http://<backend-host>:8123
# Optional auth:
DRONE_SERVICE_TOKEN=<same as TCP_COMPUTE_TOKEN if set>
```

And the DAG's callback request **must include `execution_id`** to prevent the circular loop:
```json
{
  "execution_id": "some-uuid",
  "project_id": "...",
  "action": "analyze"
}
```

---

## 12. Troubleshooting

### Still hitting direct path instead of Airflow

```bash
# Check if env var is set inside container
docker exec drone_docker-api-1 env | grep TCP_AIRFLOW_BASE_URL

# Should print:
# TCP_AIRFLOW_BASE_URL=http://corestac-stacd-airflow:8080
# If empty → check .env file, restart container
docker compose -f docker-compose.hub.yml restart api
```

### Two DAG runs being triggered (circular loop)

The Airflow DAG callback is not sending `execution_id`. The backend re-triggers Airflow on the callback.

**Fix**: Ensure the Airflow `drone_pipeline` DAG sends `execution_id` in its POST body to `/project/drone_api`.

### "Failed to fetch" in frontend (old issue — now resolved)

Was caused by long-running HTTP connection timing out. Fixed by:
- `drone_api` returns `dag_run_id` immediately
- Frontend polls `/drone_status/{dag_run_id}` every 5s

### `available_k` / `recommended_k` undefined after success

Caused by stale SQLAlchemy session cache. Fixed by `db.expire_all()` in `drone_status` before querying project.

### HuggingFace error: "client has been closed"

Set in `.env`:
```
HF_HUB_OFFLINE=1
```
Model is already cached in `/data/hf-cache`. This prevents the async httpx client from being called in a sync context.

### Check container logs

```bash
# Backend logs (last 50 lines)
docker logs drone_docker-api-1 --tail 50

# Follow live
docker logs drone_docker-api-1 -f

# Frontend nginx logs
docker logs drone_docker-frontend-1 --tail 20
```

### Verify code changes inside container

```bash
# Check analyze.py has drone_status endpoint
docker exec drone_docker-api-1 grep -n "drone_status\|dag_run_id" /code/app/api/v1/analyze.py

# Check airflow_client.py has get_dag_run_state
docker exec drone_docker-api-1 grep -n "def get_dag_run_state\|def trigger_drone" /code/app/services/airflow_client.py

# Check execution_id in schema
docker exec drone_docker-api-1 grep -n "execution_id" /code/app/schemas/project.py
```
