# -*- coding: utf-8 -*-
"""User-defined custom-group rate limiting (PR4 / #60).

Categories (``t2i`` / ``i2i`` / ``t2v`` / ``i2v`` / ``r2v``) cover the common
case but cannot express "throttle these specific endpoints together
regardless of category". The upstream MCP service applies extra,
narrower rate limits to certain high-cost models (e.g. video models
priced higher than text-to-image), and we need to mirror that on the
client.

Schema in ``queue_config.json``::

    {
      "custom_groups": {
        "premium-video": {
          "endpoints": [
            "https://kamui-code.ai/t2v/fal/veo3*",
            "https://kamui-code.ai/i2v/fal/veo3*"
          ],
          "max_inflight": 1,
          "min_interval": 30,
          "exhaust_cooldown": 7200
        }
      }
    }

Endpoints are matched against ``endpoints`` patterns (fnmatch / glob
syntax). On a match the dispatcher uses :class:`CustomGroupLimiter`
INSTEAD of :class:`~job_queue.category_limiter.CategoryLimiter` for that
endpoint — the matched group fully replaces category accounting (the
endpoint is not double-counted against its category).

When multiple groups would match the same endpoint, the first one in
configuration order wins. Python 3.7+ guarantees dict insertion
order is preserved, so the wire-format ordering is the user-visible
ordering.

The state machine (inflight, 429 cooldown, pause/resume) is provided
by :class:`~job_queue.limiter_state.LimiterStateMixin`, the same one
:class:`CategoryLimiter` uses. Bug fixes there fix both limiters at
once.

Unknown groups (PHASE1_PLAN_v3 fix #1)
--------------------------------------

``can_submit("unknown_group_name") == acquire_inflight(...) == False``,
and **no inflight state is created**. This mirrors CategoryLimiter's
behaviour so the dispatcher's ``_resolve_limiter()`` (PR4) sees a
consistent contract regardless of which limiter it picked.
"""
from __future__ import annotations

import fnmatch
import logging
import time
from datetime import datetime, timezone

from .limiter_state import LimiterStateMixin

logger = logging.getLogger(__name__)


# Hardcoded fallback values, mirroring CategoryLimiter's HARDCODED_DEFAULT_*.
# These exist as a safety net for config typos; the canonical schema
# requires every group to specify its own values explicitly.
HARDCODED_DEFAULT_MAX_INFLIGHT: int = 1
HARDCODED_DEFAULT_MIN_INTERVAL: float = 1.0
HARDCODED_DEFAULT_EXHAUST_COOLDOWN: float = 3600.0


# Sentinel for the match cache: distinguishes "key not yet computed"
# from "computed and known to match no group". (PHASE1_PLAN_v3 fix #13)
_SENTINEL: object = object()


