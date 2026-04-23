# -*- coding: utf-8 -*-
"""Queue dispatcher with per-endpoint rate limiting.

Reads pending jobs from the DB, evaluates per-endpoint concurrency and
interval limits, and submits eligible jobs to a thread pool for execution.
"""
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

logger = logging.getLogger(__name__)

from . import db
from .category_limiter import CategoryLimiter


class QueueConfig:
    """Rate limit and server configuration."""

    def __init__(self):
        self.host: str = "127.0.0.1"
        self.port: int = 54321
        self.idle_timeout: int = 60
        self.default_max_concurrent: int = 2
        self.default_min_interval: float = 10.0
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

        cfg.category_rate_limits = d.get("category_rate_limits", {})
        cfg.stale_polling_timeout = float(
            d.get("stale_polling_timeout_seconds", 1800.0)
        )
        return cfg

    @classmethod
    def from_file(cls, path: str) -> "QueueConfig":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def set_defaults(self, max_concurrent: int | None = None, min_interval: float | None = None):
        """Update default rate limits at runtime."""
        if max_concurrent is not None:
            self.default_max_concurrent = max(1, int(max_concurrent))
        if min_interval is not None:
            self.default_min_interval = max(0.0, float(min_interval))

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
        self.category_limiter = CategoryLimiter(
            getattr(config, "category_rate_limits", None)
        )
        self._last_run_time: dict[str, float] = {}
        self._pause_until: dict[str, float] = {}
        self._endpoint_paused: set[str] = set()
        self._endpoint_pause_reason: dict[str, dict] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._running = False
        self._thread: threading.Thread | None = None

    def pause_endpoint(self, endpoint: str, seconds: float):
        """Suspend new dispatches to an endpoint for *seconds* seconds."""
        self._pause_until[endpoint] = time.monotonic() + seconds

    def resume_endpoint(self, endpoint: str):
        """Remove endpoint pause (for non-429 error recovery)."""
        self._endpoint_paused.discard(endpoint)
        self._endpoint_pause_reason.pop(endpoint, None)
        logger.info("[Dispatcher] Endpoint resumed: %s", endpoint)

    def is_endpoint_paused(self, endpoint: str) -> bool:
        """Return True if *endpoint* is paused due to a non-429 error."""
        return endpoint in self._endpoint_paused

    def get_endpoint_pause_reason(self, endpoint: str) -> dict | None:
        """Return pause reason for an endpoint, or None."""
        return self._endpoint_pause_reason.get(endpoint)

    def get_all_endpoint_pauses(self) -> dict:
        """Return all endpoint pause states."""
        return dict(self._endpoint_pause_reason)

    def register_endpoint_limits(
        self, endpoint: str, max_concurrent: int, min_interval: float
    ):
        """Register per-endpoint rate limits in-memory (does not overwrite existing)."""
        if endpoint not in self.config._endpoint_limits:
            self.config._endpoint_limits[endpoint] = (max_concurrent, min_interval)

    def dispatch_once(self) -> int:
        """Evaluate all pending/recovering endpoints and dispatch eligible jobs.

        Returns the number of jobs dispatched in this round.
        """
        dispatched = 0
        dispatched_categories: set[str] = set()

        # --- Phase 1: pending jobs (existing logic) ---
        endpoints = self.store.get_pending_endpoints()

        for ep in endpoints:
            # Check category limit
            category = self.category_limiter.extract_category(ep)
            if not self.category_limiter.can_submit(category):
                continue

            # Limit to one dispatch per category per round to avoid
            # submit bursts across different endpoints in the same category
            if category is not None and category in dispatched_categories:
                continue

            # Check endpoint pause (non-429 error pause)
            if ep in self._endpoint_paused:
                continue

            max_concurrent, min_interval = self.config.get_limits(ep)

            # Check temporary pause (cooldown)
            if time.monotonic() < self._pause_until.get(ep, 0.0):
                continue

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
                # Re-check category limit before each dispatch
                if not self.category_limiter.can_submit(category):
                    break

                job = self.store.get_oldest_pending(ep)
                if job is None:
                    break

                self.store.update_status(job["id"], "running")
                self.category_limiter.touch_submit(category)
                self._last_run_time[ep] = time.monotonic()
                self._pool.submit(self._run_job, job)
                dispatched += 1
                available_slots -= 1
                if category is not None:
                    dispatched_categories.add(category)

                # After first dispatch, re-check interval for subsequent jobs
                if min_interval > 0:
                    break  # Must wait for interval before next dispatch

        # --- Phase 2: recovering jobs (zombie recovery) ---
        recovering_endpoints = self.store.get_recovering_endpoints()

        for ep in recovering_endpoints:
            # Check category pause (but NOT quota — recovery doesn't consume a new submit)
            category = self.category_limiter.extract_category(ep)
            if category is not None and self.category_limiter.is_paused(category):
                continue

            # Check endpoint pause
            if ep in self._endpoint_paused:
                continue

            max_concurrent, min_interval = self.config.get_limits(ep)

            # Check temporary pause (cooldown)
            if time.monotonic() < self._pause_until.get(ep, 0.0):
                continue

            # Check concurrency limit (recovering is NOT counted as active)
            active = self.store.count_active_jobs(ep)
            if active >= max_concurrent:
                continue

            # Check interval limit
            last_run = self._last_run_time.get(ep, 0.0)
            now = time.monotonic()
            if (now - last_run) < min_interval:
                continue

            job = self.store.get_oldest_recovering(ep)
            if job is None:
                continue

            # Transition to polling (skip running — no re-submit, no record_submit)
            self.store.update_status(job["id"], "polling")
            self._last_run_time[ep] = time.monotonic()
            self._pool.submit(self._run_job, job)
            dispatched += 1

        # --- Phase 3: stale polling detection ---
        stale_timeout = getattr(self.config, "stale_polling_timeout", 1800.0)
        if stale_timeout > 0:
            stale_jobs = self.store.get_stale_polling(stale_timeout)
            for job in stale_jobs:
                self.store.update_status(job["id"], "recovering")
                logger.warning(
                    "[Dispatcher] Stale polling detected: job %s → recovering "
                    "(no heartbeat for %ds)",
                    job["id"][:8], int(stale_timeout),
                )

        return dispatched

    @staticmethod
    def _extract_error_detail(exc: Exception) -> str:
        """Build a detailed error string from an exception."""
        detail: dict = {"type": type(exc).__name__, "message": str(exc)}
        resp = getattr(exc, "response", None)
        if resp is not None:
            detail["status_code"] = getattr(resp, "status_code", None)
            try:
                detail["response_body"] = resp.text[:2000]
            except Exception:
                pass
        return json.dumps(detail, ensure_ascii=False)

    def _run_job(self, job: dict):
        """Execute a job via the injected executor."""
        category = self.category_limiter.extract_category(job["endpoint"])
        acquired = (
            self.category_limiter.acquire_inflight(category)
            if category else False
        )
        had_error = False
        try:
            self.job_executor(job)
            if category:
                self.category_limiter.record_success(category)
        except Exception as e:
            had_error = True
            resp = getattr(e, "response", None)
            status_code = getattr(resp, "status_code", 0) if resp else 0

            # Fallback: extract status code from error message
            # (e.g. "429 Client Error: Too Many Requests for url: ...")
            if status_code == 0:
                import re
                m = re.match(r"(\d{3})\s", str(e))
                if m:
                    status_code = int(m.group(1))

            body_text = ""
            if resp is not None:
                try:
                    body_text = resp.text[:2000]
                except Exception:
                    pass

            if status_code == 429:
                # 429 does NOT consume server quota → requeue + cooldown
                if category:
                    self.category_limiter.force_cooldown(category)
                    self.category_limiter.record_429(category)
                # Always set endpoint-level cooldown (covers unknown categories)
                self.pause_endpoint(
                    job["endpoint"],
                    self.category_limiter._exhaust_cooldown,
                )
                self.store.update_status(
                    job["id"], "pending",
                    error=(
                        f"Rate Limit (429) - will retry after cooldown: {body_text}"
                        if body_text else "Rate Limit (429) - will retry after cooldown"
                    ),
                )
            else:
                # Non-429 errors consume quota → failed + pause endpoint
                error_detail = self._extract_error_detail(e)
                self.store.update_status(
                    job["id"], "failed", error=error_detail
                )
                endpoint = job["endpoint"]
                self._endpoint_paused.add(endpoint)
                self._endpoint_pause_reason[endpoint] = {
                    "reason": "submit_error",
                    "status_code": status_code,
                    "error_detail": error_detail[:2000],
                    "job_id": job["id"],
                    "paused_at": time.monotonic(),
                }
                logger.warning(
                    "[Dispatcher] Endpoint paused: %s (HTTP %s)",
                    endpoint, status_code,
                )
        finally:
            if acquired:
                self.category_limiter.release_inflight(
                    category, success=not had_error
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
            try:
                self.dispatch_once()
            except Exception:
                logger.exception("Dispatcher error (will retry next loop)")
                time.sleep(1.0)
            time.sleep(self.loop_interval)
