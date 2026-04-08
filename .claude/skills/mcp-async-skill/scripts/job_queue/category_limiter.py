# -*- coding: utf-8 -*-
"""Category-level rate limiting for MCP job queue.

Tracks per-category (t2i, i2i, t2v, i2v) submit counts using fixed time
windows (clock-hour and calendar-day).  Provides manual pause/resume,
force-exhaust with rolling-window cooldown, inflight control, and
automatic pause after consecutive 429 errors.
"""
import logging
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_LIMITS: dict[str, dict[str, int]] = {
    "t2i": {"hourly": 20, "daily": 50},
    "i2i": {"hourly": 20, "daily": 50},
    "t2v": {"hourly": 10, "daily": 25},
    "i2v": {"hourly": 10, "daily": 25},
}

DEFAULT_ALIASES: dict[str, str] = {"r2i": "i2i", "r2v": "i2v"}


def _hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")


def _day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class CategoryLimiter:
    """Category rate limiter with inflight control, rolling cooldown, and auto-pause."""

    def __init__(self, config: dict | None = None):
        config = config or {}
        self._limits: dict[str, dict[str, int]] = {
            **DEFAULT_LIMITS,
            **config.get("limits", {}),
        }
        self._aliases: dict[str, str] = {
            **DEFAULT_ALIASES,
            **config.get("aliases", {}),
        }

        # Fixed-window counters
        self._hourly: dict[str, dict] = {}
        self._daily: dict[str, dict] = {}

        # Pause / interval
        self._paused: set[str] = set()
        self._last_submit: dict[str, float] = {}
        self._min_interval: float = float(config.get("min_interval", 1.0))

        # Inflight control (submit concurrency per category)
        self._inflight: dict[str, int] = {}
        self._max_inflight: int = int(config.get("max_category_inflight", 1))

        # Rolling-window cooldown for force_exhaust
        self._exhaust_time: dict[str, float] = {}
        self._exhaust_cooldown: float = float(config.get("exhaust_cooldown", 3600.0))

        # Consecutive 429 auto-pause
        self._consecutive_429: dict[str, int] = {}
        self._auto_pause_threshold: int = int(
            config.get("auto_pause_after_consecutive_429", 3)
        )

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
        if category in self._limits:
            return category
        return None

    # ------------------------------------------------------------------
    # Window helpers (must be called under self._lock)
    # ------------------------------------------------------------------

    def _get_hourly(self, category: str) -> dict:
        cur_key = _hour_key()
        rec = self._hourly.get(category)
        if rec is None or rec["window"] != cur_key:
            rec = {"window": cur_key, "count": 0, "exhausted": False}
            self._hourly[category] = rec
        return rec

    def _get_daily(self, category: str) -> dict:
        cur_key = _day_key()
        rec = self._daily.get(category)
        if rec is None or rec["window"] != cur_key:
            rec = {"window": cur_key, "count": 0, "exhausted": False}
            self._daily[category] = rec
        return rec

    def _check_cooldown_expired(self, category: str) -> None:
        """Clear exhausted flags if rolling cooldown has elapsed."""
        exhaust_at = self._exhaust_time.get(category, 0.0)
        if exhaust_at > 0:
            if (time.monotonic() - exhaust_at) >= self._exhaust_cooldown:
                self._get_hourly(category)["exhausted"] = False
                self._get_daily(category)["exhausted"] = False
                self._exhaust_time.pop(category, None)
                logger.info(
                    "[CategoryLimiter] Cooldown expired for %s, resuming",
                    category,
                )

    # ------------------------------------------------------------------
    # Public API — submit gating
    # ------------------------------------------------------------------

    def can_submit(self, category: str) -> bool:
        """Return True if *category* has remaining quota, is not paused,
        and has inflight slots available."""
        if category is None:
            return True
        with self._lock:
            if category in self._paused:
                return False
            limits = self._limits.get(category)
            if limits is None:
                return True

            # Enforce minimum interval between submits
            now = time.monotonic()
            last = self._last_submit.get(category, 0.0)
            if (now - last) < self._min_interval:
                return False

            # Inflight check
            if self._inflight.get(category, 0) >= self._max_inflight:
                return False

            # Check rolling cooldown expiry
            self._check_cooldown_expired(category)

            hourly = self._get_hourly(category)
            if hourly["exhausted"] or hourly["count"] >= limits["hourly"]:
                return False

            daily = self._get_daily(category)
            if daily["exhausted"] or daily["count"] >= limits["daily"]:
                return False

            return True

    def record_submit(self, category: str):
        """Increment submit count for *category* in both windows."""
        if category is None:
            return
        with self._lock:
            self._get_hourly(category)["count"] += 1
            self._get_daily(category)["count"] += 1
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
    # Force exhaust (real 429 / 503)
    # ------------------------------------------------------------------

    def force_exhaust_hourly(self, category: str, is_429: bool = True):
        """Mark hourly window as exhausted with rolling cooldown.

        Args:
            is_429: True for real 429 (counts toward auto-pause),
                    False for 503 (transient, no auto-pause).
        """
        if category is None:
            return
        with self._lock:
            self._get_hourly(category)["exhausted"] = True
            self._exhaust_time[category] = time.monotonic()

            if is_429:
                count = self._consecutive_429.get(category, 0) + 1
                self._consecutive_429[category] = count
                if count >= self._auto_pause_threshold:
                    self._paused.add(category)
                    logger.warning(
                        "[CategoryLimiter] Auto-paused %s after %d consecutive 429s",
                        category, count,
                    )

    def force_exhaust_daily(self, category: str):
        """Mark daily window as exhausted (real 429 received)."""
        if category is None:
            return
        with self._lock:
            self._get_daily(category)["exhausted"] = True
            self._exhaust_time[category] = time.monotonic()

            count = self._consecutive_429.get(category, 0) + 1
            self._consecutive_429[category] = count
            if count >= self._auto_pause_threshold:
                self._paused.add(category)
                logger.warning(
                    "[CategoryLimiter] Auto-paused %s after %d consecutive 429s",
                    category, count,
                )

    # ------------------------------------------------------------------
    # Manual pause / resume
    # ------------------------------------------------------------------

    def pause_category(self, category: str):
        """Manually pause a category. Pending jobs stay in queue."""
        with self._lock:
            self._paused.add(category)

    def resume_category(self, category: str):
        """Remove manual pause and reset consecutive 429 counter.
        Dispatch resumes if limits allow."""
        with self._lock:
            self._paused.discard(category)
            self._consecutive_429.pop(category, None)

    def is_paused(self, category: str) -> bool:
        """Return True if *category* is manually paused."""
        with self._lock:
            return category in self._paused

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_all_status(self) -> dict:
        """Return status for all configured categories."""
        with self._lock:
            now = time.monotonic()
            result = {}
            for cat, limits in self._limits.items():
                hourly = self._get_hourly(cat)
                daily = self._get_daily(cat)
                exhaust_at = self._exhaust_time.get(cat, 0.0)
                cooldown_remaining = max(
                    0.0, self._exhaust_cooldown - (now - exhaust_at)
                ) if exhaust_at > 0 else 0.0

                result[cat] = {
                    "hourly": {
                        "count": hourly["count"],
                        "limit": limits["hourly"],
                        "exhausted": hourly["exhausted"],
                        "window": hourly["window"],
                    },
                    "daily": {
                        "count": daily["count"],
                        "limit": limits["daily"],
                        "exhausted": daily["exhausted"],
                        "window": daily["window"],
                    },
                    "paused": cat in self._paused,
                    "inflight": self._inflight.get(cat, 0),
                    "max_inflight": self._max_inflight,
                    "consecutive_429": self._consecutive_429.get(cat, 0),
                    "cooldown_remaining_s": round(cooldown_remaining, 1),
                }
            return result
