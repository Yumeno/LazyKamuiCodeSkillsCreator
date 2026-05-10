# -*- coding: utf-8 -*-
"""Queue client for submitting and querying jobs via the worker HTTP API.

Provides three modes:
  - submit_job: Submit a job and return immediately (--submit-only)
  - wait_job: Query a job's current status (--wait)
  - blocking_job: Submit → poll until done (--blocking, default)
"""
import json
import subprocess
import sys
import time

import requests

from job_queue import DEFAULT_MAX_POLLS, __version__ as CLIENT_VERSION
from job_queue.versioning import (
    get_worker_version,
    warn_if_version_mismatch,
)


def _check_worker_version(response) -> None:
    """Inspect a worker response for the X-Worker-Version header and warn
    on mismatch. (PR2 / #62.)

    The actual warning fires at most once per process via the one-shot
    guard inside ``versioning.warn_if_version_mismatch``.
    """
    if response is None:
        return
    headers = getattr(response, "headers", None)
    worker_v = get_worker_version(headers)
    warn_if_version_mismatch(worker_v, CLIENT_VERSION)


def is_worker_running(worker_url: str) -> bool:
    """Check if the worker is reachable.

    Also pipes the response through ``_check_worker_version`` so the
    health-probe path that runs at every CLI invocation gets the same
    stale-worker warning treatment as job-submission paths. Without
    this, the most common entry point (``_ensure_worker_running``)
    would silently miss the version skew until the user submitted a
    job.
    """
    try:
        resp = requests.get(f"{worker_url}/api/health", timeout=2)
        _check_worker_version(resp)
        return resp.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


def start_worker(
    worker_script: str,
    config_path: str | None = None,
    timeout: float = 10.0,
    port: int = 54321,
) -> bool:
    """Start the worker daemon as a background process.

    Returns True if the worker became reachable within timeout.
    """
    cmd = [sys.executable, worker_script]
    if config_path:
        cmd.extend(["--config", config_path])

    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
        )
    else:
        kwargs["start_new_session"] = True

    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        **kwargs,
    )

    worker_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_worker_running(worker_url):
            return True
        time.sleep(0.2)
    return False


def submit_job(
    worker_url: str,
    endpoint: str,
    submit_tool: str,
    args: dict,
    status_tool: str | None = None,
    result_tool: str | None = None,
    headers: dict | None = None,
    rate_limits: dict | None = None,
) -> dict:
    """Submit a job to the worker. Returns {"job_id": ..., "status": "pending"}."""
    payload = {
        "endpoint": endpoint,
        "submit_tool": submit_tool,
        "args": args,
    }
    if status_tool:
        payload["status_tool"] = status_tool
    if result_tool:
        payload["result_tool"] = result_tool
    if headers:
        payload["headers"] = headers
    if rate_limits:
        payload["rate_limits"] = rate_limits

    resp = requests.post(f"{worker_url}/api/jobs", json=payload, timeout=10)
    _check_worker_version(resp)
    resp.raise_for_status()
    return resp.json()


def wait_job(worker_url: str, job_id: str) -> dict:
    """Query a job's current status once. Returns the job state."""
    resp = requests.get(f"{worker_url}/api/jobs/{job_id}", timeout=10)
    _check_worker_version(resp)
    return resp.json()


def blocking_job(
    worker_url: str,
    endpoint: str,
    submit_tool: str,
    args: dict,
    status_tool: str | None = None,
    result_tool: str | None = None,
    headers: dict | None = None,
    rate_limits: dict | None = None,
    poll_interval: float = 2.0,
    max_polls: int = DEFAULT_MAX_POLLS,
) -> dict:
    """Submit a job, poll until complete, and return the final result.

    Raises TimeoutError if max_polls is exceeded.
    """
    submit_result = submit_job(
        worker_url=worker_url,
        endpoint=endpoint,
        submit_tool=submit_tool,
        args=args,
        status_tool=status_tool,
        result_tool=result_tool,
        headers=headers,
        rate_limits=rate_limits,
    )
    job_id = submit_result["job_id"]

    for i in range(max_polls):
        result = wait_job(worker_url, job_id)
        status = result.get("status", "unknown")

        if status in ("completed", "done", "success", "finished"):
            return result
        if status in ("failed", "error", "cancelled"):
            return result

        time.sleep(poll_interval)

    raise TimeoutError(
        f"Job {job_id} did not complete within {max_polls} polls"
    )
