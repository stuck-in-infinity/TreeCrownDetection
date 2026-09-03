"""Google Drive ortho download, isolated in its own process.

Why this file exists
--------------------
``gdown.download()`` is a single blocking call. Once it is running there is no
cooperative point at which the caller can say "stop" — no chunk callback, no
cancel token. So a Drive fetch that stalls behind a slow or wedged connection
holds a uvicorn worker, and the project's ``UPLOADING`` claim, for as long as
Drive keeps the socket open.

The only way to actually interrupt it is to put it in a separate process and
kill that process. This module is the child's entry point, kept deliberately
tiny: the parent starts it with the **spawn** context, which re-imports this
module in a fresh interpreter. A fork would be cheaper but is unsafe here —
uvicorn is multi-threaded, and forking a threaded process can inherit locks
held by threads that do not exist in the child, which deadlocks. Spawn costs
about a second of interpreter start-up, which is nothing against a transfer
measured in minutes.

Nothing here touches the database or the app's settings. The child's whole
contract is: download into ``tmp_dir``, or write why it could not.
"""
import os

#: The child writes its failure reason here, inside ``tmp_dir``. The parent
#: reads it after the child exits and turns it into the HTTP error, so a
#: failure message survives without pickling an exception across processes.
ERROR_FILE = "_download_error.txt"


def download_to(file_id: str, tmp_dir: str) -> int:
    """Fetch one Drive file into ``tmp_dir``. Runs as the child's main.

    Returns a process exit code: 0 on success, 1 on a handled failure (with the
    reason written to ``ERROR_FILE``). The parent never reads the return value
    directly — it inspects ``tmp_dir`` — but a non-zero code keeps the child's
    exitcode meaningful in logs.
    """
    try:
        import gdown
    except ImportError:
        _write_error(tmp_dir, "Server is missing the 'gdown' dependency.")
        return 1

    try:
        out = gdown.download(id=file_id, output=tmp_dir + os.sep, quiet=True)
    except Exception as exc:                       # noqa: BLE001 - reported, not raised
        _write_error(tmp_dir, f"Google Drive download failed: {exc}")
        return 1

    if not out or not os.path.exists(out):
        _write_error(tmp_dir, "Download failed - the file may be private, deleted, "
                              "or over its Google Drive download quota.")
        return 1
    return 0


def _write_error(tmp_dir: str, message: str) -> None:
    """Best effort: if this fails the parent just reports a generic failure."""
    try:
        with open(os.path.join(tmp_dir, ERROR_FILE), "w", encoding="utf-8") as fh:
            fh.write(message)
    except OSError:
        pass


def read_error(tmp_dir: str) -> str | None:
    """Parent side: the child's reason, or None if it did not record one."""
    path = os.path.join(tmp_dir, ERROR_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def downloaded_file(tmp_dir: str) -> str | None:
    """Parent side: the single file the child produced, ignoring its own error
    note. Returns None if the child produced nothing (killed, or failed)."""
    try:
        names = [n for n in os.listdir(tmp_dir) if n != ERROR_FILE]
    except OSError:
        return None
    files = [os.path.join(tmp_dir, n) for n in names]
    files = [f for f in files if os.path.isfile(f)]
    return files[0] if len(files) == 1 else None
