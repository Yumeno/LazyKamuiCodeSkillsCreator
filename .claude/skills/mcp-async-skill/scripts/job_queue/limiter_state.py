# -*- coding: utf-8 -*-
"""Shared inflight / cooldown / pause / 429-counter state for rate limiters.

Used by both :class:`~job_queue.category_limiter.CategoryLimiter`
(``key=category``, e.g. ``"t2v"``) and
:class:`~job_queue.custom_group_limiter.CustomGroupLimiter`
(``key=group_name``, e.g. ``"premium-video"``).

Centralising these primitives means a bug fix or new behaviour (e.g.
exponential backoff on consecutive 429s) only needs to be written
once, and the "unknown key is out of scope" rule that PR1 (#59)
established for CategoryLimiter applies uniformly to CustomGroupLimiter
too.

Design contract
---------------

The mixin owns the following per-key state:

* ``_inflight``: ``dict[str, int]`` — current concurrent submits per key
* ``_exhaust_time``: ``dict[str, float]`` — ``time.monotonic()`` when a
  429 cooldown started for the key (rolling window)
* ``_paused``: ``set[str]`` — keys explicitly paused (manual or after
  a non-429 error)
* ``_pause_reason``: ``dict[str, dict]`` — diagnostic info for paused keys
* ``_consecutive_429``: ``dict[str, int]`` — informational counter
* ``_last_submit``: ``dict[str, float]`` — for ``min_interval`` enforcement
* ``_lock``: ``threading.Lock`` — guards every mutation above

The mixin does NOT own per-key configured limits (``max_inflight``,
``min_interval``, ``exhaust_cooldown``). Those are subclass concerns
because the lookup keys (category vs group) and the schemas
(``limits.{cat}`` vs ``custom_groups.{name}``) differ. Subclasses
read their own per-key limit and pass the integer to
:meth:`_acquire_inflight_locked` / :meth:`_check_cooldown_locked`.

Unknown keys (PHASE1_PLAN_v3 fix #1)
------------------------------------

Subclasses are expected to reject unknown keys at the top of their
``can_submit`` / ``acquire_inflight`` overrides BEFORE calling into
the mixin. The mixin trusts that anything reaching ``_*_locked``
helpers is already a known key. This keeps unknown-key handling
(and the "create no state" rule) at the subclass boundary where the
"known set" lives.

Init helper guard (PHASE1_PLAN_v3 fix M2)
-----------------------------------------

Subclasses MUST call :meth:`_init_state` from their ``__init__``. The
mixin's mutators raise :class:`RuntimeError` if invoked before
``_init_state`` so the failure mode is loud rather than the cryptic
``AttributeError: '_lock'`` you'd otherwise get the first time a
production code path calls ``acquire_inflight``.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class LimiterStateMixin:
    """Provides the per-key inflight / cooldown / pause primitives.

    Subclass requirements:

    * Override ``__init__`` and call ``self._init_state()`` somewhere
      inside it.
    * Override ``can_submit(key)`` / ``acquire_inflight(key)`` /
      ``release_inflight(key, success)`` — those need the subclass's
      "known key set" plus its per-key configured limit lookups, which
      the mixin doesn't have.

    Mixin-provided methods (call directly on the subclass):

    * :meth:`force_cooldown` / :meth:`record_429` / :meth:`record_success`
    * :meth:`is_paused` / :meth:`pause_with_reason` / :meth:`pause_key`
      / :meth:`resume_key` / :meth:`get_pause_reason`
    * :meth:`touch_submit`
    """

    # --- Initialisation -----------------------------------------------

    def _init_state(self) -> None:
        """Allocate the per-key state dicts. Must be called from
        subclass ``__init__``."""
        self._inflight: dict[str, int] = {}
        self._exhaust_time: dict[str, float] = {}
        self._paused: set[str] = set()
        self._pause_reason: dict[str, dict] = {}
        self._consecutive_429: dict[str, int] = {}
        self._last_submit: dict[str, float] = {}
        self._lock = threading.Lock()

    def _ensure_state_initialised(self) -> None:
        """Sanity guard: raise RuntimeError if a mutator is called
        before ``_init_state``.

        This protects against a future subclass forgetting the init
        call. Without it, the first ``acquire_inflight`` would raise
        ``AttributeError: '_lock'`` at an unexpected place in the
        dispatch path, which is hard to diagnose."""
        if not hasattr(self, "_lock"):
            raise RuntimeError(
                f"{type(self).__name__} forgot to call _init_state() "
                f"in __init__. LimiterStateMixin requires explicit "
                f"state initialisation."
            )

    # --- Lock-protected primitives (caller must hold self._lock) ------

    def _acquire_inflight_locked(self, key: str, max_inflight: int) -> bool:
        """Try to bump the per-key inflight counter. Must be called
        inside ``self._lock``."""
        current = self._inflight.get(key, 0)
        if current >= max_inflight:
            return False
        self._inflight[key] = current + 1
        return True

    def _release_inflight_locked(self, key: str) -> None:
        """Decrement the per-key inflight counter, never below zero.
        Must be called inside ``self._lock``."""
        self._inflight[key] = max(0, self._inflight.get(key, 0) - 1)

    def _check_cooldown_locked(
        self, key: str, cooldown_seconds: float, now: float | None = None,
    ) -> bool:
        """Return True when the per-key 429 cooldown has expired (or
        was never set). Side effect: clears the exhaust_time entry on
        expiry so subsequent calls return True without re-evaluating.

        Must be called inside ``self._lock``."""
        exhaust_at = self._exhaust_time.get(key, 0.0)
        if exhaust_at == 0:
            return True
        if now is None:
            now = time.monotonic()
        if (now - exhaust_at) >= cooldown_seconds:
            self._exhaust_time.pop(key, None)
            return True
        return False

    # --- 429 / submit-tracking ----------------------------------------

    def force_cooldown(self, key: str) -> None:
        """Start (or restart) the rolling 429 cooldown for ``key``.

        ``key=None`` is a no-op so dispatcher code can pass through
        ``extract_*`` results unconditionally.
        """
        if key is None:
            return
        self._ensure_state_initialised()
        with self._lock:
            self._exhaust_time[key] = time.monotonic()

    def record_429(self, key: str) -> None:
        """Increment the consecutive-429 counter (informational only —
        cooldown handles the actual throttling). Logs every 10."""
        if key is None:
            return
        self._ensure_state_initialised()
        with self._lock:
            count = self._consecutive_429.get(key, 0) + 1
            self._consecutive_429[key] = count
            if count and count % 10 == 0:
                logger.info(
                    "[%s] %s has hit %d consecutive 429s "
                    "(cooldown handles retry automatically)",
                    type(self).__name__, key, count,
                )

    def record_success(self, key: str) -> None:
        """Reset the consecutive-429 counter after a successful submit."""
        if key is None:
            return
        self._ensure_state_initialised()
        with self._lock:
            self._consecutive_429.pop(key, None)

    def touch_submit(self, key: str) -> None:
        """Update the last-submit timestamp (for ``min_interval``)."""
        if key is None:
            return
        self._ensure_state_initialised()
        with self._lock:
            self._last_submit[key] = time.monotonic()

    # --- Pause / resume -----------------------------------------------

    def is_paused(self, key: str | None) -> bool:
        if key is None:
            return False
        self._ensure_state_initialised()
        with self._lock:
            return key in self._paused

    def get_pause_reason(self, key: str | None) -> dict | None:
        if key is None:
            return None
        self._ensure_state_initialised()
        with self._lock:
            r = self._pause_reason.get(key)
            return dict(r) if r else None

    def pause_with_reason(
        self,
        key: str | None,
        reason: str,
        status_code: int | None = None,
        error_detail: str = "",
        job_id: str = "",
        endpoint: str = "",
    ) -> None:
        """Pause a key due to an error. Stores diagnostic info for
        later inspection via the worker API."""
        if key is None:
            return
        self._ensure_state_initialised()
        with self._lock:
            self._paused.add(key)
            self._pause_reason[key] = {
                "reason": reason,
                "status_code": status_code,
                "error_detail": (error_detail or "")[:2000],
                "job_id": job_id,
                "endpoint": endpoint,
                "paused_at": datetime.now(timezone.utc).isoformat(),
            }
        logger.warning(
            "[%s] Paused %s: %s (HTTP %s) — %s",
            type(self).__name__, key, reason, status_code,
            (error_detail or "")[:200],
        )

    def pause_key(self, key: str) -> None:
        """Manually pause a key. Pending jobs stay queued."""
        self._ensure_state_initialised()
        with self._lock:
            self._paused.add(key)
            self._pause_reason[key] = {
                "reason": "manual",
                "paused_at": datetime.now(timezone.utc).isoformat(),
            }

    def resume_key(self, key: str) -> None:
        """Remove pause + clear consecutive-429 + cooldown for ``key``."""
        self._ensure_state_initialised()
        with self._lock:
            self._paused.discard(key)
            self._pause_reason.pop(key, None)
            self._consecutive_429.pop(key, None)
            self._exhaust_time.pop(key, None)
