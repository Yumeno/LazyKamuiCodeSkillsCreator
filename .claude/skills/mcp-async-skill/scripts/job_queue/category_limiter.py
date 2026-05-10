# -*- coding: utf-8 -*-
"""Category-level rate limiting for MCP job queue.

Manages per-category (t2i, i2i, t2v, i2v) dispatch gating with
**per-category individual values** for inflight, min_interval, and
429 cooldown:

- Inflight control (submit concurrency per category)
- Rolling-window cooldown after 429 responses
- Immediate pause with detailed reason on non-429 submit errors
- Manual pause/resume with category state reporting

Each category has its own `max_inflight`, `min_interval`, `exhaust_cooldown`
configured via the `limits` mapping. Categories or keys not in `limits`
fall back to module-level hardcoded defaults (this is a safety net for
config typos; the canonical schema fully populates `limits`).

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

# Hardcoded fallback values for categories / keys missing from `limits`.
# The canonical schema is to fully populate `limits.{cat}.{key}` for each
# known category. These defaults exist only as a safety net for config typos
# and for unit-test convenience.
HARDCODED_DEFAULT_MAX_INFLIGHT: int = 1
HARDCODED_DEFAULT_MIN_INTERVAL: float = 1.0
HARDCODED_DEFAULT_EXHAUST_COOLDOWN: float = 3600.0


class CategoryLimiter:
    """Category limiter with per-category inflight, cooldown, and pause control."""

    def __init__(self, config: dict | None = None):
        config = config or {}

        # ---- Build category set + aliases ----
        cat_list = config.get("categories", None)
        if cat_list is not None:
            self._categories: set[str] = set(cat_list)
        else:
            # Legacy: extract from "limits" keys if present, else use defaults
            limits_block = config.get("limits", None)
            self._categories = set(limits_block.keys()) if limits_block else set(KNOWN_CATEGORIES)

        self._aliases: dict[str, str] = {
            **DEFAULT_ALIASES,
            **config.get("aliases", {}),
        }

        # ---- Per-category limits (dict) ----
        self._max_inflight: dict[str, int] = {}
        self._min_interval: dict[str, float] = {}
        self._exhaust_cooldown: dict[str, float] = {}

        legacy_used = self._load_limits_from_config(config)

        # ---- Pause / runtime state ----
        self._paused: set[str] = set()
        self._pause_reason: dict[str, dict] = {}
        self._last_submit: dict[str, float] = {}
        self._inflight: dict[str, int] = {}
        self._exhaust_time: dict[str, float] = {}
        self._consecutive_429: dict[str, int] = {}

        self._lock = threading.Lock()

        # v3 fix #9: deprecation warning fires once per CategoryLimiter instance
        # (not process-globally). Multiple workers / multiple limiters each emit
        # their own warning, which is intentional for visibility.
        if legacy_used:
            logger.warning(
                "[CategoryLimiter] (instance %s) DEPRECATED: flat "
                "`max_category_inflight` / `min_interval` / `exhaust_cooldown` "
                "in category_rate_limits will be removed in lazy-v2.13.0 or later. "
                "Migrate to `limits.{category}.{key}` per-category form. "
                "See docs/category-limits.md.",
                id(self),
            )

    # ------------------------------------------------------------------
    # Config loading (with legacy compatibility)
    # ------------------------------------------------------------------

    def _load_limits_from_config(self, config: dict) -> bool:
        """Populate per-category dicts from config. Returns True if legacy
        flat schema was used (so caller can emit a deprecation warning).
        """
        new_limits = config.get("limits", None)
        legacy_max = config.get("max_category_inflight", None)
        legacy_interval = config.get("min_interval", None)
        legacy_cooldown = config.get("exhaust_cooldown", None)

        legacy_used = False

        # ---- New schema: explicit per-category `limits` ----
        if new_limits and isinstance(new_limits, dict):
            for cat, overrides in new_limits.items():
                if not isinstance(overrides, dict):
                    continue
                if "max_inflight" in overrides:
                    self._max_inflight[cat] = int(overrides["max_inflight"])
                if "min_interval" in overrides:
                    self._min_interval[cat] = float(overrides["min_interval"])
                if "exhaust_cooldown" in overrides:
                    self._exhaust_cooldown[cat] = float(overrides["exhaust_cooldown"])

        # ---- Legacy schema: flat `max_category_inflight` etc. ----
        # Fan out the legacy scalar value to ALL configured categories that
        # don't already have a per-category override. The new schema wins.
        if legacy_max is not None or legacy_interval is not None or legacy_cooldown is not None:
            legacy_used = True
            for cat in self._categories:
                if legacy_max is not None and cat not in self._max_inflight:
                    self._max_inflight[cat] = int(legacy_max)
                if legacy_interval is not None and cat not in self._min_interval:
                    self._min_interval[cat] = float(legacy_interval)
                if legacy_cooldown is not None and cat not in self._exhaust_cooldown:
                    self._exhaust_cooldown[cat] = float(legacy_cooldown)

        return legacy_used

    # ------------------------------------------------------------------
    # Per-category limit getters (public API)
    # ------------------------------------------------------------------

    def get_max_inflight(self, category: str | None) -> int:
        """Return max_inflight for the category. Hardcoded default for unknown."""
        if category is None or category not in self._categories:
            logger.debug(
                "[CategoryLimiter] get_max_inflight(%r): unknown category, "
                "returning hardcoded default", category,
            )
            return HARDCODED_DEFAULT_MAX_INFLIGHT
        return self._max_inflight.get(category, HARDCODED_DEFAULT_MAX_INFLIGHT)

    def get_min_interval(self, category: str | None) -> float:
        """Return min_interval for the category. Hardcoded default for unknown."""
        if category is None or category not in self._categories:
            logger.debug(
                "[CategoryLimiter] get_min_interval(%r): unknown category, "
                "returning hardcoded default", category,
            )
            return HARDCODED_DEFAULT_MIN_INTERVAL
        return self._min_interval.get(category, HARDCODED_DEFAULT_MIN_INTERVAL)

    def get_exhaust_cooldown(self, category: str | None) -> float:
        """Return 429 cooldown for the category. Hardcoded default for unknown.

        v3 fix M3: this replaces direct `_exhaust_cooldown` attribute access
        (which was a scalar in lazy-v2.10.x and is now a per-category dict).
        Callers from dispatcher / worker MUST use this getter.
        """
        if category is None or category not in self._categories:
            logger.debug(
                "[CategoryLimiter] get_exhaust_cooldown(%r): unknown category, "
                "returning hardcoded default", category,
            )
            return HARDCODED_DEFAULT_EXHAUST_COOLDOWN
        return self._exhaust_cooldown.get(category, HARDCODED_DEFAULT_EXHAUST_COOLDOWN)

    def is_known_category(self, category: str | None) -> bool:
        """Return True if `category` is one of the configured categories."""
        if category is None:
            return False
        return category in self._categories

    def get_categories(self) -> list[str]:
        """Return the configured category list (sorted, deterministic)."""
        return sorted(self._categories)

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

    def can_submit(self, category: str | None) -> bool:
        """Return True if *category* can submit now.

        v3 fix #1: unknown categories return **False** so dispatcher can
        treat them consistently with `acquire_inflight()` (which also
        returns False). The dispatcher is expected to skip the category
        check entirely when `extract_category()` returns None, so this
        function returning False for unknown is only reached if the
        caller explicitly passes an unknown category name.
        """
        if category is None:
            # category-less endpoints bypass category accounting in the dispatcher
            return True
        with self._lock:
            if category not in self._categories:
                # v3 fix #1: unknown is OUT OF SCOPE — no accounting at all.
                return False
            if category in self._paused:
                return False

            # Enforce minimum interval between submits
            now = time.monotonic()
            min_interval = self._min_interval.get(category, HARDCODED_DEFAULT_MIN_INTERVAL)
            if (now - self._last_submit.get(category, 0.0)) < min_interval:
                return False

            # Inflight check
            max_inflight = self._max_inflight.get(category, HARDCODED_DEFAULT_MAX_INFLIGHT)
            if self._inflight.get(category, 0) >= max_inflight:
                return False

            # Cooldown check (429 rolling window)
            exhaust_at = self._exhaust_time.get(category, 0.0)
            if exhaust_at > 0:
                cooldown = self._exhaust_cooldown.get(
                    category, HARDCODED_DEFAULT_EXHAUST_COOLDOWN
                )
                if (now - exhaust_at) < cooldown:
                    return False
                else:
                    # Cooldown expired
                    self._exhaust_time.pop(category, None)
                    logger.info(
                        "[CategoryLimiter] Cooldown expired for %s, resuming",
                        category,
                    )

            return True

    def touch_submit(self, category: str | None):
        """Update last-submit timestamp (for min_interval enforcement)."""
        if category is None:
            return
        with self._lock:
            self._last_submit[category] = time.monotonic()

    def record_success(self, category: str | None):
        """Record a successful submit — resets consecutive 429 counter."""
        if category is None:
            return
        with self._lock:
            self._consecutive_429.pop(category, None)

    # ------------------------------------------------------------------
    # Inflight control
    # ------------------------------------------------------------------

    def acquire_inflight(self, category: str | None) -> bool:
        """Try to acquire an inflight slot. Returns True if acquired.

        v3 fix #1: unknown categories return **False** and do NOT create
        inflight state. This keeps `can_submit` and `acquire_inflight`
        consistent for unknown keys.
        """
        if category is None:
            return False
        with self._lock:
            if category not in self._categories:
                # v3 fix #1: do not create inflight state for unknown
                return False
            max_inflight = self._max_inflight.get(category, HARDCODED_DEFAULT_MAX_INFLIGHT)
            current = self._inflight.get(category, 0)
            if current >= max_inflight:
                return False
            self._inflight[category] = current + 1
            return True

    def release_inflight(self, category: str | None, success: bool):
        """Release an inflight slot."""
        if category is None:
            return
        with self._lock:
            if category not in self._categories:
                # No state to release for unknown (acquire_inflight returned False)
                return
            self._inflight[category] = max(0, self._inflight.get(category, 0) - 1)

    # ------------------------------------------------------------------
    # Runtime config setters
    # ------------------------------------------------------------------

    def set_max_inflight(self, category: str, value: int):
        """Change max concurrent inflight jobs for a SPECIFIC category at runtime.

        BREAKING CHANGE in lazy-v2.11.0: the previous single-arg signature
        ``set_max_inflight(value)`` (which updated all categories at once)
        is removed. Callers must now pass an explicit category. The worker's
        ``PATCH /api/config`` handler is responsible for emulating the
        legacy "apply to all" behaviour at the API layer when receiving a
        legacy-shaped body.
        """
        with self._lock:
            if category not in self._categories:
                logger.warning(
                    "[CategoryLimiter] set_max_inflight: unknown category %r ignored",
                    category,
                )
                return
            self._max_inflight[category] = max(1, int(value))

    def set_min_interval(self, category: str, value: float):
        """Change minimum interval between submits for a SPECIFIC category."""
        with self._lock:
            if category not in self._categories:
                logger.warning(
                    "[CategoryLimiter] set_min_interval: unknown category %r ignored",
                    category,
                )
                return
            self._min_interval[category] = max(0.0, float(value))

    def set_exhaust_cooldown(self, category: str, value: float):
        """Change 429 cooldown duration for a SPECIFIC category."""
        with self._lock:
            if category not in self._categories:
                logger.warning(
                    "[CategoryLimiter] set_exhaust_cooldown: unknown category %r ignored",
                    category,
                )
                return
            self._exhaust_cooldown[category] = max(0.0, float(value))

    def get_config(self) -> dict:
        """Return current runtime configuration (per-category dicts).

        New shape:
            {
                "limits": {
                    "t2i": {"max_inflight": 3, "min_interval": 1.0, "exhaust_cooldown": 600},
                    ...
                }
            }
        """
        with self._lock:
            limits = {}
            for cat in sorted(self._categories):
                limits[cat] = {
                    "max_inflight": self._max_inflight.get(cat, HARDCODED_DEFAULT_MAX_INFLIGHT),
                    "min_interval": self._min_interval.get(cat, HARDCODED_DEFAULT_MIN_INTERVAL),
                    "exhaust_cooldown": self._exhaust_cooldown.get(
                        cat, HARDCODED_DEFAULT_EXHAUST_COOLDOWN
                    ),
                }
            return {"limits": limits}

    # ------------------------------------------------------------------
    # 429 handling (cooldown only — no auto-pause)
    # ------------------------------------------------------------------

    def force_cooldown(self, category: str | None):
        """Start rolling cooldown for the category (on 429)."""
        if category is None:
            return
        with self._lock:
            if category not in self._categories:
                return
            self._exhaust_time[category] = time.monotonic()

    def record_429(self, category: str | None):
        """Increment consecutive 429 counter (informational only).

        Unlike previous versions, this does NOT auto-pause the category.
        The cooldown from ``force_cooldown`` is sufficient — once it expires,
        the dispatcher will automatically retry pending jobs.
        """
        if category is None:
            return
        with self._lock:
            if category not in self._categories:
                return
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
        category: str | None,
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
            if category not in self._categories:
                return
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
            if category not in self._categories:
                return
            self._paused.add(category)
            self._pause_reason[category] = {
                "reason": "manual",
                "paused_at": datetime.now(timezone.utc).isoformat(),
            }

    def resume_category(self, category: str):
        """Remove pause, clear reason and consecutive 429 counter."""
        with self._lock:
            if category not in self._categories:
                return
            self._paused.discard(category)
            self._pause_reason.pop(category, None)
            self._consecutive_429.pop(category, None)
            # Also clear cooldown so dispatch resumes immediately
            self._exhaust_time.pop(category, None)

    def is_paused(self, category: str | None) -> bool:
        """Return True if *category* is paused."""
        if category is None:
            return False
        with self._lock:
            return category in self._paused

    def get_pause_reason(self, category: str | None) -> dict | None:
        """Return pause reason dict, or None if not paused."""
        if category is None:
            return None
        with self._lock:
            return self._pause_reason.get(category)

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_all_status(self) -> dict:
        """Return status for all configured categories.

        Each category's `max_inflight` reflects its per-category value
        (v3 change: was a single shared value in lazy-v2.10.x).
        """
        with self._lock:
            now = time.monotonic()
            result = {}
            for cat in sorted(self._categories):
                exhaust_at = self._exhaust_time.get(cat, 0.0)
                cooldown = self._exhaust_cooldown.get(
                    cat, HARDCODED_DEFAULT_EXHAUST_COOLDOWN
                )
                cooldown_remaining = max(
                    0.0, cooldown - (now - exhaust_at)
                ) if exhaust_at > 0 else 0.0

                entry: dict = {
                    "paused": cat in self._paused,
                    "inflight": self._inflight.get(cat, 0),
                    "max_inflight": self._max_inflight.get(
                        cat, HARDCODED_DEFAULT_MAX_INFLIGHT
                    ),
                    "min_interval": self._min_interval.get(
                        cat, HARDCODED_DEFAULT_MIN_INTERVAL
                    ),
                    "exhaust_cooldown": cooldown,
                    "consecutive_429": self._consecutive_429.get(cat, 0),
                    "cooldown_remaining_s": round(cooldown_remaining, 1),
                }
                reason = self._pause_reason.get(cat)
                if reason:
                    entry["pause_reason"] = reason
                result[cat] = entry
            return result
