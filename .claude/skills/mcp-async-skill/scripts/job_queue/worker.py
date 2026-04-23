# -*- coding: utf-8 -*-
"""Worker HTTP daemon for the job queue system.

Provides a local REST API (http.server) that accepts job submissions,
returns job status, and runs a background dispatcher for execution.
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Callable

logger = logging.getLogger(__name__)

from . import db
from .dispatcher import Dispatcher, QueueConfig


def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 with Z suffix."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_db_timestamp(ts: str | None) -> datetime | None:
    """Parse a timestamp stored in the DB as a timezone-aware datetime (UTC).

    Handles both the new Z-suffixed format and the legacy un-suffixed format
    (which was also UTC but without timezone marker).
    """
    if not ts:
        return None
    try:
        # Normalize: strip trailing Z and microsecond fraction length variations
        s = ts.rstrip("Z")
        # datetime.fromisoformat accepts "2026-04-11T01:23:45.678901" form
        try:
            parsed = datetime.fromisoformat(s)
        except ValueError:
            # Fallback: try stripping the fractional part
            parsed = datetime.strptime(
                re.sub(r"\.\d+$", "", s), "%Y-%m-%dT%H:%M:%S"
            )
        # Legacy records have no tzinfo — treat as UTC
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _age_seconds(ts: str | None) -> float | None:
    """Return seconds elapsed from *ts* (UTC) to now."""
    dt = _parse_db_timestamp(ts)
    if dt is None:
        return None
    return (datetime.now(timezone.utc) - dt).total_seconds()


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
            include_args = params.get("include_args", [None])[0] in ("true", "1")

            jobs = app.store.get_all_jobs(
                status=status_filter,
                endpoint=endpoint_filter,
                limit=limit,
            )
            job_list = []
            for j in jobs:
                created_age = _age_seconds(j["created_at"])
                updated_age = _age_seconds(j["updated_at"])
                entry = {
                    "job_id": j["id"],
                    "endpoint": j["endpoint"],
                    "submit_tool": j["submit_tool"],
                    "status": j["status"],
                    "created_at": j["created_at"],
                    "updated_at": j["updated_at"],
                    "created_age_seconds": round(created_age, 1) if created_age is not None else None,
                    "updated_age_seconds": round(updated_age, 1) if updated_age is not None else None,
                    "error": j["error"],
                }
                if include_args:
                    try:
                        entry["args"] = json.loads(j["args"])
                    except (json.JSONDecodeError, TypeError):
                        entry["args"] = j["args"]
                job_list.append(entry)
            self._send_json(200, {
                "server_time_utc": _utc_now_iso(),
                "total": len(jobs),
                "jobs": job_list,
            })
            return

        if self.path == "/api/stats":
            stats = app.store.get_stats_by_endpoint()
            cat_status = app.dispatcher.category_limiter.get_all_status()
            ep_pauses = app.dispatcher.get_all_endpoint_pauses()
            self._send_json(200, {
                "server_time_utc": _utc_now_iso(),
                "endpoints": stats,
                "category_limits": cat_status,
                "endpoint_pauses": ep_pauses,
            })
            return

        if self.path == "/api/categories":
            cat_status = app.dispatcher.category_limiter.get_all_status()
            self._send_json(200, {
                "server_time_utc": _utc_now_iso(),
                "categories": cat_status,
            })
            return

        if self.path == "/api/config":
            cat_cfg = app.dispatcher.category_limiter.get_config()
            self._send_json(200, {
                "category": cat_cfg,
                "endpoint": {
                    "default_max_concurrent": app.dispatcher.config.default_max_concurrent,
                    "default_min_interval": app.dispatcher.config.default_min_interval,
                },
                "worker": {
                    "idle_timeout": app.idle_timeout,
                    "port": app.port,
                    "host": app.host,
                },
            })
            return

        if self.path == "/api/worker/status":
            self._send_json(200, {
                "pid": os.getpid(),
                "running": app._running,
                "active_jobs": app.store.count_all_active_jobs(),
                "pending_jobs": app.store.count_all_pending_jobs(),
            })
            return

        if self.path.startswith("/api/jobs/"):
            from urllib.parse import urlparse as _urlparse, parse_qs as _parse_qs
            _parsed = _urlparse(self.path)
            job_id = _parsed.path[len("/api/jobs/"):]
            _params = _parse_qs(_parsed.query)
            _include_args = _params.get("include_args", [None])[0] in ("true", "1")

            job = app.store.get_job(job_id)
            if job is None:
                self._send_json(404, {
                    "server_time_utc": _utc_now_iso(),
                    "error": "Job not found",
                })
                return
            created_age = _age_seconds(job["created_at"])
            updated_age = _age_seconds(job["updated_at"])
            resp_data = {
                "server_time_utc": _utc_now_iso(),
                "job_id": job["id"],
                "status": job["status"],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
                "created_age_seconds": round(created_age, 1) if created_age is not None else None,
                "updated_age_seconds": round(updated_age, 1) if updated_age is not None else None,
                "result": job["result"],
                "error": job["error"],
                "remote_job_id": job["remote_job_id"],
            }
            if _include_args:
                try:
                    resp_data["args"] = json.loads(job["args"])
                except (json.JSONDecodeError, TypeError):
                    resp_data["args"] = job["args"]
            self._send_json(200, resp_data)
            return

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        app: WorkerApp = self.server.app  # type: ignore

        # Touch last access time
        app.touch()

        # Endpoint resume
        if self.path == "/api/endpoints/resume":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json(400, {"error": "Invalid JSON"})
                return
            endpoint = body.get("endpoint", "")
            if not endpoint:
                self._send_json(400, {"error": "Missing 'endpoint' field"})
                return
            app.dispatcher.resume_endpoint(endpoint)
            self._send_json(200, {
                "endpoint": endpoint,
                "paused": False,
            })
            return

        # Category pause/resume
        if self.path.startswith("/api/categories/"):
            parts = self.path.rstrip("/").split("/")
            # /api/categories/{category}/{action}
            if len(parts) == 5:
                category = parts[3]
                action = parts[4]
                limiter = app.dispatcher.category_limiter
                if action == "pause":
                    limiter.pause_category(category)
                    self._send_json(200, {
                        "category": category,
                        "paused": True,
                        "status": limiter.get_all_status().get(category, {}),
                    })
                    return
                if action == "resume":
                    limiter.resume_category(category)
                    self._send_json(200, {
                        "category": category,
                        "paused": False,
                        "status": limiter.get_all_status().get(category, {}),
                    })
                    return
            self._send_json(400, {"error": "Invalid category action. Use /api/categories/{category}/pause or /resume"})
            return

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

            # Register per-endpoint rate limits if provided
            rate_limits = body.get("rate_limits")
            if rate_limits and endpoint:
                app.dispatcher.register_endpoint_limits(
                    endpoint,
                    rate_limits.get("max_concurrent_jobs", 2),
                    rate_limits.get("min_interval_seconds", 10.0),
                )

            job_id = app.store.insert_job(
                endpoint=endpoint,
                submit_tool=submit_tool,
                args=args_str,
                status_tool=body.get("status_tool"),
                result_tool=body.get("result_tool"),
                headers=json.dumps(body["headers"]) if body.get("headers") else None,
            )

            resp_data = {"job_id": job_id, "status": "pending"}

            # Warn if the category is paused
            limiter = app.dispatcher.category_limiter
            cat = limiter.extract_category(endpoint)
            if cat and limiter.is_paused(cat):
                resp_data["warning"] = f"Category {cat} is paused"
                reason = limiter.get_pause_reason(cat)
                if reason:
                    resp_data["pause_reason"] = reason

            self._send_json(200, resp_data)
            return

        if self.path == "/api/worker/shutdown":
            try:
                body = json.loads(self._read_body() or b"{}")
            except (json.JSONDecodeError, ValueError):
                body = {}
            timeout = min(int(body.get("timeout", 30)), 120)
            self._send_json(202, {"status": "shutting_down", "timeout": timeout})
            threading.Thread(target=app.stop, daemon=True).start()
            return

        self._send_json(404, {"error": "Not found"})

    def do_PATCH(self):  # noqa: N802
        app: WorkerApp = self.server.app  # type: ignore
        app.touch()

        if self.path == "/api/config":
            try:
                body = json.loads(self._read_body())
            except (json.JSONDecodeError, ValueError):
                self._send_json(400, {"error": "Invalid JSON"})
                return

            applied = {}
            rejected = {}
            requires_restart = []
            limiter = app.dispatcher.category_limiter

            # Category settings
            cat = body.get("category", {})
            for key, setter, vtype, vmin in [
                ("max_inflight", limiter.set_max_inflight, int, 1),
                ("min_interval", limiter.set_min_interval, float, 0),
                ("exhaust_cooldown", limiter.set_exhaust_cooldown, float, 0),
            ]:
                if key in cat:
                    v = cat[key]
                    try:
                        v = vtype(v)
                        if v < vmin:
                            raise ValueError
                        setter(v)
                        applied[f"category.{key}"] = v
                    except (ValueError, TypeError):
                        rejected[f"category.{key}"] = f"must be {vtype.__name__} >= {vmin}"

            # Endpoint defaults
            ep = body.get("endpoint", {})
            ep_mc = ep.get("default_max_concurrent")
            ep_mi = ep.get("default_min_interval")
            if ep_mc is not None:
                try:
                    ep_mc = int(ep_mc)
                    if ep_mc < 1:
                        raise ValueError
                    applied["endpoint.default_max_concurrent"] = ep_mc
                except (ValueError, TypeError):
                    rejected["endpoint.default_max_concurrent"] = "must be int >= 1"
                    ep_mc = None
            if ep_mi is not None:
                try:
                    ep_mi = float(ep_mi)
                    if ep_mi < 0:
                        raise ValueError
                    applied["endpoint.default_min_interval"] = ep_mi
                except (ValueError, TypeError):
                    rejected["endpoint.default_min_interval"] = "must be float >= 0"
                    ep_mi = None
            if ep_mc is not None or ep_mi is not None:
                app.dispatcher.config.set_defaults(
                    max_concurrent=ep_mc, min_interval=ep_mi,
                )

            # Worker settings
            wk = body.get("worker", {})
            if "idle_timeout" in wk:
                try:
                    v = int(wk["idle_timeout"])
                    if v < 0:
                        raise ValueError
                    app.idle_timeout = v
                    applied["worker.idle_timeout"] = v
                except (ValueError, TypeError):
                    rejected["worker.idle_timeout"] = "must be int >= 0"
            if "port" in wk:
                requires_restart.append("port")

            self._send_json(200, {
                "applied": applied,
                "requires_restart": requires_restart,
                "rejected": rejected,
            })
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
        results_dir: str | None = None,
    ):
        self.host = host
        self.port = port
        self.results_dir = results_dir
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

    def start(
        self,
        rollback_fn: Callable | None = None,
        retention_seconds: float = 86400.0,
    ):
        """Start HTTP server, dispatcher, and idle monitor.

        Args:
            rollback_fn: Optional callable(store) to handle stale jobs on startup.
            retention_seconds: Purge completed/failed jobs older than this (default 24h).
        """
        self._running = True

        # Purge old completed/failed jobs
        self.store.purge_old_jobs(retention_seconds)

        # Run zombie job state transitions
        if rollback_fn is not None:
            rollback_fn(self.store)

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
            try:
                elapsed = time.monotonic() - self._last_access
                active = self.store.count_all_active_jobs()
                pending = self.store.count_all_pending_jobs()
                if active == 0 and pending == 0 and elapsed > self.idle_timeout:
                    self.stop()
                    return
            except Exception:
                logger.exception("Idle monitor error (will retry)")
