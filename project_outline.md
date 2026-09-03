# Tree-Crown Pipeline — Implementation Progress

---

## 1. System Components

Three Docker containers work together:

**Frontend (port 8200)**
A single HTML page. Buttons for upload, analyze, label, finalize. Talks only to the backend — never directly to Airflow.

**Backend (FastAPI — port 8123)**
The brain. Receives requests from frontend, manages the database, talks to Airflow, and runs the ML pipeline when Airflow calls back. All business logic lives here.

**Airflow (external — port 8080)**
A job scheduler. Does zero ML computation. Its only job: receive a trigger from our backend, call our backend to run the pipeline, and track success or failure.

```mermaid
graph TD
    A[👤 User Browser<br/>port 8200] -->|HTTP API calls| B[⚙️ Backend FastAPI<br/>port 8123]
    B -->|Trigger DAG| C[🌀 Airflow<br/>port 8080]
    C -->|Callback to run pipeline| B
    B -->|Reads & Writes| D[(💾 /data volume<br/>SQLite DB + outputs)]
    A -->|Static files served by| E[🌐 Frontend<br/>port 8200]
```

---

## 2. How the Three Parts Connect

**Frontend talks to Backend. Backend talks to Airflow. Airflow talks back to Backend.** The frontend never knows Airflow exists.

```mermaid
flowchart LR
    F([Frontend]) -- "1. User actions\n API calls" --> B([Backend])
    B -- "2. Trigger DAG" --> AF([Airflow])
    AF -- "3. Callback\n with execution_id" --> B
    B -- "4. Results\n back to user" --> F
    B -- "5. Poll DAG status" --> AF
    F -- "6. Poll every 5s" --> B
```

---

## 3. Full Pipeline

Create Project → Upload Orthomosic → Analyze → Build Cluster Table → Submit Label  Finalize and Export


**Analyze:** Detectree2 detects tree crowns → DINOv2 extracts visual features per crown → KMeans clusters similar crowns → user reviews cluster images and picks species labels.

**Finalize:** User submits labels → species assigned to every crown → exports KMZ (Google Earth), GeoJSON, CSV, STAC catalog entry.

---

## 4. Airflow Integration — How It Works

The backend connects to Airflow using environment variables in `.env`:

```
TCP_AIRFLOW_BASE_URL  →  URL of Airflow (internal Docker hostname)
TCP_AIRFLOW_USERNAME  →  admin
TCP_AIRFLOW_PASSWORD  →  admin
TCP_DRONE_DAG_ID      →  drone_pipeline
```

When `TCP_AIRFLOW_BASE_URL` is empty, the system runs the pipeline directly in-process — used for local development without needing Airflow at all.

```mermaid
sequenceDiagram
    participant F as Frontend
    participant B as Backend
    participant AF as Airflow

    F->>B: POST /project/drone_api<br/>{action: "analyze", project_id, params}
    B->>AF: POST /api/v1/dags/drone_pipeline/dagRuns<br/>{conf: {project_id, action, params}}
    AF-->>B: {dag_run_id: "manual__2026..."}
    B-->>F: {dag_run_id: "manual__2026...", status: "started"}
    Note over F,B: Returns in under 1 second — no waiting
```

---

## 5. The Callback Architecture

**Airflow is the manager. Our backend is the worker.**

- **Step 1** — Backend calls Airflow to START the job
- **Step 2** — Airflow calls our backend to DO the work (run the ML pipeline)
- **Step 3** — Backend returns result; Airflow marks DAG success or failed based on HTTP response code

Airflow has no idea what DINOv2 or KMeans is. It simply makes a POST to our backend and waits up to 2 hours for a response.

```mermaid
sequenceDiagram
    participant B as Backend
    participant AF as Airflow

    Note over B,AF: Step 1 — Backend triggers Airflow
    B->>AF: POST /dagRuns {project_id, action, params}
    AF-->>B: {dag_run_id}

    Note over B,AF: Step 2 — Airflow calls Backend to do the work
    AF->>B: POST /project/drone_api<br/>{execution_id: "uuid", project_id, action}
    Note over B: execution_id present → skip re-triggering Airflow<br/>Run ML pipeline directly
    B-->>AF: 200 OK {clustering results}

    Note over B,AF: Step 3 — DAG marked success
    AF->>AF: state = "success"
```

---

## 6. Polling — How the UI Stays Alive

**The problem:** ML pipeline takes 10–30 minutes. Browser drops HTTP connections after ~60–120 seconds. Holding one connection open for the full duration causes "Failed to fetch."

**The solution:** Break it into many short requests.

- **Trigger:** Frontend calls backend once → returns `dag_run_id` in under 1 second
- **Poll:** Frontend calls backend every 5 seconds → backend checks Airflow once → returns current state
- **Done:** When Airflow reports success, backend fetches fresh results from DB and returns full payload

