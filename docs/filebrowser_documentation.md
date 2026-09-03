# FileBrowser — End to End Documentation

## Table of Contents
1. [What is FileBrowser?](#what-is-filebrowser)
2. [Why FileBrowser over WebDAV?](#why-filebrowser-over-webdav)
3. [Architecture Overview](#architecture-overview)
4. [Setup and Installation](#setup-and-installation)
5. [Docker Compose File — Line by Line](#docker-compose-file--line-by-line)
6. [User Management](#user-management)
7. [Permissions](#permissions)
8. [Public Sharing](#public-sharing)
9. [Persistence — Why the Named Volume Matters](#persistence--why-the-named-volume-matters)
10. [Running with a Single Docker Command](#running-with-a-single-docker-command)
11. [End to End Flow](#end-to-end-flow)
12. [Troubleshooting](#troubleshooting)

---

## What is FileBrowser?

FileBrowser is a **lightweight, self-hosted web UI** for browsing and managing files on a server. It runs inside a Docker container and exposes a browser-based interface where users can:

- Browse files and folders
- Download files
- Upload files (if permitted)
- Edit, rename, delete files (if permitted)
- Share files or folders via public links

FileBrowser does **not** require any client-side installation. Users just open a URL in their browser.

---

## Why FileBrowser over WebDAV?

Both FileBrowser and WebDAV were evaluated for exposing the shared output folder to users.

| Feature | FileBrowser | WebDAV |
|---|---|---|
| Web UI | ✅ Yes | ❌ No |
| User management | ✅ Built-in UI | ⚠️ Manual htpasswd file |
| Read-only users | ✅ Per-user permissions | ⚠️ Complex nginx config |
| Read-write users | ✅ Yes | ✅ Yes |
| Per-user folder scope | ✅ Yes | ❌ No |
| Public share links | ✅ Yes | ❌ No |
| No client software needed | ✅ Just a browser | ❌ Needs drive mount or curl |

**Conclusion:** FileBrowser is better suited for human users who need to browse and download outputs. WebDAV is better for programmatic/protocol access.

---

## Architecture Overview

```
STACD DAGs (orchestrator)
        │
        │ triggers
        ▼
Backend Docker (FastAPI)
        │ generates GeoJSON + PNG
        │ writes to /data/shared/
        ▼
./data/shared/ (shared volume on host)
        │
        │ same folder mounted
        ▼
FileBrowser Docker
        │ exposes ./data via browser UI
        ▼
Users access via http://server-ip:8097
  ├── readonlyuser  → browse + download only
  └── readwriteuser → full access
```

The `./data` folder on the host is the **backbone** — the backend writes to it, and FileBrowser reads from it. They never communicate directly; the shared folder is the only connection.

---

## Setup and Installation

### Step 1 — Create the shared data folder

Always create the folder manually before starting containers to avoid permission issues (Docker auto-creates folders as root-owned which can cause write failures).

```bash
mkdir -p ./data
```

### Step 2 — Create the docker-compose.yml

```yaml
services:
  

  filebrowser:
    image: filebrowser/filebrowser
    ports:
      - "8097:80"
    volumes:
      - ./data:/srv
      - filebrowser_db:/database
    restart: unless-stopped

volumes:
  filebrowser_db:
```

### Step 3 — Start the containers

```bash
docker compose up -d
```

### Step 4 — Get the admin password

On first start, FileBrowser generates a random admin password:

```bash
docker logs experiment-filebrowser-1
```

Look for:
```
User 'admin' initialized with randomly generated password: <password>
```

### Step 5 — Login

Open `http://localhost:8097` in your browser and login with:
- **Username:** `admin`
- **Password:** `<randomly generated password from logs>`

---

## Docker Compose File — Line by Line

```yaml
services:
```
Defines all containers to run. Each entry is one container.

```yaml
  filebrowser:
    image: filebrowser/filebrowser
```
Pull the pre-built FileBrowser image from Docker Hub. No custom build needed.

```yaml
    ports:
      - "8097:80"
```
Map host port `8097` to container port `80`. Format is always `host:container`. FileBrowser runs on port 80 inside the container. Access it from outside on port 8097.

```yaml
    volumes:
      - ./data:/srv
```
Mount the `./data` folder from the host to `/srv` inside the container. FileBrowser serves files from `/srv` by default. This is the same folder the backend writes to — making outputs visible in FileBrowser automatically.

```yaml
      - filebrowser_db:/database
```
Mount a named Docker volume called `filebrowser_db` to `/database` inside the container. FileBrowser stores its user database (`filebrowser.db`) here. This is NOT a local folder — it is Docker-managed and persists across container restarts and recreations.

```yaml
    restart: unless-stopped
```
Automatically restart the container if it crashes. Does not restart if manually stopped with `docker compose down`.

```yaml
volumes:
  filebrowser_db:
```
Declares the named volume `filebrowser_db`. Required in docker-compose — without this declaration, Docker throws an error. The `:` with nothing after it means use default settings. Docker stores this volume at `/var/lib/docker/volumes/experiment_filebrowser_db/_data` on the host.

---

## User Management

FileBrowser has a built-in user management system. Users are managed via the **Admin UI**.

### Adding a user via UI

1. Login as `admin`
2. Click the hamburger menu (☰) → **Settings** → **User Management**
3. Click **"New User"**
4. Fill in username, password, scope, and permissions
5. Click **Save**

### User fields explained

| Field | Description |
|---|---|
| **Username** | Login username |
| **Password** | Login password |
| **Scope** | Which folder the user can see. `.` means root (`./data`). Use `./drone` to restrict to a subfolder |
| **Language** | UI language |
| **Prevent password change** | If checked, user cannot change their own password |

### Adding users via CLI (when container is not running)

```bash
# Stop the running container first
docker compose down

# Run a temporary container to add user
docker run --rm \
  -v filebrowser_db:/database \
  filebrowser/filebrowser \
  /bin/filebrowser users add username password \
  --perm.admin=false \
  --perm.create=false \
  --perm.delete=false \
  --database /database/filebrowser.db

# Start containers again
docker compose up -d
```

> **Note:** You cannot run CLI commands against the database while FileBrowser is running — it holds a lock on the database. Always stop the container first or use the Web UI instead.

### Listing users via CLI

```bash
docker exec experiment-filebrowser-1 /bin/filebrowser users ls --database /database/filebrowser.db
```

---

## Permissions

FileBrowser supports granular per-user permissions.

| Permission | What it allows |
|---|---|
| **Administrator** | Full access + manage other users. All other permissions auto-checked. |
| **Create files and directories** | Upload files, create new folders |
| **Delete files and directories** | Delete files and folders |
| **Download** | Download files to local machine |
| **Edit files** | Edit file contents directly in the browser |
| **Rename or move files and directories** | Rename files, move them between folders |
| **Share files** | Generate public shareable links (requires Download permission) |

### For the professor's use case

**Read-only user** — can only browse and download:
- ✅ Download
- ❌ Create, Delete, Edit, Rename, Share

**Read-write user** — full access except admin:
- ✅ Create, Delete, Download, Edit, Rename
- ❌ Administrator

### Users created in this setup

| Username | Password | Access Level |
|---|---|---|
| `admin` | randomly generated | Full admin |
| `readonlyuser` | `readonlyuser` | Browse + download only |
| `readwriteuser` | `readwriteuser` | Full access except admin |

---

## Public Sharing

FileBrowser supports generating **public shareable links** for files or folders — no login required.

### How to share a folder

1. Login as `admin` or `readwriteuser`
2. Right-click on a file or folder
3. Click **Share**
4. FileBrowser generates a public link:
   ```
   http://localhost:8097/share/xxxxxxxx
   ```
5. Anyone with this link can browse and download — no login needed

### Share link properties

- ✅ Read-only (cannot edit/delete via share link)
- ✅ Works for entire folders — not just individual files
- ✅ Can set an expiry time
- ✅ Can be revoked anytime by the admin

### Organizing outputs for sharing

You can organize the shared folder by job type and share each subfolder separately:

```
./data/
├── drone/           ← share with drone team
├── bioacoustics/    ← share with bioacoustics team
└── shared/          ← public share for all outputs
```

Each team gets their own public share link pointing to their subfolder.

---

## Persistence — Why the Named Volume Matters

### The problem without a volume

If no volume is mounted, the user database lives **inside the container**:

```bash
docker compose down   # container DESTROYED
docker compose up -d  # brand new container
                      # ❌ all users LOST
```

### The solution — named Docker volume

With `filebrowser_db:/database`, the database lives **outside the container** in a Docker-managed volume:

```bash
docker compose down   # container DESTROYED
docker compose up -d  # new container, volume reattached
                      # ✅ all users PRESERVED
```

### Persistence across different scenarios

| Scenario | Without volume | With named volume |
|---|---|---|
| `docker restart` | ✅ users kept | ✅ users kept |
| `docker compose down` + `up` | ❌ users lost | ✅ users kept |
| Container crashes and restarts | ✅ users kept | ✅ users kept |
| Server reboot | ✅ users kept | ✅ users kept |
| Move to new machine | ❌ users lost | ✅ kept (if volume copied) |

### Where the volume is stored on the host

```bash
docker volume inspect experiment_filebrowser_db
```

Output:
```json
{
  "Name": "experiment_filebrowser_db",
  "Mountpoint": "/var/lib/docker/volumes/experiment_filebrowser_db/_data"
}
```

The actual database file is at:
```
/var/lib/docker/volumes/experiment_filebrowser_db/_data/filebrowser.db
```

> **Note on WSL/Windows:** The volume is stored inside the WSL virtual disk, not directly accessible from Windows File Explorer. Use `sudo ls /var/lib/docker/volumes/experiment_filebrowser_db/_data` to inspect it.

### Deleting the volume (destructive — loses all users)

```bash
docker volume rm experiment_filebrowser_db
```

---

## Running with a Single Docker Command

The entire FileBrowser setup can also be run with a single `docker run` command (without docker-compose):

```bash
# Create data folder first
mkdir -p ./data/shared

# Run FileBrowser
docker run -d \
  --name filebrowser \
  -p 8097:80 \
  -v ./data:/srv \
  -v filebrowser_db:/database \
  --restart unless-stopped \
  filebrowser/filebrowser
```

### Mapping to docker-compose

| docker-compose | docker run |
|---|---|
| `image: filebrowser/filebrowser` | `filebrowser/filebrowser` (at end) |
| `ports: - "8097:80"` | `-p 8097:80` |
| `volumes: - ./data:/srv` | `-v ./data:/srv` |
| `volumes: - filebrowser_db:/database` | `-v filebrowser_db:/database` |
| `restart: unless-stopped` | `--restart unless-stopped` |
| service name `filebrowser` | `--name filebrowser` |
| `volumes: filebrowser_db:` (declaration) | Auto-created by Docker if not exists |

> **Key difference:** In `docker run`, named volumes are auto-created if they don't exist. In `docker-compose`, they must be explicitly declared under `volumes:` at the bottom of the file.

---

## End to End Flow

### 1. STACD DAG triggers the backend API

```bash
# Simulated by curl (in production, triggered by Airflow)
curl -X POST http://localhost:8096/compute \
  -H "Content-Type: application/json" \
  -d '{"job_name": "drone_survey", "num_points": 5}'
```

Response:
```json
{
  "status": "success",
  "geojson_file": "drone_survey_xxxxxxxx.geojson",
  "image_file": "drone_survey_xxxxxxxx.png",
  "num_points": 5
}
```

### 2. Backend writes outputs to shared folder

```
./data/shared/
├── drone_survey_xxxxxxxx.geojson
└── drone_survey_xxxxxxxx.png
```

### 3. User browses outputs via FileBrowser

- Open `http://localhost:8097`
- Login as `readonlyuser` / `readonlyuser`
- Navigate to `shared/` folder
- See and download the output files

### 4. Public access via share link

- Admin generates a share link for the `shared/` folder
- Share the link with external users
- No login required — just open the link in a browser

---

## Troubleshooting

### Cannot login with admin/admin

FileBrowser generates a **random** password on first start, not `admin`.

```bash
docker logs experiment-filebrowser-1 | grep password
```

### Users lost after restart

The named volume was not declared or mounted correctly. Check:

```bash
docker volume ls | grep filebrowser
```

If missing, the volume was deleted. Recreate containers and re-add users via the UI.

### FileBrowser shows empty directory

The `./data` folder is not mounted correctly. Check:

```bash
docker exec experiment-filebrowser-1 ls /srv
```

If empty, verify the volume mount in docker-compose and that `./data` exists on the host.

### Cannot add users via CLI (timeout error)

FileBrowser holds a lock on the database while running. Use the **Web UI** to add users instead, or stop the container first before running CLI commands.

### Port already in use

```
Error: bind: address already in use
```

Another process is using port 8097. Either stop that process or change the host port in docker-compose:

```yaml
ports:
  - "8099:80"   # use a different host port
```