class CustomGroupLimiter(LimiterStateMixin):
    """Limiter for user-defined endpoint groups.

    See module docstring for the wire format. The relevant constructor
    contract is::

        CustomGroupLimiter({"group_name": {
            "endpoints": ["pattern1", "pattern2"],
            "max_inflight": int,
            "min_interval": float,        # optional
            "exhaust_cooldown": float,    # optional
        }})

    or, with a top-level ``custom_groups`` wrapper::

        CustomGroupLimiter({"custom_groups": { ... }})

    A bare ``CustomGroupLimiter()`` (no config / empty config) is a
    no-op — every endpoint will return ``None`` from
    :meth:`extract_group`, telling the dispatcher to fall through to
    the category limiter.
    """

    def __init__(self, config: dict | None = None):
        config = config or {}

        # Allow either {"custom_groups": {...}} or the inner dict directly.
        if "custom_groups" in config:
            groups_block = config["custom_groups"] or {}
        else:
            groups_block = config

        # Insertion order matters: first match wins (Python 3.7+).
        self._groups: list[str] = []
        self._patterns: dict[str, list[str]] = {}
        self._max_inflight: dict[str, int] = {}
        self._min_interval: dict[str, float] = {}
        self._exhaust_cooldown: dict[str, float] = {}

        if isinstance(groups_block, dict):
            for name, spec in groups_block.items():
                if not isinstance(spec, dict):
                    logger.warning(
                        "[CustomGroupLimiter] group %r: spec must be dict, ignoring",
                        name,
                    )
                    continue
                patterns = spec.get("endpoints")
                if not isinstance(patterns, list) or not patterns:
                    logger.warning(
                        "[CustomGroupLimiter] group %r: 'endpoints' must be a "
                        "non-empty list, ignoring", name,
                    )
                    continue

                self._groups.append(name)
                self._patterns[name] = list(patterns)
                self._max_inflight[name] = int(
                    spec.get("max_inflight", HARDCODED_DEFAULT_MAX_INFLIGHT)
                )
                self._min_interval[name] = float(
                    spec.get("min_interval", HARDCODED_DEFAULT_MIN_INTERVAL)
                )
                self._exhaust_cooldown[name] = float(
                    spec.get("exhaust_cooldown", HARDCODED_DEFAULT_EXHAUST_COOLDOWN)
                )

        # endpoint → group_name (or None) cache. Lock-protected per
        # PHASE1_PLAN_v3 fix #13 so a future `_match_cache` writer can
        # not race a reader.
        self._match_cache: dict[str, str | None] = {}

        self._init_state()

    # ------------------------------------------------------------------
    # Public read-only accessors
    # ------------------------------------------------------------------

    def get_groups(self) -> list[str]:
        """Configured group names in declaration order."""
        return list(self._groups)

    def is_known_group(self, group: str | None) -> bool:
        if group is None:
            return False
        return group in self._patterns

    def get_max_inflight(self, group: str | None) -> int:
        if group is None or group not in self._patterns:
            return HARDCODED_DEFAULT_MAX_INFLIGHT
        return self._max_inflight.get(group, HARDCODED_DEFAULT_MAX_INFLIGHT)

    def get_min_interval(self, group: str | None) -> float:
        if group is None or group not in self._patterns:
            return HARDCODED_DEFAULT_MIN_INTERVAL
        return self._min_interval.get(group, HARDCODED_DEFAULT_MIN_INTERVAL)

    def get_exhaust_cooldown(self, group: str | None) -> float:
        if group is None or group not in self._patterns:
            return HARDCODED_DEFAULT_EXHAUST_COOLDOWN
        return self._exhaust_cooldown.get(group, HARDCODED_DEFAULT_EXHAUST_COOLDOWN)

    # ------------------------------------------------------------------
    # Endpoint matching
    # ------------------------------------------------------------------

    def extract_group(self, endpoint: str) -> str | None:
        """Match ``endpoint`` against the configured groups (fnmatch).

        Returns the first matching group name in configuration order, or
        ``None`` if no group matches. Results are cached for subsequent
        calls with the same endpoint string.

        Cache reads + writes happen under ``self._lock`` so concurrent
        dispatcher threads cannot observe a half-populated cache entry.
        """
        if endpoint is None or not self._groups:
            return None
        with self._lock:
            cached = self._match_cache.get(endpoint, _SENTINEL)
            if cached is not _SENTINEL:
                return cached  # type: ignore[return-value]
            for name in self._groups:
                for pat in self._patterns[name]:
                    try:
                        if fnmatch.fnmatchcase(endpoint, pat):
                            self._match_cache[endpoint] = name
                            return name
                    except Exception:
                        # Defensive: a malformed pattern should not crash
                        # the dispatcher. Log once and continue.
                        logger.warning(
                            "[CustomGroupLimiter] fnmatch failed for "
                            "pattern %r on endpoint %r", pat, endpoint,
                            exc_info=True,
                        )
            self._match_cache[endpoint] = None
            return None

    # ------------------------------------------------------------------
    # Submit gating (mirrors CategoryLimiter's contract)
    # ------------------------------------------------------------------

    def can_submit(self, group: str | None) -> bool:
        if group is None:
            return True  # category-less; dispatcher decides
        with self._lock:
            if group not in self._patterns:
                return False
            if group in self._paused:
                return False

            now = time.monotonic()
            min_interval = self._min_interval.get(group, HARDCODED_DEFAULT_MIN_INTERVAL)
            if (now - self._last_submit.get(group, 0.0)) < min_interval:
                return False

            max_inflight = self._max_inflight.get(group, HARDCODED_DEFAULT_MAX_INFLIGHT)
            if self._inflight.get(group, 0) >= max_inflight:
                return False

            cooldown = self._exhaust_cooldown.get(group, HARDCODED_DEFAULT_EXHAUST_COOLDOWN)
            if not self._check_cooldown_locked(group, cooldown, now):
                return False

            return True

    def acquire_inflight(self, group: str | None) -> bool:
        if group is None:
            return False
        with self._lock:
            if group not in self._patterns:
                return False
            max_inflight = self._max_inflight.get(group, HARDCODED_DEFAULT_MAX_INFLIGHT)
            return self._acquire_inflight_locked(group, max_inflight)

    def release_inflight(self, group: str | None, success: bool):
        if group is None:
            return
        with self._lock:
            if group not in self._patterns:
                return
            self._release_inflight_locked(group)

    # ------------------------------------------------------------------
    # 429 / pause overrides — restrict to known groups
    # ------------------------------------------------------------------

    def touch_submit(self, group: str | None):
        """Update last-submit timestamp for ``min_interval`` enforcement.

        Overrides the mixin to enforce the "unknown keys create no
        state" contract that ``can_submit`` / ``acquire_inflight``
        already follow. Without this guard the mixin would happily
        insert an entry into ``_last_submit`` for an arbitrary string,
        growing the dict unboundedly if a caller ever mistakenly fed
        it raw endpoint URLs.
        """
        if group is None or group not in self._patterns:
            return
        super().touch_submit(group)

    def force_cooldown(self, group: str | None):
        if group is None or group not in self._patterns:
            return
        super().force_cooldown(group)

    def record_429(self, group: str | None):
        if group is None or group not in self._patterns:
            return
        super().record_429(group)

    def record_success(self, group: str | None):
        if group is None or group not in self._patterns:
            return
        super().record_success(group)

    def pause_with_reason(
        self,
        group: str | None,
        reason: str,
        status_code: int | None = None,
        error_detail: str = "",
        job_id: str = "",
        endpoint: str = "",
    ):
        if group is None or group not in self._patterns:
            return
        super().pause_with_reason(
            group,
            reason,
            status_code=status_code,
            error_detail=error_detail,
            job_id=job_id,
            endpoint=endpoint,
        )

    # ------------------------------------------------------------------
    # Manual pause / resume
    # ------------------------------------------------------------------

    def pause_group(self, group: str):
        """Manually pause a group. Pending jobs stay queued."""
        if group not in self._patterns:
            return
        self.pause_key(group)

    def resume_group(self, group: str):
        """Resume a paused group + clear cooldown / consecutive-429."""
        if group not in self._patterns:
            return
        self.resume_key(group)

    # ------------------------------------------------------------------
    # Runtime config setters (per-group)
    # ------------------------------------------------------------------

    def set_max_inflight(self, group: str, value: int):
        with self._lock:
            if group not in self._patterns:
                logger.warning(
                    "[CustomGroupLimiter] set_max_inflight: unknown group %r ignored",
                    group,
                )
                return
            self._max_inflight[group] = max(1, int(value))

    def set_min_interval(self, group: str, value: float):
        with self._lock:
            if group not in self._patterns:
                logger.warning(
                    "[CustomGroupLimiter] set_min_interval: unknown group %r ignored",
                    group,
                )
                return
            self._min_interval[group] = max(0.0, float(value))

    def set_exhaust_cooldown(self, group: str, value: float):
        with self._lock:
            if group not in self._patterns:
                logger.warning(
                    "[CustomGroupLimiter] set_exhaust_cooldown: unknown group %r ignored",
                    group,
                )
                return
            self._exhaust_cooldown[group] = max(0.0, float(value))

    # ------------------------------------------------------------------
    # Status reporting
    # ------------------------------------------------------------------

    def get_config(self) -> dict:
        """Return current per-group config in the wire-format shape."""
        with self._lock:
            groups = {}
            for name in self._groups:
                groups[name] = {
                    "endpoints": list(self._patterns[name]),
                    "max_inflight": self._max_inflight[name],
                    "min_interval": self._min_interval[name],
                    "exhaust_cooldown": self._exhaust_cooldown[name],
                }
            return {"custom_groups": groups}

    def get_all_status(self) -> dict:
        """Return runtime status for every configured group."""
        with self._lock:
            now = time.monotonic()
            result = {}
            for name in self._groups:
                exhaust_at = self._exhaust_time.get(name, 0.0)
                cooldown = self._exhaust_cooldown.get(
                    name, HARDCODED_DEFAULT_EXHAUST_COOLDOWN
                )
                cooldown_remaining = (
                    max(0.0, cooldown - (now - exhaust_at)) if exhaust_at > 0 else 0.0
                )

                entry: dict = {
                    "endpoints": list(self._patterns[name]),
                    "paused": name in self._paused,
                    "inflight": self._inflight.get(name, 0),
                    "max_inflight": self._max_inflight[name],
                    "min_interval": self._min_interval[name],
                    "exhaust_cooldown": cooldown,
                    "consecutive_429": self._consecutive_429.get(name, 0),
                    "cooldown_remaining_s": round(cooldown_remaining, 1),
                }
                reason = self._pause_reason.get(name)
                if reason:
                    entry["pause_reason"] = reason
                result[name] = entry
            return result
