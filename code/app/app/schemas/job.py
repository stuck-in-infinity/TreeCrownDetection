"""Removed.

The synchronous API does not expose job status — the previous
/api/v1/jobs/{job_id} polling endpoint and its JobOut schema were dropped when
the service moved to fully synchronous analyze + finalize calls. Job rows are
still created internally for audit (see app/db/models.py), but they are not
serialised through the HTTP layer.
"""
