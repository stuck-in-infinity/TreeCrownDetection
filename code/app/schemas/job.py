"""Intentionally empty.

The API no longer exposes job status. The /api/v1/jobs/{job_id} polling endpoint
and its JobOut schema went away when analyze and finalize became synchronous
calls. Job rows are still written for audit purposes (see app/db/models.py), but
nothing serialises them over HTTP.
"""