Every individual request completes in under 1 second. No connection is held open. Browser never times out.

```mermaid
sequenceDiagram
    participant F as Frontend
    participant B as Backend
    participant AF as Airflow

    F->>B: POST /drone_api
    B->>AF: Trigger DAG
    AF-->>B: dag_run_id
    B-->>F: {dag_run_id} ← instant return

    loop Every 5 seconds
        F->>B: GET /drone_status/{dag_run_id}
        B->>AF: GET /dagRuns/{dag_run_id}
        AF-->>B: {state: "running"}
        B-->>F: {state: "running"}
        Note over F: UI: "DAG status: running (poll N)"
    end

    Note over AF: Pipeline finishes → DAG = success

    F->>B: GET /drone_status/{dag_run_id}
    AF-->>B: {state: "success"}
    B->>B: db.expire_all() → fetch fresh project
    B-->>F: {state: "success", available_k, recommended_k, ...}
    Note over F: UI: "clustering ready · recommended: 4"
```

---

## 7. API Calls on Clicking Analyze

```mermaid
sequenceDiagram
    participant U as User
    participant F as Frontend
    participant B as Backend
    participant AF as Airflow

    U->>F: Clicks "Run Analysis"
    F->>B: POST /api/v1/projects
    B-->>F: {project_id}

    F->>B: POST /api/v1/project/orthomosaic
    Note over F,B: Upload GeoTIFF drone image
    B-->>F: {state: UPLOADED}

    F->>B: POST /api/v1/project/drone_api<br/>{action:"analyze", project_id, params}
    B->>AF: POST /dags/drone_pipeline/dagRuns<br/>{conf: {project_id, action, params}}
    AF-->>B: {dag_run_id}
    B-->>F: {dag_run_id, status:"started"} ← instant, no waiting

    loop Frontend polls every 5 seconds
        F->>B: GET /api/v1/project/drone_status/{dag_run_id}?action=analyze
        B->>AF: GET /dagRuns/{dag_run_id}
        AF-->>B: {state: "running"}
        B-->>F: {state: "running"}
        Note over F: UI text updates: "DAG status: running (poll N)"
    end

    Note over AF: Pipeline finishes
    AF->>B: POST /project/drone_api<br/>{execution_id:"uuid", project_id, action:"analyze"}
    Note over B: execution_id present → run ML pipeline directly<br/>Detectree2 → DINOv2 → KMeans
    B-->>AF: 200 OK {clustering results}
    Note over AF: Marks DAG state = "success"

    F->>B: GET /drone_status/{dag_run_id}?action=analyze
    B->>AF: GET /dagRuns/{dag_run_id}
    AF-->>B: {state: "success"}
    B->>B: db.expire_all() → fetch fresh project from DB
    B-->>F: {state:"success", available_k:[2,4,6,8,10], recommended_k:4}
    F->>U: UI shows cluster images and recommended k
```

---

## 8. API Calls on Clicking Finalize

```mermaid
sequenceDiagram
    participant U as User
    participant F as Frontend
    participant B as Backend
    participant AF as Airflow

    U->>F: Reviews clusters, submits species labels
    F->>B: POST /api/v1/project/labels<br/>{species_csv: "Oak,Pine,...", chosen_k: 4}
    B-->>F: {state: LABELS_SUBMITTED}

    U->>F: Clicks "Finalize"
    F->>B: POST /api/v1/project/drone_api<br/>{action:"finalize", project_id}
    B->>AF: POST /dags/drone_pipeline/dagRuns<br/>{conf: {project_id, action:"finalize"}}
    AF-->>B: {dag_run_id}
    B-->>F: {dag_run_id, status:"started"} ← instant, no waiting

    loop Frontend polls every 5 seconds
        F->>B: GET /api/v1/project/drone_status/{dag_run_id}?action=finalize
        B->>AF: GET /dagRuns/{dag_run_id}
        AF-->>B: {state: "running"}
        B-->>F: {state: "running"}
        Note over F: UI text updates: "DAG status: running (poll N)"
    end

    Note over AF: Pipeline finishes
    AF->>B: POST /project/drone_api<br/>{execution_id:"uuid", project_id, action:"finalize"}
    Note over B: Species assignment → KMZ → GeoJSON → CSV → STAC item
    B-->>AF: 200 OK
    Note over AF: Marks DAG state = "success"

    F->>B: GET /drone_status/{dag_run_id}?action=finalize
    AF-->>B: {state: "success"}
    B-->>F: {state:"success", downloads:{kmz, geojson, crown_csv, polygon_csv}}
    F->>U: Shows download links for all output files
```

---

## 9. Bugs & Challenges

### Bug 1 — Circular Airflow Loop (two DAG runs triggered)

**What happened:** Every click of Analyze triggered two DAG runs instead of one.

