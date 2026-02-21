# -*- coding: utf-8 -*-
"""Queue dispatcher with per-endpoint rate limiting.

Reads pending jobs from the DB, evaluates per-endpoint concurrency and
interval limits, and submits eligible jobs to a thread pool for execution.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from . import db


class QueueConfig:
    """Rate limit and server configuration."""

    def __init__(self):
        self.host: str = "127.0.0.1"
        self.port: int = 54321
        self.idle_timeout: int = 60
        self.default_max_concurrent: int = 2
        self.default_min_interval: float = 2.0
        self._endpoint_limits: dict[str, tuple[int, float]] = {}

    @classmethod
    def from_dict(cls, d: dict) -> "QueueConfig":
        cfg = cls()
        cfg.host = d.get("host", cfg.host)
        cfg.port = d.get("port", cfg.port)
        cfg.idle_timeout = d.get("idle_timeout_seconds", cfg.idle_timeout)

        default_rl = d.get("default_rate_limit", {})
        cfg.default_max_concurrent = default_rl.get(
            "max_concurrent_jobs", cfg.default_max_concurrent
        )
        cfg.default_min_interval = default_rl.get(
            "min_interval_seconds", cfg.default_min_interval
        )

        for ep, limits in d.get("endpoint_rate_limits", {}).items():
            cfg._endpoint_limits[ep] = (
                limits.get("max_concurrent_jobs", cfg.default_max_concurrent),
                limits.get("min_interval_seconds", cfg.default_min_interval),
            )
        return cfg

    @classmethod
    def from_file(cls, path: str) -> "QueueConfig":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def get_limits(self, endpoint: str) -> tuple[int, float]:
        """Return (max_concurrent_jobs, min_interval_seconds) for an endpoint."""
        return self._endpoint_limits.get(
            endpoint,
            (self.default_max_concurrent, self.default_min_interval),
        )


class Dispatcher:
    """Evaluates pending jobs against rate limits and dispatches to executor."""

    def __init__(
        self,
        store: db.JobStore,
        config: QueueConfig,
        job_executor: Callable[[dict], None],
        loop_interval: float = 0.1,
        max_workers: int = 10,
    ):
        self.store = store
        self.config = config
        self.job_executor = job_executor
        self.loop_interval = loop_interval
        self._last_run_time: dict[str, float] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._running = False
        self._thread: threading.Thread | None = None

    def dispatch_once(self) -> int:
        """Evaluate all pending endpoints and dispatch eligible jobs.

        Returns the number of jobs dispatched in this round.
        """
        dispatched = 0
        endpoints = self.store.get_pending_endpoints()

        for ep in endpoints:
            max_concurrent, min_interval = self.config.get_limits(ep)

            # Check concurrency limit
            active = self.store.count_active_jobs(ep)
            if active >= max_concurrent:
                continue

            # Check interval limit
            last_run = self._last_run_time.get(ep, 0.0)
            now = time.monotonic()
            if (now - last_run) < min_interval:
                continue

            # How many slots are available?
            available_slots = max_concurrent - active

            # Dispatch as many pending jobs as slots allow
            while available_slots > 0:
                job = self.store.get_oldest_pending(ep)
                if job is None:
                    break

                self.store.update_status(job["id"], "running")
                self._last_run_time[ep] = time.monotonic()
                self._pool.submit(self._run_job, job)
                dispatched += 1
                available_slots -= 1

                # After first dispatch, re-check interval for subsequent jobs
                if min_interval > 0:
                    break  # Must wait for interval before next dispatch

        return dispatched

    def _run_job(self, job: dict):
        """Execute a job via the injected executor."""
        try:
            self.job_executor(job)
        except Exception as e:
            self.store.update_status(
                job["id"], "failed", error=str(e)
            )

    def start(self):
        """Start the dispatcher loop in a background thread."""
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the dispatcher loop."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
        self._pool.shutdown(wait=True)

    def _loop(self):
        while self._running:
            self.dispatch_once()
            time.sleep(self.loop_interval)
