# -*- coding: utf-8 -*-
"""Worker HTTP daemon for the job queue system.

Provides a local REST API (http.server) that accepts job submissions,
returns job status, and runs a background dispatcher for execution.
"""
import json
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable

from . import db
from .dispatcher import Dispatcher, QueueConfig


class _RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the worker API."""

    def log_message(self, format, *args):
        # Suppress default logging to stderr
        pass

    def _send_json(self, status_code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length)

    def do_GET(self):
        app: WorkerApp = self.server.app  # type: ignore

        # Touch last access time
        app.touch()

        if self.path == "/api/health":
            self._send_json(200, {
                "status": "ok",
                "active_jobs": app.store.count_all_active_jobs(),
                "pending_jobs": app.store.count_all_pending_jobs(),
            })
            return

        if self.path == "/api/jobs" or self.path.startswith("/api/jobs?"):
            # Parse query parameters
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            params = parse_qs(parsed.query)
            status_filter = params.get("status", [None])[0]
            endpoint_filter = params.get("endpoint", [None])[0]
            limit_str = params.get("limit", [None])[0]
            limit = int(limit_str) if limit_str else None

            jobs = app.store.get_all_jobs(
                status=status_filter,
                endpoint=endpoint_filter,
                limit=limit,
            )
            self._send_json(200, {
                "total": len(jobs),
                "jobs": [
                    {
                        "job_id": j["id"],
                        "endpoint": j["endpoint"],
                        "submit_tool": j["submit_tool"],
                        "status": j["status"],
                        "created_at": j["created_at"],
                        "updated_at": j["updated_at"],
                        "error": j["error"],
                    }
                    for j in jobs
                ],
            })
            return

        if self.path == "/api/stats":
            stats = app.store.get_stats_by_endpoint()
            self._send_json(200, {"endpoints": stats})
            return

        if self.path.startswith("/api/jobs/"):
            job_id = self.path[len("/api/jobs/"):]
            job = app.store.get_job(job_id)
            if job is None:
                self._send_json(404, {"error": "Job not found"})
                return
            self._send_json(200, {
                "job_id": job["id"],
                "status": job["status"],
                "result": job["result"],
                "error": job["error"],
                "remote_job_id": job["remote_job_id"],
            })
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        app: WorkerApp = self.server.app  # type: ignore

        # Touch last access time
        app.touch()

        if self.path == "/api/jobs":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json(400, {"error": "Invalid JSON"})
                return

            endpoint = body.get("endpoint")
            submit_tool = body.get("submit_tool")
            args = body.get("args")

            if not endpoint or not submit_tool or args is None:
                self._send_json(400, {
                    "error": "Missing required fields: endpoint, submit_tool, args"
                })
                return

            # args can be dict or string; store as JSON string
            args_str = json.dumps(args) if isinstance(args, dict) else str(args)

            job_id = app.store.insert_job(
                endpoint=endpoint,
                submit_tool=submit_tool,
                args=args_str,
                status_tool=body.get("status_tool"),
                result_tool=body.get("result_tool"),
                headers=json.dumps(body["headers"]) if body.get("headers") else None,
            )

            self._send_json(200, {"job_id": job_id, "status": "pending"})
            return

        self._send_json(404, {"error": "Not found"})


class WorkerApp:
    """Manages the HTTP server, dispatcher, and idle timeout."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 54321,
        db_path: str = "jobs.db",
        config_dict: dict | None = None,
        job_executor: Callable[[dict], None] | None = None,
        idle_timeout: int = 60,
    ):
        self.host = host
        self.port = port
        self.store = db.JobStore(db_path)

        config = QueueConfig.from_dict(config_dict or {})
        config.host = host
        config.port = port
        config.idle_timeout = idle_timeout

        self.idle_timeout = idle_timeout
        self._last_access = time.monotonic()

        # Default executor is a no-op (useful for testing the HTTP layer)
        executor = job_executor or (lambda job: None)

        self.dispatcher = Dispatcher(
            store=self.store,
            config=config,
            job_executor=executor,
            loop_interval=0.1,
        )

        self._server: HTTPServer | None = None
        self._server_thread: threading.Thread | None = None
        self._idle_thread: threading.Thread | None = None
        self._running = False

    def touch(self):
        """Update last access time."""
        self._last_access = time.monotonic()

    def start(self):
        """Start HTTP server, dispatcher, and idle monitor."""
        self._running = True

        self._server = HTTPServer((self.host, self.port), _RequestHandler)
        self._server.app = self  # type: ignore

        self._server_thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._server_thread.start()

        self.dispatcher.start()

        if self.idle_timeout > 0:
            self._idle_thread = threading.Thread(target=self._idle_monitor, daemon=True)
            self._idle_thread.start()

    def stop(self):
        """Shutdown everything."""
        self._running = False
        if self._server:
            self._server.shutdown()
        self.dispatcher.stop()
        self.store.close()

    def _idle_monitor(self):
        """Check idle timeout and shutdown if exceeded."""
        while self._running:
            time.sleep(0.5)
            elapsed = time.monotonic() - self._last_access
            active = self.store.count_all_active_jobs()
            pending = self.store.count_all_pending_jobs()
            if active == 0 and pending == 0 and elapsed > self.idle_timeout:
                self.stop()
                return
