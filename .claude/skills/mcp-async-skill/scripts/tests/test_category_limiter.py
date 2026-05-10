# -*- coding: utf-8 -*-
"""Tests for ``CategoryLimiter`` per-category rate limiting (#59).

Covers:

* New ``limits.{cat}.{key}`` schema and per-category isolation.
* Hardcoded fallback when a category or key is missing from the config.
* Legacy flat schema (``max_category_inflight`` / ``min_interval`` /
  ``exhaust_cooldown``) — fanned out to all configured categories with
  a one-shot deprecation warning per ``CategoryLimiter`` instance.
* Unknown category: ``can_submit`` and ``acquire_inflight`` both return
  ``False`` and create no inflight state. The two getters agree on
  unknown keys so the dispatcher sees a consistent answer.
* Concurrency: ``acquire_inflight`` never overshoots ``max_inflight``
  even under heavy thread contention.
* ``get_all_status`` reflects per-category cooldown / max_inflight values.
* Public API: ``is_known_category``, ``get_categories``, and the
  required ``cat`` argument on ``set_*`` setters.

Spec context: per-category isolation lets the dispatcher mirror the
upstream MCP service's per-category rolling-window rate limits.
``set_max_inflight(cat, value)`` is the supported runtime mutation; the
single-arg legacy form is gone — the worker's PATCH handler emulates
"apply to all" at the API layer instead.
"""
import logging
import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue.category_limiter import (  # noqa: E402
    CategoryLimiter,
    HARDCODED_DEFAULT_EXHAUST_COOLDOWN,
    HARDCODED_DEFAULT_MAX_INFLIGHT,
    HARDCODED_DEFAULT_MIN_INTERVAL,
    KNOWN_CATEGORIES,
)


def _make_limiter(**overrides) -> CategoryLimiter:
    """Build a CategoryLimiter with sane per-category defaults for tests.

    Each test can override individual categories via ``overrides``.
    """
    base_limits = {
        "t2i": {"max_inflight": 3, "min_interval": 0.0, "exhaust_cooldown": 60},
        "i2i": {"max_inflight": 2, "min_interval": 0.0, "exhaust_cooldown": 120},
        "t2v": {"max_inflight": 1, "min_interval": 0.0, "exhaust_cooldown": 1800},
        "i2v": {"max_inflight": 1, "min_interval": 0.0, "exhaust_cooldown": 3600},
    }
    for cat, vals in overrides.items():
        base_limits.setdefault(cat, {}).update(vals)

    return CategoryLimiter({
        "categories": list(base_limits.keys()),
        "limits": base_limits,
    })


class TestPerCategoryIndependentValues(unittest.TestCase):
    """Each category's max_inflight / min_interval / exhaust_cooldown is independent."""

    def test_max_inflight_per_category(self):
        lim = _make_limiter()
        self.assertEqual(lim.get_max_inflight("t2i"), 3)
        self.assertEqual(lim.get_max_inflight("i2i"), 2)
        self.assertEqual(lim.get_max_inflight("t2v"), 1)
        self.assertEqual(lim.get_max_inflight("i2v"), 1)

    def test_exhaust_cooldown_per_category(self):
        lim = _make_limiter()
        self.assertEqual(lim.get_exhaust_cooldown("t2i"), 60)
        self.assertEqual(lim.get_exhaust_cooldown("t2v"), 1800)
        self.assertEqual(lim.get_exhaust_cooldown("i2v"), 3600)

    def test_min_interval_per_category(self):
        lim = CategoryLimiter({
            "categories": ["t2i", "t2v"],
            "limits": {
                "t2i": {"max_inflight": 1, "min_interval": 0.5, "exhaust_cooldown": 60},
                "t2v": {"max_inflight": 1, "min_interval": 5.0, "exhaust_cooldown": 60},
            },
        })
        self.assertEqual(lim.get_min_interval("t2i"), 0.5)
        self.assertEqual(lim.get_min_interval("t2v"), 5.0)

    def test_inflight_per_category_does_not_leak(self):
        """Acquiring inflight on t2i must not block i2i, even under
        contention on either category."""
        lim = _make_limiter()
        # Saturate t2i (max_inflight=3)
        self.assertTrue(lim.acquire_inflight("t2i"))
        self.assertTrue(lim.acquire_inflight("t2i"))
        self.assertTrue(lim.acquire_inflight("t2i"))
        self.assertFalse(lim.acquire_inflight("t2i"))  # full
        # i2i (max_inflight=2) is unaffected
        self.assertTrue(lim.acquire_inflight("i2i"))
        self.assertTrue(lim.acquire_inflight("i2i"))
        self.assertFalse(lim.acquire_inflight("i2i"))  # i2i now full
        # t2v (max_inflight=1) is also unaffected
        self.assertTrue(lim.acquire_inflight("t2v"))
        self.assertFalse(lim.acquire_inflight("t2v"))