**Why:** Airflow calls our backend's `drone_api` to run the pipeline. The backend saw that call, noticed Airflow was configured, and triggered Airflow again — infinite loop.

**Fix:** Added `execution_id` field. Airflow always sends it in its callback; the frontend never sends it. Backend checks — if `execution_id` is present, skip Airflow and run directly. One field, zero loop.

---

### Bug 2 — "Failed to fetch" on Frontend

**What happened:** Frontend showed "Failed to fetch" even though the pipeline was running correctly.

**Why:** Backend held one HTTP connection open for 10–30 minutes while polling Airflow internally. Browser TCP idle timeout (~60s) dropped the connection.

**Fix:** Changed architecture — backend returns `dag_run_id` immediately (under 1 second). Frontend polls a status endpoint every 5 seconds. Each poll completes in under 1 second. No connection is held long enough to time out.

---

### Bug 3 — `available_k` and `recommended_k` showing undefined

**What happened:** After pipeline succeeded, UI showed "clustering ready · recommended: undefined".

**Why:** SQLAlchemy caches DB objects in the session. The status endpoint read the project from the old cached version — before the pipeline had written the results.

**Fix:** Added `db.expire_all()` before reading the project on success. Forces SQLAlchemy to discard the cache and fetch fresh data from disk.

---

### Bug 4 — HuggingFace Async Client Crash

**What happened:** Pipeline crashed with "Cannot send a request, as the client has been closed" during DINOv2 feature extraction.

**Why:** HuggingFace Hub uses an async HTTP client to check for model updates. When called from a synchronous FastAPI endpoint, the async client was already closed.

**Fix:** Set `HF_HUB_OFFLINE=1` in environment variables. Model is already cached locally — no network check needed. Completely bypasses the async client.

---

## 10. Future Work

### FileBrowser Integration — Auto-Share Project Outputs

After finalize, users get individual download links for each file. The next step is to integrate [FileBrowser](docs/filebrowser_integration.md) — an open-source file manager running as a Docker container — so users get one browsable link to all their outputs.

**The key idea:** When a project is created (`POST /projects`), the backend immediately calls FileBrowser's API to create a permanent public share for that project's folder. FileBrowser returns a unique `hash` tied to that `project_id`. This hash is stored in the DB alongside the project. After finalize completes, the frontend constructs the share URL from this pre-generated hash and shows it to the user — no login required to browse or download files.

```mermaid
sequenceDiagram
    participant F as Frontend
    participant B as Backend
    participant FB as FileBrowser

    F->>B: POST /api/v1/projects
    B->>B: Generate project_id (UUID)
    B->>FB: POST /api/share/{project_id}
    FB-->>B: {"hash": "fNqIKDS3"}
    Note over B: Store hash in DB with project
    B-->>F: {project_id, share_hash:"fNqIKDS3"}

    Note over F,FB: ...pipeline runs, finalize completes...

    B-->>F: {state:"success", files_url:"http://filebrowser/share/fNqIKDS3"}
    Note over F: "Browse all output files →" link shown to user
```

Each `project_id` gets its own unique hash — so every user gets an isolated, permanent link to only their project's files. No project can access another's outputs.

See [docs/filebrowser_integration.md](docs/filebrowser_integration.md) for the full technical plan and API calls.

---

### Frontend UX Improvements

The current frontend is a functional single-page app, but minimal. Planned improvements:
- Better progress display during long polling (step-by-step status messages)
- Visual cluster image gallery for reviewing KMeans results
- One-click download all outputs button via FileBrowser share link
- Mobile-friendly layout

---

### Centralized Logging

Currently each Docker container writes logs independently, viewable only via `docker logs`. Planned work:
- All containers (API, frontend, Airflow, pipeline workers) write to a shared external volume at `/var/logs/drone`
- Structured JSON log format so logs can be queried by project ID, user, timestamp
- Makes debugging across containers significantly easier — one place to look

---

### Google Single Sign-On

Currently the frontend has no authentication — anyone who knows the URL can use the system. Hridayansh has built Google SSO for the bioacoustics app. The plan is to reuse the same SSO mechanism:

- User signs in with Google before triggering any pipeline
- SSO returns: Google account, unique event ID, timestamp, auth key
- Every API call must include these parameters in headers
- Backend logs them so every pipeline run is traceable to a specific user and session

---

### Data Sharing Policy

Per professor's direction: all pipeline outputs (GeoJSON, CSVs, STAC items) should have a standardized sharing policy. When a user submits a compute request, they are asked:
- Are you okay making outputs publicly visible?
- What license applies to your data?
- Brief description of the survey

This information flows to a centralized data sharing service (likely CKAN-based) which sets external visibility on STAC items and handles data housekeeping (e.g. raw data cleanup after one month). This is a system to be designed across all drone, bioacoustics, and other apps — not just ours.
