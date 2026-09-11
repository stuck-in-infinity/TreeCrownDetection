"""FileBrowser REST client.

Creates a permanent public share for a project folder, so users can browse and
download every pipeline output from one link without logging in.

The share is created once when the project is created, using the project_id as
the path inside /srv. The hash that comes back is stored on the Project row and
returned as ``files_url`` in the analyze and finalize responses.

With TCP_FILEBROWSER_BASE_URL unset, this module does nothing.
"""
import json
import urllib.error
import urllib.request

from app.core.logging import ERROR_CODES, classify_conn_error, get_logger
from app.core.settings import settings

log = get_logger("app.filebrowser")


def filebrowser_enabled() -> bool:
    return bool((settings.filebrowser_base_url or "").strip())


def _get_token() -> str:
    """Log in and return the JWT token."""
    base = settings.filebrowser_base_url.rstrip("/")
    body = json.dumps({
        "username": settings.filebrowser_username or "admin",
        "password": settings.filebrowser_password or "",
    }).encode()
    req = urllib.request.Request(
        f"{base}/api/login",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.read().decode().strip()


def create_project_share(project_id: str) -> str:
    """Create a permanent public share for the project folder.

    Returns the share hash, for example 'fNqIKDS3'. Raises RuntimeError on any
    failure, leaving it to the caller to decide whether to report or ignore it.
    """
    base = settings.filebrowser_base_url.rstrip("/")
    try:
        token = _get_token()
    except urllib.error.HTTPError as e:
        log.error("FileBrowser login failed at %s: HTTP %s", base, e.code, exc_info=True)
        err = RuntimeError(f"FileBrowser login failed (HTTP {e.code})")
        err.code = ERROR_CODES["FILEBROWSER_AUTH"]
        raise err from e
    except urllib.error.URLError as e:
        reason = classify_conn_error(e, timeout=10)
        log.error("FileBrowser unreachable at %s: %s", base, reason, exc_info=True)
        err = RuntimeError(f"FileBrowser unreachable at {base}: {reason}")
        err.code = ERROR_CODES["FILEBROWSER_UNREACHABLE"]
        raise err from e
    except Exception as e:
        log.error("FileBrowser login failed at %s", base, exc_info=True)
        err = RuntimeError(f"FileBrowser login failed: {e}")
        err.code = ERROR_CODES["FILEBROWSER_AUTH"]
        raise err from e

    body = json.dumps({}).encode()
    req = urllib.request.Request(
        f"{base}/api/share/{project_id}",
        data=body,
        headers={"Content-Type": "application/json", "X-Auth": token},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        return data["hash"]
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        log.error("FileBrowser share API returned %s at %s", e.code, base, exc_info=True)
        err = RuntimeError(f"FileBrowser share API returned {e.code}: {detail}")
        err.code = ERROR_CODES["FILEBROWSER_SHARE_FAILED"]
        raise err from e
    except urllib.error.URLError as e:
        reason = classify_conn_error(e, timeout=10)
        log.error("FileBrowser unreachable at %s: %s", base, reason, exc_info=True)
        err = RuntimeError(f"FileBrowser unreachable at {base}: {reason}")
        err.code = ERROR_CODES["FILEBROWSER_UNREACHABLE"]
        raise err from e
    except Exception as e:
        log.error("FileBrowser share failed at %s", base, exc_info=True)
        err = RuntimeError(f"FileBrowser share failed: {e}")
        err.code = ERROR_CODES["FILEBROWSER_SHARE_FAILED"]
        raise err from e


def share_url(share_hash: str) -> str:
    """Build the share URL shown to the user from a stored hash.

    Uses filebrowser_public_url, the address the browser can reach, when it is
    set, and filebrowser_base_url otherwise.
    """
    base = (settings.filebrowser_public_url or settings.filebrowser_base_url).rstrip("/")
    return f"{base}/share/{share_hash}"


def run_share_url(share_hash: str, run: int) -> str:
    """Deep link to one run's folder inside the project's share.

    FileBrowser creates one permanent share per project (see
    ``create_project_share``), and every run's outputs live under
    ``work/run_<n>/`` inside it. A share URL takes a subpath, so a per-run link
    needs no extra share, no extra API call, and no new failure mode when
    FileBrowser is down — it is built from the hash already on the project row.

    Confirm once against your FileBrowser before relying on it: open a run
    link and check it lands inside that folder rather than at the share root.
    If your build rejects the subpath, the fallback is a share per run folder,
    stored as a hash on the run row — one more round-trip per run.
    """
    return f"{share_url(share_hash)}/work/run_{int(run)}"
