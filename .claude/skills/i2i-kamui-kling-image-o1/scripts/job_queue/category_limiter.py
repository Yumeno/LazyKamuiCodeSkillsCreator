# -*- coding: utf-8 -*-
"""Category-level rate limiting for MCP job queue.

Tracks per-category (t2i, i2i, t2v, i2v) submit counts using fixed time
windows (clock-hour and calendar-day).  Provides manual pause/resume and
force-exhaust (triggered by real 429 responses from the server).
"""
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

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
    """Fixed-window category rate limiter with manual pause support."""

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

        self._hourly: dict[str, dict] = {}
        self._daily: dict[str, dict] = {}
        self._paused: set[str] = set()
        self._last_submit: dict[str, float] = {}
        self._min_interval: float = float(config.get("min_interval", 1.0))
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

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def can_submit(self, category: str) -> bool:
        """Return True if *category* has remaining quota and is not paused."""
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

    def force_exhaust_hourly(self, category: str):
        """Mark hourly window as exhausted (real 429 received)."""
        if category is None:
            return
        with self._lock:
            self._get_hourly(category)["exhausted"] = True

    def force_exhaust_daily(self, category: str):
        """Mark daily window as exhausted (real 429 received)."""
        if category is None:
            return
        with self._lock:
            self._get_daily(category)["exhausted"] = True

    # ------------------------------------------------------------------
    # Manual pause / resume
    # ------------------------------------------------------------------

    def pause_category(self, category: str):
        """Manually pause a category. Pending jobs stay in queue."""
        with self._lock:
            self._paused.add(category)

    def resume_category(self, category: str):
        """Remove manual pause. Dispatch resumes if limits allow."""
        with self._lock:
            self._paused.discard(category)

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
            result = {}
            for cat, limits in self._limits.items():
                hourly = self._get_hourly(cat)
                daily = self._get_daily(cat)
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
                }
            return result
