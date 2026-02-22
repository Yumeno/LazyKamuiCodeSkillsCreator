#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP Worker Daemon - Entry point for the queue worker process.

Starts a local HTTP server that accepts job submissions and dispatches
them to external MCP servers with per-endpoint rate limiting.

Usage:
    python mcp_worker_daemon.py [--config queue_config.json] [--port 54321]
"""
import argparse
import json
import os
import sys
import time

# Force UTF-8 encoding for stdout/stderr (Windows compatibility)
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

from job_queue.dispatcher import QueueConfig
from job_queue.worker import WorkerApp


def _is_retryable(exc, connection_error_cls=None, http_error_cls=None):
    """Return True if the exception is a retryable transient error.

    Retryable: ConnectionError, HTTP 429/503/504.
    The cls parameters allow testing without importing requests.
    """
    if connection_error_cls is None:
        import requests
        connection_error_cls = requests.ConnectionError
    if http_error_cls is None:
        import requests
        http_error_cls = requests.HTTPError

    if isinstance(exc, connection_error_cls):
        return True
    if isinstance(exc, http_error_cls) and getattr(exc, "response", None) is not None:
        return exc.response.status_code in (429, 503, 504)
    return False


def _get_retry_after(exc, http_error_cls=None):
    """Extract Retry-After header from a 429 response (capped at 60s).

    Returns float seconds or None.
    """
    if http_error_cls is None:
        import requests
        http_error_cls = requests.HTTPError

    if not isinstance(exc, http_error_cls):
        return None
    resp = getattr(exc, "response", None)
    if resp is None:
        return None
    if resp.status_code != 429:
        return None
    val = resp.headers.get("Retry-After")
    if val:
        try:
            return min(float(val), 60.0)
        except ValueError:
            pass
    return None


def _with_retry(
    fn,
    max_retries=3,
    backoff_base=None,
    on_retry=None,
    connection_error_cls=None,
    http_error_cls=None,
):
    """Call fn() with exponential backoff retry on transient errors.

    Args:
        fn: Zero-arg callable to invoke.
        max_retries: Maximum number of retries (total attempts = max_retries + 1).
        backoff_base: List of wait times per attempt [2.0, 4.0, 8.0].
        on_retry: Optional callback(attempt, exc, wait_seconds) called before each retry.
        connection_error_cls: Exception class for connection errors (for testing).
        http_error_cls: Exception class for HTTP errors (for testing).

    Returns:
        The return value of fn().

    Raises:
        The last exception if all retries are exhausted or the error is not retryable.
    """
    if backoff_base is None:
        backoff_base = [2.0, 4.0, 8.0]

    for attempt in range(max_retries + 1):
        try:
            return fn()
        except Exception as e:
            if not _is_retryable(e, connection_error_cls, http_error_cls):
                raise
            if attempt >= max_retries:
                raise
            wait = _get_retry_after(e, http_error_cls)
            if wait is None:
                wait = backoff_base[min(attempt, len(backoff_base) - 1)]
            if on_retry is not None:
                on_retry(attempt, e, wait)
            time.sleep(wait)


def _download_results(result_resp, job_id, results_dir):
    """Download files from result URLs and save result.json.

    Args:
        result_resp: The raw result dict from MCP server.
        job_id: Job ID for organizing output directory.
        results_dir: Base directory for results. If None, skip downloads.

    Returns:
        Dict with keys: remote_result, local_files, download_errors.
    """
    if results_dir is None:
        return {"remote_result": result_resp, "local_files": []}

    from mcp_async_call import extract_download_urls

    job_dir = os.path.join(results_dir, job_id)
    os.makedirs(job_dir, exist_ok=True)

    info = {
        "remote_result": result_resp,
        "local_files": [],
        "download_errors": [],
    }

    # Save result.json
    result_path = os.path.join(job_dir, "result.json")
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=2)

    # Download files
    urls = extract_download_urls(result_resp)
    for url in urls:
        try:
            saved_path = download_file(url, output_dir=job_dir)
            info["local_files"].append(saved_path)
        except Exception as e:
            info["download_errors"].append(str(e))

    # Re-save result.json with local file paths
    if urls:
        with open(result_path, "w", encoding="utf-8") as f:
            json.dump(info, f, ensure_ascii=False, indent=2)

    return info


def create_rollback_fn():
    """Create a function that transitions zombie jobs on startup.

    Returns:
        Callable(store) that marks running jobs as failed and
        polling jobs (with remote_job_id) as recovering.
    """
    def rollback_stale_jobs(store):
        stale = store.get_stale_jobs(["running", "polling"])
        for job in stale:
            if job["status"] == "running" or not job.get("remote_job_id"):
                store.update_status(
                    job["id"], "failed",
                    error="Worker restarted before submit completed",
                )
            else:
                store.update_status(job["id"], "recovering")

    return rollback_stale_jobs


def create_mcp_job_executor(results_dir=None):
    """Create the real job executor that uses MCPAsyncClient.

    Args:
        results_dir: Directory to save downloaded results. If None, skip downloads.
    """
    def _poll_and_get_result(store, job_id, client, request_id,
                             status_tool, result_tool):
        """Poll status and retrieve results (shared by new and recovery jobs)."""
        import json as _json

        # Poll status
        if status_tool:
            completed_statuses = {"completed", "done", "success", "finished", "ready"}
            failed_statuses = {"failed", "error", "cancelled", "timeout"}
            max_polls = 300
            poll_interval = 2.0

            for _ in range(max_polls):
                try:
                    status_resp = client.check_status(status_tool, request_id)
                    status, status_result = parse_status_response(status_resp)
                except Exception:
                    # Individual poll errors are non-fatal; retry on next cycle
                    time.sleep(poll_interval)
                    continue

                if status in completed_statuses:
                    break
                if status in failed_statuses:
                    store.update_status(
                        job_id, "failed",
                        error=_json.dumps(status_result, ensure_ascii=False),
                    )
                    return

                time.sleep(poll_interval)
            else:
                store.update_status(job_id, "failed", error="Polling timeout")
                return

        # Get result
        if result_tool:
            result_resp = _with_retry(
                lambda: client.get_result(result_tool, request_id)
            )
            info = _download_results(result_resp, job_id, results_dir)
            store.update_status(
                job_id,
                "completed",
                result=_json.dumps(info, ensure_ascii=False),
            )
        else:
            store.update_status(job_id, "completed", result="{}")

    def execute_job(job: dict):
        """Execute a single job against an external MCP server."""
        import json as _json

        store = execute_job._store  # injected after creation

        endpoint = job["endpoint"]
        submit_tool = job["submit_tool"]
        args = _json.loads(job["args"]) if isinstance(job["args"], str) else job["args"]
        status_tool = job.get("status_tool")
        result_tool = job.get("result_tool")
        headers = None
        if job.get("headers"):
            headers = _json.loads(job["headers"]) if isinstance(job["headers"], str) else job["headers"]

        # Recovery path: job already submitted, skip to poll+result
        if job.get("remote_job_id"):
            client = MCPAsyncClient(endpoint, headers)
            client.session_id = job["session_id"]
            _poll_and_get_result(
                store, job["id"], client, job["remote_job_id"],
                status_tool, result_tool,
            )
            return

        # Normal path: submit → poll → result
        client = MCPAsyncClient(endpoint, headers)
        request_id = _with_retry(
            lambda: client.submit(submit_tool, args)
        )
        store.update_status(
            job["id"],
            "polling",
            session_id=client.session_id,
            remote_job_id=request_id,
        )

        _poll_and_get_result(
            store, job["id"], client, request_id,
            status_tool, result_tool,
        )

    return execute_job


# Module-level imports for mocking in tests
try:
    from mcp_async_call import download_file, MCPAsyncClient, parse_status_response
except ImportError:
    def download_file(*args, **kwargs):
        raise RuntimeError("mcp_async_call not available")

    class MCPAsyncClient:  # type: ignore
        pass

    def parse_status_response(*args, **kwargs):
        raise RuntimeError("mcp_async_call not available")


def main():
    parser = argparse.ArgumentParser(description="MCP Worker Daemon")
    parser.add_argument(
        "--config", "-c",
        help="Path to queue_config.json",
    )
    parser.add_argument("--port", "-p", type=int, help="Override port number")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind (default: 127.0.0.1)")
    args = parser.parse_args()

    # Load config
    config_dict = {}
    if args.config and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            config_dict = json.load(f)

    config = QueueConfig.from_dict(config_dict)
    if args.port:
        config.port = args.port
    if args.host:
        config.host = args.host

    # Determine DB path (same directory as config, or current dir)
    if args.config:
        db_dir = os.path.dirname(os.path.abspath(args.config))
    else:
        db_dir = os.getcwd()
    db_path = os.path.join(db_dir, "jobs.db")

    # Create executor
    executor = create_mcp_job_executor()

    # Create and start worker
    app = WorkerApp(
        host=config.host,
        port=config.port,
        db_path=db_path,
        config_dict=config_dict,
        job_executor=executor,
        idle_timeout=config.idle_timeout,
    )

    # Inject store reference into executor
    executor._store = app.store

    print(f"[WORKER] Starting on {config.host}:{config.port}")
    print(f"[WORKER] DB: {db_path}")
    print(f"[WORKER] Idle timeout: {config.idle_timeout}s")

    try:
        app.start()
        # Keep main thread alive
        while app._running:
            time.sleep(1)
    except OSError as e:
        if "Address already in use" in str(e) or "Only one usage" in str(e):
            print(f"[WORKER] Port {config.port} already in use. Another worker is running.")
            sys.exit(0)
        raise
    except KeyboardInterrupt:
        print("\n[WORKER] Shutting down...")
        app.stop()


if __name__ == "__main__":
    main()
