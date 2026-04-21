# -*- coding: utf-8 -*-
"""Category-level rate limiting for MCP job queue.

Manages per-category (t2i, i2i, t2v, i2v) dispatch gating:
- Inflight control (submit concurrency per category)
- Rolling-window cooldown after 429 responses
- Immediate pause with detailed reason on non-429 submit errors
- Manual pause/resume with category state reporting

429 errors trigger a cooldown but do NOT auto-pause the category.
The job is returned to pending and retried after the cooldown expires.
Only non-429 errors (which consume server quota) trigger an immediate
category pause to prevent further quota waste.
"""
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

KNOWN_CATEGORIES: set[str] = {"t2i", "i2i", "t2v", "i2v"}

DEFAULT_ALIASES: dict[str, str] = {"r2i": "i2i", "r2v": "i2v"}


class CategoryLimiter:
    """Category limiter with inflight control, rolling cooldown, and error pause."""

    def __init__(self, config: dict | None = None):
        config = config or {}

        # Build category set from config or defaults
        cat_list = config.get("categories", None)
        if cat_list is not None:
            self._categories: set[str] = set(cat_list)
        else:
            # Legacy: extract from "limits" keys if present, else use defaults
            limits = config.get("limits", None)
            self._categories = set(limits.keys()) if limits else set(KNOWN_CATEGORIES)

        self._aliases: dict[str, str] = {
            **DEFAULT_ALIASES,
            **config.get("aliases", {}),
        }

        # Pause state (only for non-429 errors and manual pause)
        self._paused: set[str] = set()
        self._pause_reason: dict[str, dict] = {}

        # Submit interval
        self._last_submit: dict[str, float] = {}
        self._min_interval: float = float(config.get("min_interval", 1.0))

        # Inflight control (submit concurrency per category)
        self._inflight: dict[str, int] = {}
        self._max_inflight: int = int(config.get("max_category_inflight", 1))

        # Rolling-window cooldown for 429
        self._exhaust_time: dict[str, float] = {}
        self._exhaust_cooldown: float = float(config.get("exhaust_cooldown", 3600.0))

        # 429 counter (informational — does NOT trigger auto-pause)
        self._consecutive_429: dict[str, int] = {}

        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Category extraction
    # ------------------------------------------------------------------

    def extract_category(self, endpoint: str) -> str | None:
        """Extract category from endpoint URL.

        ``https://kamui-code.ai/t2i/fal/flux-lora`` → ``"t2i"``

        Returns the canonical category (after alias resolution) or *None*
        if the URL does not match any known category.
        """
        try:
            path = urlparse(endpoint).path
            segments = [s for s in path.split("/") if s]
            if not segments:
                return None
            raw = segments[0]
        except Exception:
            return None

        category = self._aliases.get(raw, raw)
        if category in self._categories:
            return category
        return None

    # ------------------------------------------------------------------
    # Public API — submit gating
    # ------------------------------------------------------------------

    def can_submit(self, category: str) -> bool:
        """Return True if *category* is not paused, has inflight slots,
        and is not in cooldown."""
        if category is None:
            return True
        with self._lock:
            if category in self._paused:
                return False
            if category not in self._categories:
                return True

            # Enforce minimum interval between submits
            now = time.monotonic()
            if (now - self._last_submit.get(category, 0.0)) < self._min_interval:
                return False

            # Inflight check
            if self._inflight.get(category, 0) >= self._max_inflight:
                return False

            # Cooldown check (429 rolling window)
            exhaust_at = self._exhaust_time.get(category, 0.0)
            if exhaust_at > 0:
                if (now - exhaust_at) < self._exhaust_cooldown:
                    return False
                else:
                    # Cooldown expired
                    self._exhaust_time.pop(category, None)
                    logger.info(
                        "[CategoryLimiter] Cooldown expired for %s, resuming",
                        category,
                    )

            return True

    def touch_submit(self, category: str):
        """Update last-submit timestamp (for min_interval enforcement)."""
        if category is None:
            return
        with self._lock:
            self._last_submit[category] = time.monotonic()

    def record_success(self, category: str):
        """Record a successful submit — resets consecutive 429 counter."""
        if category is None:
            return
        with self._lock:
            self._consecutive_429.pop(category, None)

    # ------------------------------------------------------------------
    # Inflight control
    # ------------------------------------------------------------------

    def acquire_inflight(self, category: str) -> bool:
        """Try to acquire an inflight slot. Returns True if acquired."""
        if category is None:
            return False
        with self._lock:
            current = self._inflight.get(category, 0)
            if current >= self._max_inflight:
                return False
            self._inflight[category] = current + 1
            return True

    def release_inflight(self, category: str, success: bool):
        """Release an inflight slot."""
        if category is None:
            return
        with self._lock:
            self._inflight[category] = max(0, self._inflight.get(category, 0) - 1)

    # ------------------------------------------------------------------
    # Runtime config setters
    # ------------------------------------------------------------------

    def set_max_inflight(self, value: int):
        """Change max concurrent inflight jobs per category at runtime."""
        with self._lock:
            self._max_inflight = max(1, int(value))

    def set_min_interval(self, value: float):
        """Change minimum interval between submits at runtime."""
        with self._lock:
            self._min_interval = max(0.0, float(value))

    def set_exhaust_cooldown(self, value: float):
        """Change 429 cooldown duration at runtime."""
        with self._lock:
            self._exhaust_cooldown = max(0.0, float(value))

    def get_config(self) -> dict:
        """Return current runtime configuration values."""
        with self._lock:
            return {
                "max_inflight": self._max_inflight,
                "min_interval": self._min_interval,
                "exhaust_cooldown": self._exhaust_cooldown,
            }

    # ------------------------------------------------------------------
    # 429 handling (cooldown only — no auto-pause)
    # ------------------------------------------------------------------

    def force_cooldown(self, category: str):
        """Start rolling cooldown for the category (on 429)."""
        if category is None:
            return
        with self._lock:
            self._exhaust_time[category] = time.monotonic()

    def record_429(self, category: str):
        """Increment consecutive 429 counter (informational only).

        Unlike previous versions, this does NOT auto-pause the category.
        The cooldown from ``force_cooldown`` is sufficient — once it expires,
        the dispatcher will automatically retry pending jobs.
        """
        if category is None:
            return
        with self._lock:
            count = self._consecutive_429.get(category, 0) + 1
            self._consecutive_429[category] = count
            if count % 10 == 0:
                logger.info(
                    "[CategoryLimiter] %s has hit %d consecutive 429s "
                    "(cooldown handles retry automatically)",
                    category, count,
                )

    # ------------------------------------------------------------------
    # Error-triggered pause (non-429 only)
    # ------------------------------------------------------------------

    def pause_with_reason(
        self,
        category: str,
        reason: str,
        status_code: int | None = None,
        error_detail: str = "",
        job_id: str = "",
        endpoint: str = "",
    ):
        """Pause category due to an error. Stores detailed reason."""
        if category is None:
            return
        with self._lock:
            self._paused.add(category)
            self._pause_reason[category] = {
                "reason": reason,
                "status_code": status_code,
                "error_detail": error_detail[:2000],
                "job_id": job_id,
                "endpoint": endpoint,
                "paused_at": datetime.now(timezone.utc).isoformat(),
            }
        logger.warning(
            "[CategoryLimiter] Paused %s: %s (HTTP %s) — %s",
            category, reason, status_code, error_detail[:200],
        )

    # ------------------------------------------------------------------
    # Manual pause / resume
    # ------------------------------------------------------------------

    def pause_category(self, category: str):
        """Manually pause a category. Pending jobs stay in queue."""
        with self._lock:
            self._paused.add(category)
            self._pause_reason[category] = {
                "reason": "manual",
                "paused_at": datetime.now(timezone.utc).isoformat(),
            }

    def resume_category(self, category: str):
        """Remove pause, clear reason and consecutive 429 counter."""
        with self._lock:
            self._paused.discard(category)
            self._pause_reason.pop(category, None)
            self._consecutive_429.pop(category, None)
            # Also clear cooldown so dispatch resumes immediately
            self._exhaust_time.pop(category, None)

    def is_paused(self, category: str) -> bool:
        """Return True if *category* is paused."""
        with self._lock:
            return category in self._paused

    def get_pause_reason(self, category: str) -> dict | None:
        """Return pause reason dict, or None if not paused."""
        with self._lock:
            return self._pause_reason.get(category)

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_all_status(self) -> dict:
        """Return status for all configured categories."""
        with self._lock:
            now = time.monotonic()
            result = {}
            for cat in sorted(self._categories):
                exhaust_at = self._exhaust_time.get(cat, 0.0)
                cooldown_remaining = max(
                    0.0, self._exhaust_cooldown - (now - exhaust_at)
                ) if exhaust_at > 0 else 0.0

                entry: dict = {
                    "paused": cat in self._paused,
                    "inflight": self._inflight.get(cat, 0),
                    "max_inflight": self._max_inflight,
                    "consecutive_429": self._consecutive_429.get(cat, 0),
                    "cooldown_remaining_s": round(cooldown_remaining, 1),
                }
                reason = self._pause_reason.get(cat)
                if reason:
                    entry["pause_reason"] = reason
                result[cat] = entry
            return result