class TestHardcodedFallback(unittest.TestCase):
    """Categories or keys missing from `limits` fall back to module defaults."""

    def test_missing_category_returns_hardcoded_defaults(self):
        # Configure t2i only; t2v / i2v are in `categories` but not in `limits`
        lim = CategoryLimiter({
            "categories": ["t2i", "t2v", "i2v"],
            "limits": {"t2i": {"max_inflight": 5, "min_interval": 0.5, "exhaust_cooldown": 60}},
        })
        self.assertEqual(lim.get_max_inflight("t2v"), HARDCODED_DEFAULT_MAX_INFLIGHT)
        self.assertEqual(lim.get_min_interval("t2v"), HARDCODED_DEFAULT_MIN_INTERVAL)
        self.assertEqual(lim.get_exhaust_cooldown("t2v"), HARDCODED_DEFAULT_EXHAUST_COOLDOWN)
        # The configured one is unaffected
        self.assertEqual(lim.get_max_inflight("t2i"), 5)

    def test_missing_key_within_category_falls_back(self):
        lim = CategoryLimiter({
            "categories": ["t2i"],
            # Only max_inflight is set; min_interval / exhaust_cooldown missing
            "limits": {"t2i": {"max_inflight": 7}},
        })
        self.assertEqual(lim.get_max_inflight("t2i"), 7)
        self.assertEqual(lim.get_min_interval("t2i"), HARDCODED_DEFAULT_MIN_INTERVAL)
        self.assertEqual(lim.get_exhaust_cooldown("t2i"), HARDCODED_DEFAULT_EXHAUST_COOLDOWN)


