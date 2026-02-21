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


def create_mcp_job_executor():
    """Create the real job executor that uses MCPAsyncClient."""
    from mcp_async_call import MCPAsyncClient, parse_status_response

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

        client = MCPAsyncClient(endpoint, headers)

        # Submit
        request_id = client.submit(submit_tool, args)
        store.update_status(
            job["id"],
            "polling",
            session_id=client.session_id,
            remote_job_id=request_id,
        )

        # Poll status
        if status_tool:
            completed = {"completed", "done", "success", "finished", "ready"}
            failed = {"failed", "error", "cancelled", "timeout"}
            max_polls = 300
            poll_interval = 2.0

            for _ in range(max_polls):
                status_resp = client.check_status(status_tool, request_id)
                status, status_result = parse_status_response(status_resp)

                if status in completed:
                    break
                if status in failed:
                    store.update_status(
                        job["id"], "failed",
                        error=_json.dumps(status_result, ensure_ascii=False),
                    )
                    return

                time.sleep(poll_interval)
            else:
                store.update_status(
                    job["id"], "failed", error="Polling timeout"
                )
                return

        # Get result
        if result_tool:
            result_resp = client.get_result(result_tool, request_id)
            store.update_status(
                job["id"],
                "completed",
                result=_json.dumps(result_resp, ensure_ascii=False),
            )
        else:
            store.update_status(job["id"], "completed", result="{}")

    return execute_job


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
