# FileBrowser — Public Share Link API

## Overview

FileBrowser exposes REST API to create public share links programmatically via curl or any HTTP client. No UI needed.

Public share link = anyone with URL can browse/download folder — no login required.

---

## Prerequisites

- FileBrowser running at `http://<server-ip>:8097`
- Admin or user with `share` permission

---

## Step 1 — Get JWT Token (Login)

```bash
curl -X POST http://localhost:8097/api/login \
  -H "Content-Type: application/json" \
  -d '{"username": "admin", "password": "your_password"}'
```

Response — raw JWT token string:
```
eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
```

Save this token — needed for all subsequent API calls. Token expires after some time; re-login to get fresh one.

---

## Step 2 — Create Share Link for a Folder

```bash
curl -X POST http://localhost:8097/api/share/<folder_name> \
  -H "X-Auth: <jwt_token>" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Example — share folder named `shared_2`:

```bash
curl -X POST http://localhost:8097/api/share/shared_2 \
  -H "X-Auth: eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..." \
  -H "Content-Type: application/json" \
  -d '{}'
```

Response:
```json
{
  "hash": "MI6CcH7J",
  "path": "/shared_2",
  "userID": 1,
  "expire": 0
}
```

Public URL:
```
http://localhost:8097/share/MI6CcH7J
```

---

## Step 3 — Create Share with Expiry (optional)

`expire` is a Unix timestamp. Set it to limit access duration.

```bash
# Share expires after 24 hours
EXPIRE=$(date -d "+24 hours" +%s)

curl -X POST http://localhost:8097/api/share/shared_2 \
  -H "X-Auth: <jwt_token>" \
  -H "Content-Type: application/json" \
  -d "{\"expire\": $EXPIRE}"
```

`expire: 0` = never expires.

---

## Step 4 — List All Existing Shares

```bash
curl http://localhost:8097/api/shares \
  -H "X-Auth: <jwt_token>"
```

Response:
```json
[
  {
    "hash": "MI6CcH7J",
    "path": "/shared_2",
    "userID": 1,
    "expire": 0
  }
]
```

---

## Step 5 — Delete a Share

```bash
curl -X DELETE http://localhost:8097/api/share/MI6CcH7J \
  -H "X-Auth: <jwt_token>"
```

---

## Full Script — Login + Share in One Go

```bash
#!/bin/bash

FB_URL="http://localhost:8097"
USERNAME="admin"
PASSWORD="your_password"
FOLDER="shared_2"

# Step 1 — Login and get token
TOKEN=$(curl -s -X POST $FB_URL/api/login \
  -H "Content-Type: application/json" \
  -d "{\"username\": \"$USERNAME\", \"password\": \"$PASSWORD\"}")

echo "Token: $TOKEN"

# Step 2 — Create share
RESPONSE=$(curl -s -X POST $FB_URL/api/share/$FOLDER \
  -H "X-Auth: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}')

echo "Response: $RESPONSE"

# Step 3 — Extract hash and print public URL
HASH=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['hash'])")
echo "Public URL: $FB_URL/share/$HASH"
```

---

## API Reference

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/api/login` | Login, get JWT token |
| `POST` | `/api/share/<path>` | Create share link for file/folder |
| `GET` | `/api/shares` | List all shares |
| `DELETE` | `/api/share/<hash>` | Delete a share |

---

## Key Notes

- `X-Auth` header carries JWT token — not `Authorization: Bearer`
- Body `{}` required even if no params — empty body causes `400 Bad Request`
- `expire: 0` = never expires
- Share links are always **read-only** — no edit/delete via share URL
- Works for both files and folders
- Hash is unique per share — same folder can have multiple share links

---

## Integration with STACD Pipeline

After a DAG run completes and outputs land in the shared folder, the share link can be auto-generated and included in the DAG completion notification:

```python
import requests

# Login
token = requests.post(
    "http://localhost:8097/api/login",
    json={"username": "admin", "password": "your_password"}
).text.strip('"')

# Create share
response = requests.post(
    f"http://localhost:8097/api/share/shared_2",
    headers={"X-Auth": token, "Content-Type": "application/json"},
    json={}
).json()

share_url = f"http://localhost:8097/share/{response['hash']}"
print(f"Outputs available at: {share_url}")
```