class TestLegacySchemaCompat(unittest.TestCase):
    """Legacy flat ``max_category_inflight`` etc. are fanned out to all
    configured categories, with a one-shot deprecation warning per
    CategoryLimiter instance."""

    def test_legacy_flat_values_apply_to_all_categories(self):
        lim = CategoryLimiter({
            "categories": ["t2i", "i2i", "t2v", "i2v"],
            "max_category_inflight": 4,
            "min_interval": 2.5,
            "exhaust_cooldown": 1234,
        })
        for cat in ["t2i", "i2i", "t2v", "i2v"]:
            self.assertEqual(lim.get_max_inflight(cat), 4)
            self.assertEqual(lim.get_min_interval(cat), 2.5)
            self.assertEqual(lim.get_exhaust_cooldown(cat), 1234)

    def test_new_form_wins_over_legacy_per_category(self):
        """When both shapes are present, ``limits.{cat}`` overrides the legacy
        flat value for that key only."""
        lim = CategoryLimiter({
            "categories": ["t2i", "i2i"],
            "limits": {"t2i": {"max_inflight": 9}},
            "max_category_inflight": 1,
            "exhaust_cooldown": 500,
        })
        # t2i's max_inflight overridden by new form, but exhaust_cooldown
        # falls through to legacy
        self.assertEqual(lim.get_max_inflight("t2i"), 9)
        self.assertEqual(lim.get_exhaust_cooldown("t2i"), 500)
        # i2i has no new-form override; both come from legacy
        self.assertEqual(lim.get_max_inflight("i2i"), 1)
        self.assertEqual(lim.get_exhaust_cooldown("i2i"), 500)

    def test_legacy_emits_deprecation_warning_once_per_instance(self):
        """The deprecation log fires exactly once per CategoryLimiter
        instance, never globally. Two instances → two warnings."""
        with self.assertLogs("job_queue.category_limiter", level="WARNING") as cm1:
            CategoryLimiter({
                "categories": ["t2i"],
                "max_category_inflight": 2,
            })
        deprecated_logs_1 = [m for m in cm1.output if "DEPRECATED" in m]
        self.assertEqual(len(deprecated_logs_1), 1,
                         f"Expected 1 deprecation warning, got: {cm1.output}")

        with self.assertLogs("job_queue.category_limiter", level="WARNING") as cm2:
            CategoryLimiter({
                "categories": ["t2i"],
                "max_category_inflight": 2,
            })
        deprecated_logs_2 = [m for m in cm2.output if "DEPRECATED" in m]
        self.assertEqual(len(deprecated_logs_2), 1,
                         "New instance must emit its own deprecation warning")

    def test_new_only_schema_does_not_warn(self):
        """No legacy keys → no deprecation warning."""
        # assertNoLogs requires Python 3.10+, fall back to manual check.
        logger = logging.getLogger("job_queue.category_limiter")
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture(level=logging.WARNING)
        logger.addHandler(handler)
        try:
            CategoryLimiter({
                "categories": ["t2i"],
                "limits": {"t2i": {"max_inflight": 2, "min_interval": 0.5,
                                   "exhaust_cooldown": 60}},
            })
        finally:
            logger.removeHandler(handler)

        self.assertFalse(
            any("DEPRECATED" in r.getMessage() for r in records),
            "New-only schema must not emit deprecation warning",
        )


class TestUnknownCategoryFalse(unittest.TestCase):
    """Unknown category: can_submit and acquire_inflight both return False
    and create no inflight state (PR #59 fix #1)."""

    def test_acquire_inflight_unknown_returns_false(self):
        lim = _make_limiter()
        self.assertFalse(lim.acquire_inflight("unknown_cat"))
        # No state created
        self.assertNotIn("unknown_cat", lim._inflight)

    def test_can_submit_unknown_returns_false(self):
        lim = _make_limiter()
        self.assertFalse(lim.can_submit("unknown_cat"))

    def test_can_submit_and_acquire_inflight_consistent_for_unknown(self):
        """Both must agree on unknown — dispatcher relies on this."""
        lim = _make_limiter()
        self.assertEqual(
            lim.can_submit("unknown_cat"),
            lim.acquire_inflight("unknown_cat"),
        )

    def test_release_inflight_unknown_is_noop(self):
        """Releasing inflight on an unknown category does nothing
        (acquire returned False, so there's nothing to release)."""
        lim = _make_limiter()
        # Should not raise; should not create state
        lim.release_inflight("unknown_cat", success=True)
        self.assertNotIn("unknown_cat", lim._inflight)

    def test_force_cooldown_unknown_is_noop(self):
        lim = _make_limiter()
        lim.force_cooldown("unknown_cat")
        self.assertNotIn("unknown_cat", lim._exhaust_time)

    def test_pause_with_reason_unknown_is_noop(self):
        lim = _make_limiter()
        lim.pause_with_reason("unknown_cat", "test", status_code=500)
        self.assertFalse(lim.is_paused("unknown_cat"))

    def test_unknown_category_getters_return_hardcoded_defaults(self):
        lim = _make_limiter()
        self.assertEqual(lim.get_max_inflight("unknown"),
                         HARDCODED_DEFAULT_MAX_INFLIGHT)
        self.assertEqual(lim.get_min_interval("unknown"),
                         HARDCODED_DEFAULT_MIN_INTERVAL)
        self.assertEqual(lim.get_exhaust_cooldown("unknown"),
                         HARDCODED_DEFAULT_EXHAUST_COOLDOWN)


