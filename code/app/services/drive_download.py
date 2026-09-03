"""Google Drive orthomosaic download, run in its own process.

``gdown.download()`` is one blocking call with no way to interrupt it: there is
no chunk callback and no cancel token. A Drive fetch that stalls on a slow or
stuck connection therefore holds a uvicorn worker, and the project's
``UPLOADING`` claim, for as long as Drive keeps the socket open.

The only way to stop it is to run it in a separate process and kill that
process. This module is that child process's entry point, and is kept small on
purpose: the parent starts it with the spawn context, which re-imports this
module in a new interpreter. Fork would be cheaper but is not safe here, because
uvicorn runs threads and a forked child can inherit a lock held by a thread that
does not exist on its side, which deadlocks. Spawn costs about a second of
start-up, which is nothing next to a transfer measured in minutes.

Nothing here reads the database or the app settings. The child only has to
download into ``tmp_dir``, or record why it could not.
"""
import os

# The child writes its failure reason to this file inside ``tmp_dir``. The
# parent reads it once the child exits and turns it into the HTTP error, which
# passes the message across processes without pickling an exception.
ERROR_FILE = "_download_error.txt"


def download_to(file_id: str, tmp_dir: str) -> int:
    """Fetch one Drive file into ``tmp_dir``. This is the child's main function.

    Returns a process exit code: 0 on success, 1 on a failure whose reason was
    written to ``ERROR_FILE``. The parent works out what happened by looking at
    ``tmp_dir`` rather than reading this value, but a non-zero code still makes
    the child's exit meaningful in the logs.
    """
    try:
        import gdown
    except ImportError:
        _write_error(tmp_dir, "Server is missing the 'gdown' dependency.")
        return 1

    try:
        out = gdown.download(id=file_id, output=tmp_dir + os.sep, quiet=True)
    except Exception as exc:                       # noqa: BLE001 - recorded, not raised
        _write_error(tmp_dir, f"Google Drive download failed: {exc}")
        return 1

    if not out or not os.path.exists(out):
        _write_error(tmp_dir, "Download failed - the file may be private, deleted, "
                              "or over its Google Drive download quota.")
        return 1
    return 0


def _write_error(tmp_dir: str, message: str) -> None:
    """Best effort. If this fails, the parent reports a generic failure."""
    try:
        with open(os.path.join(tmp_dir, ERROR_FILE), "w", encoding="utf-8") as fh:
            fh.write(message)
    except OSError:
        pass


def read_error(tmp_dir: str) -> str | None:
    """Called by the parent: the child's reason, or None if it recorded none."""
    path = os.path.join(tmp_dir, ERROR_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def downloaded_file(tmp_dir: str) -> str | None:
    """Called by the parent: the one file the child downloaded, ignoring its
    error note. Returns None if the child produced nothing, because it failed or
    was killed."""
    try:
        names = [n for n in os.listdir(tmp_dir) if n != ERROR_FILE]
    except OSError:
        return None
    files = [os.path.join(tmp_dir, n) for n in names]
    files = [f for f in files if os.path.isfile(f)]
    return files[0] if len(files) == 1 else None
