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
from .custom_group_limiter import CustomGroupLimiter


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
        # PR4 (#60): user-defined endpoint groups with their own
        # rate limits, overriding category accounting on match.
        cfg.custom_groups = d.get("custom_groups", {})
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
        # PR4 (#60): user-defined groups override category accounting on
        # match. An empty group config (= no groups configured) leaves
        # the category_limiter as the sole limiter.
        self.group_limiter = CustomGroupLimiter(
            getattr(config, "custom_groups", None)
        )
        self._last_run_time: dict[str, float] = {}
        self._pause_until: dict[str, float] = {}
        self._endpoint_paused: set[str] = set()
        self._endpoint_pause_reason: dict[str, dict] = {}
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._running = False
        self._thread: threading.Thread | None = None

    def _resolve_limiter(self, endpoint: str):
        """Pick the limiter responsible for ``endpoint``.

        Returns ``(limiter, key)`` where:

        * ``limiter`` is :class:`CustomGroupLimiter` when ``endpoint``
          matches a configured custom group, otherwise
          :class:`CategoryLimiter`, otherwise ``None``.
        * ``key`` is the matched group name / extracted category /
          ``None`` (truly unknown endpoint).

        PR4 contract (PHASE1_PLAN_v3 fix #1 / #11): callers MUST treat
        ``key=None`` (or ``limiter=None``) as "out of accounting scope"
        and skip every rate-limit gate. The dispatcher does so for
        category-less endpoints today and continues to do so for
        endpoints that match no group AND no recognised category.

        This is the single funnel through which dispatch / run paths
        reach the rate limiters. The grep checklist in the PR4 PR body
        verifies that no other call site touches ``self.category_limiter``
        or ``self.group_limiter`` directly except the constructor.
        """
        group = self.group_limiter.extract_group(endpoint)
        if group is not None:
            return self.group_limiter, group
        cat = self.category_limiter.extract_category(endpoint)
        if cat is None:
            return None, None
        return self.category_limiter, cat

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
        # Track keys we have already dispatched against this round to
        # avoid bursts across multiple endpoints sharing the same key
        # (category or group). ``None`` keys are never inserted —
        # category-less / unmatched endpoints have no shared accounting
        # bucket, so each one can dispatch independently.
        dispatched_keys: set[tuple[int, str]] = set()

        # --- Phase 1: pending jobs ---
        endpoints = self.store.get_pending_endpoints()

        for ep in endpoints:
            limiter, key = self._resolve_limiter(ep)

            # Limiter / key gating: only meaningful when we have one.
            if limiter is not None and key is not None:
                if not limiter.can_submit(key):
                    continue
                if (id(limiter), key) in dispatched_keys:
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
                # Re-check limiter before each dispatch (only when applicable)
                if limiter is not None and key is not None:
                    if not limiter.can_submit(key):
                        break

                job = self.store.get_oldest_pending(ep)
                if job is None:
                    break

                self.store.update_status(job["id"], "running")
                if limiter is not None and key is not None:
                    limiter.touch_submit(key)
                self._last_run_time[ep] = time.monotonic()
                self._pool.submit(self._run_job, job)
                dispatched += 1
                available_slots -= 1
                if limiter is not None and key is not None:
                    dispatched_keys.add((id(limiter), key))

                # After first dispatch, re-check interval for subsequent jobs
                if min_interval > 0:
                    break  # Must wait for interval before next dispatch

        # --- Phase 2: recovering jobs (zombie recovery) ---
        recovering_endpoints = self.store.get_recovering_endpoints()

        for ep in recovering_endpoints:
            # Check pause (but NOT quota — recovery doesn't consume a new submit)
            limiter, key = self._resolve_limiter(ep)
            if limiter is not None and key is not None and limiter.is_paused(key):
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
        """Execute a job via the injected executor.

        Resolves the limiter once via :meth:`_resolve_limiter` and uses
        it for inflight acquire/release, 429 cooldown, and success/error
        reporting. ``limiter is None`` (unknown endpoint with no
        category and no group match) means "no rate-limit accounting" —
        the executor still runs, but no inflight state is created and
        no per-key cooldown is applied. The endpoint-level
        ``pause_endpoint`` cooldown still fires for safety.
        """
        endpoint = job["endpoint"]
        limiter, key = self._resolve_limiter(endpoint)
        acquired = (
            limiter.acquire_inflight(key) if (limiter is not None and key is not None) else False
        )
        had_error = False
        try:
            self.job_executor(job)
            if limiter is not None and key is not None:
                limiter.record_success(key)
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
                if limiter is not None and key is not None:
                    limiter.force_cooldown(key)
                    limiter.record_429(key)
                # Always set endpoint-level cooldown. For an unknown
                # endpoint we fall back to CategoryLimiter's hardcoded
                # default — there's no group/category-specific value to
                # use, but we still want SOMETHING so we don't
                # immediately retry a 429.
                if limiter is not None and key is not None:
                    cooldown = limiter.get_exhaust_cooldown(key)
                else:
                    cooldown = self.category_limiter.get_exhaust_cooldown(None)
                self.pause_endpoint(endpoint, cooldown)
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
            if acquired and limiter is not None and key is not None:
                limiter.release_inflight(key, success=not had_error)

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