class TestNoneCategory(unittest.TestCase):
    """Category=None means "endpoint without a recognised category" — the
    dispatcher passes it through, and the limiter must not block it
    nor create state."""

    def test_can_submit_none_returns_true(self):
        # category-less endpoints should be allowed to dispatch
        lim = _make_limiter()
        self.assertTrue(lim.can_submit(None))

    def test_acquire_inflight_none_returns_false(self):
        # but no inflight slot is created for category=None
        lim = _make_limiter()
        self.assertFalse(lim.acquire_inflight(None))

    def test_is_paused_none_returns_false(self):
        lim = _make_limiter()
        self.assertFalse(lim.is_paused(None))


class TestConcurrentAcquireInflight(unittest.TestCase):
    """``acquire_inflight`` must not overshoot ``max_inflight`` even under
    heavy thread contention."""

    def test_concurrent_acquire_does_not_exceed_max_inflight(self):
        lim = CategoryLimiter({
            "categories": ["t2i"],
            "limits": {"t2i": {"max_inflight": 5, "min_interval": 0.0,
                               "exhaust_cooldown": 60}},
        })

        N_THREADS = 50
        successes: list[bool] = []
        successes_lock = threading.Lock()

        def worker():
            ok = lim.acquire_inflight("t2i")
            with successes_lock:
                successes.append(ok)

        threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Exactly max_inflight (5) acquires should have succeeded.
        n_acquired = sum(1 for s in successes if s)
        self.assertEqual(n_acquired, 5,
                         f"Expected exactly 5 acquires, got {n_acquired}")
        # And the internal counter must agree
        self.assertEqual(lim._inflight.get("t2i"), 5)


class TestPerCategoryCooldownInGetAllStatus(unittest.TestCase):
    """``get_all_status`` reflects per-category cooldown values, not a
    single shared scalar."""

    def test_status_reports_per_category_cooldown_and_max_inflight(self):
        lim = _make_limiter()
        status = lim.get_all_status()

        self.assertEqual(status["t2i"]["max_inflight"], 3)
        self.assertEqual(status["i2i"]["max_inflight"], 2)
        self.assertEqual(status["t2v"]["max_inflight"], 1)

        self.assertEqual(status["t2i"]["exhaust_cooldown"], 60)
        self.assertEqual(status["t2v"]["exhaust_cooldown"], 1800)
        self.assertEqual(status["i2v"]["exhaust_cooldown"], 3600)

    def test_cooldown_remaining_uses_per_category_cooldown(self):
        """After ``force_cooldown('t2v')``, only t2v shows non-zero
        remaining cooldown, and it's bounded by t2v's per-category value."""
        lim = _make_limiter()
        lim.force_cooldown("t2v")
        status = lim.get_all_status()

        # t2v has cooldown=1800 → remaining ≈ 1800
        self.assertGreater(status["t2v"]["cooldown_remaining_s"], 1700)
        self.assertLessEqual(status["t2v"]["cooldown_remaining_s"], 1800)
        # Other categories untouched
        self.assertEqual(status["t2i"]["cooldown_remaining_s"], 0)
        self.assertEqual(status["i2i"]["cooldown_remaining_s"], 0)
        self.assertEqual(status["i2v"]["cooldown_remaining_s"], 0)


