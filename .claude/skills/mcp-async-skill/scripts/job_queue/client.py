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


def is_worker_running(worker_url: str) -> bool:
    """Check if the worker is reachable."""
    try:
        resp = requests.get(f"{worker_url}/api/health", timeout=2)
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
    resp.raise_for_status()
    return resp.json()


def wait_job(worker_url: str, job_id: str) -> dict:
    """Query a job's current status once. Returns the job state."""
    resp = requests.get(f"{worker_url}/api/jobs/{job_id}", timeout=10)
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
    max_polls: int = 300,
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