class TestPublicCategoryAPI(unittest.TestCase):
    """``is_known_category`` and ``get_categories`` replace the old habit of
    poking ``_categories`` directly from outside the class."""

    def test_is_known_category(self):
        lim = _make_limiter()
        self.assertTrue(lim.is_known_category("t2i"))
        self.assertTrue(lim.is_known_category("t2v"))
        self.assertFalse(lim.is_known_category("unknown"))
        self.assertFalse(lim.is_known_category(None))

    def test_get_categories_returns_sorted_deterministic(self):
        lim = _make_limiter()
        cats = lim.get_categories()
        self.assertEqual(cats, ["i2i", "i2v", "t2i", "t2v"])
        # Mutating the returned list must not affect internal state
        cats.append("foo")
        self.assertEqual(lim.get_categories(), ["i2i", "i2v", "t2i", "t2v"])


class TestSetterCategoryRequired(unittest.TestCase):
    """``set_max_inflight`` / ``set_min_interval`` / ``set_exhaust_cooldown``
    take a category argument (breaking change vs lazy-v2.10.x)."""

    def test_set_max_inflight_updates_only_target_category(self):
        lim = _make_limiter()
        lim.set_max_inflight("t2v", 7)
        self.assertEqual(lim.get_max_inflight("t2v"), 7)
        # Untouched
        self.assertEqual(lim.get_max_inflight("t2i"), 3)
        self.assertEqual(lim.get_max_inflight("i2v"), 1)

    def test_set_min_interval_updates_only_target_category(self):
        lim = _make_limiter()
        lim.set_min_interval("t2i", 4.0)
        self.assertEqual(lim.get_min_interval("t2i"), 4.0)
        self.assertEqual(lim.get_min_interval("t2v"), 0.0)

    def test_set_exhaust_cooldown_updates_only_target_category(self):
        lim = _make_limiter()
        lim.set_exhaust_cooldown("i2v", 7200)
        self.assertEqual(lim.get_exhaust_cooldown("i2v"), 7200)
        self.assertEqual(lim.get_exhaust_cooldown("t2v"), 1800)

    def test_set_unknown_category_warns_and_is_ignored(self):
        lim = _make_limiter()
        with self.assertLogs("job_queue.category_limiter", level="WARNING") as cm:
            lim.set_max_inflight("foobar", 99)
        self.assertTrue(any("unknown category" in m for m in cm.output))
        # State unchanged
        self.assertNotIn("foobar", lim._max_inflight)


class TestKnownCategoriesConstant(unittest.TestCase):
    """Sanity check on the module-level KNOWN_CATEGORIES constant."""

    def test_known_categories_contains_t2i_i2i_t2v_i2v(self):
        self.assertEqual(KNOWN_CATEGORIES, {"t2i", "i2i", "t2v", "i2v"})

    def test_default_categories_when_no_config_given(self):
        """A bare ``CategoryLimiter()`` falls back to KNOWN_CATEGORIES."""
        lim = CategoryLimiter()
        self.assertEqual(set(lim.get_categories()), KNOWN_CATEGORIES)


class TestExtractCategory(unittest.TestCase):
    """``extract_category`` derives the canonical category from an MCP
    endpoint URL, applying alias resolution (r2i → i2i, r2v → i2v)."""

    def test_extracts_t2i(self):
        lim = _make_limiter()
        self.assertEqual(
            lim.extract_category("https://kamui-code.ai/t2i/fal/flux-lora"),
            "t2i",
        )

    def test_extracts_t2v(self):
        lim = _make_limiter()
        self.assertEqual(
            lim.extract_category("https://kamui-code.ai/t2v/fal/veo3"),
            "t2v",
        )

    def test_aliases_r2i_to_i2i(self):
        lim = _make_limiter()
        self.assertEqual(
            lim.extract_category("https://kamui-code.ai/r2i/fal/refer"),
            "i2i",
        )

    def test_aliases_r2v_to_i2v(self):
        lim = _make_limiter()
        self.assertEqual(
            lim.extract_category("https://kamui-code.ai/r2v/fal/refer-video"),
            "i2v",
        )

    def test_unknown_path_returns_none(self):
        lim = _make_limiter()
        self.assertIsNone(
            lim.extract_category("https://example.com/foobar/something"),
        )

    def test_empty_path_returns_none(self):
        lim = _make_limiter()
        self.assertIsNone(lim.extract_category("https://example.com"))


class TestCooldownExpiredLogOnce(unittest.TestCase):
    """``can_submit`` logs ``Cooldown expired for X, resuming`` exactly
    on the transition from "still in cooldown" to "first call after
    expiry". Without that one-shot guarantee a busy dispatcher would
    flood the log on every single subsequent dispatch round.

    PR4 review (Codex): the previous condition
    ``category not in self._exhaust_time and consecutive_429 > 0``
    stayed true across many later calls because nothing cleared
    ``consecutive_429`` until ``record_success`` ran. The fix is to
    snapshot ``had_active_cooldown`` BEFORE the helper call and only
    log on the active → expired transition.
    """

    def test_cooldown_expired_log_fires_at_most_once(self):
        # max_inflight=10 so the cooldown is the only gating factor
        lim = CategoryLimiter({
            "categories": ["t2i"],
            "limits": {"t2i": {"max_inflight": 10, "min_interval": 0.0,
                                "exhaust_cooldown": 0.05}},
        })
        # Pretend a 429 just hit
        lim.force_cooldown("t2i")
        lim.record_429("t2i")

        # Wait for the cooldown to expire
        import time as _time
        _time.sleep(0.06)

        with self.assertLogs("job_queue.category_limiter", level="INFO") as cm1:
            # First call after expiry: must log exactly once
            self.assertTrue(lim.can_submit("t2i"))
        expired_logs = [m for m in cm1.output if "Cooldown expired" in m]
        self.assertEqual(len(expired_logs), 1,
                         f"Expected exactly 1 expired log, got: {cm1.output}")

        # Subsequent calls must NOT re-log even though
        # _consecutive_429 is still non-zero (record_success has not
        # run yet because no real submit happened).
        # assertNoLogs is 3.10+; capture and check manually.
        import logging as _logging
        category_logger = _logging.getLogger("job_queue.category_limiter")
        records: list = []

        class _Capture(_logging.Handler):
            def emit(self, record):
                records.append(record)

        handler = _Capture(level=_logging.INFO)
        category_logger.addHandler(handler)
        try:
            for _ in range(5):
                lim.can_submit("t2i")
        finally:
            category_logger.removeHandler(handler)

        self.assertFalse(
            any("Cooldown expired" in r.getMessage() for r in records),
            f"Cooldown expired log must fire only on transition; got "
            f"{[r.getMessage() for r in records]}",
        )


class TestTouchSubmitUnknownGuard(unittest.TestCase):
    """``touch_submit`` must respect the same "unknown keys create no
    state" contract as ``can_submit`` / ``acquire_inflight``.

    PR4 review (Codex): the LimiterStateMixin's bare ``touch_submit``
    inserts into ``_last_submit`` for any key, so an unknown category
    fed by mistake (e.g. raw endpoint URL) would slowly grow the
    dict. CategoryLimiter overrides to guard against that.
    """

    def test_touch_submit_unknown_creates_no_state(self):
        lim = CategoryLimiter({
            "categories": ["t2i"],
            "limits": {"t2i": {"max_inflight": 1}},
        })
        lim.touch_submit("unknown")
        self.assertNotIn("unknown", lim._last_submit)

    def test_touch_submit_none_is_noop(self):
        lim = CategoryLimiter({
            "categories": ["t2i"],
            "limits": {"t2i": {"max_inflight": 1}},
        })
        lim.touch_submit(None)
        # No exception, no entry created
        self.assertEqual(lim._last_submit, {})

    def test_touch_submit_known_records_timestamp(self):
        lim = CategoryLimiter({
            "categories": ["t2i"],
            "limits": {"t2i": {"max_inflight": 1}},
        })
        lim.touch_submit("t2i")
        self.assertIn("t2i", lim._last_submit)
        self.assertGreater(lim._last_submit["t2i"], 0)


if __name__ == "__main__":
    unittest.main()
