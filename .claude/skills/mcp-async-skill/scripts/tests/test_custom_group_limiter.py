# -*- coding: utf-8 -*-
"""Tests for ``CustomGroupLimiter`` (PR4 / #60).

Covers:

* Glob-based endpoint matching, including the first-match-wins
  ordering rule (relies on Python 3.7+ dict insertion order).
* Per-group isolation of inflight / min_interval / exhaust_cooldown
  values.
* Unknown groups return ``False`` from both ``can_submit`` and
  ``acquire_inflight`` and create no inflight state — same contract
  CategoryLimiter follows (PHASE1_PLAN_v3 fix #1).
* The ``_match_cache`` is thread-safe (PHASE1_PLAN_v3 fix #13). Many
  threads hammering ``extract_group`` produce results consistent with
  a single-threaded baseline.
* Empty / malformed groups configuration produces a usable no-op
  limiter rather than crashing.
* Public API parity with CategoryLimiter where it matters: getters,
  setters, pause/resume.
"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue.custom_group_limiter import (  # noqa: E402
    CustomGroupLimiter,
    HARDCODED_DEFAULT_EXHAUST_COOLDOWN,
    HARDCODED_DEFAULT_MAX_INFLIGHT,
    HARDCODED_DEFAULT_MIN_INTERVAL,
)


def _make_limiter(**groups) -> CustomGroupLimiter:
    """Build a CustomGroupLimiter from kwargs to keep tests compact."""
    return CustomGroupLimiter(groups)


# ---------------------------------------------------------------------
# Construction edge cases
# ---------------------------------------------------------------------


class TestEmptyConfiguration(unittest.TestCase):
    """A bare CustomGroupLimiter is a no-op — useful when no
    ``custom_groups`` block is present in queue_config."""

    def test_empty_constructor_returns_no_groups(self):
        gl = CustomGroupLimiter()
        self.assertEqual(gl.get_groups(), [])

    def test_empty_constructor_extracts_no_group(self):
        gl = CustomGroupLimiter()
        self.assertIsNone(gl.extract_group("https://kamui-code.ai/t2v/fal/veo3"))

    def test_empty_constructor_does_not_block_submits(self):
        gl = CustomGroupLimiter()
        # extract_group returns None → dispatcher skips the limiter,
        # so can_submit / acquire_inflight on None must be the same
        # contract CategoryLimiter has.
        self.assertTrue(gl.can_submit(None))
        self.assertFalse(gl.acquire_inflight(None))


class TestMalformedConfigurationIsTolerant(unittest.TestCase):
    """Malformed entries are logged and skipped rather than crashing
    worker startup."""

    def test_non_dict_spec_is_skipped(self):
        gl = CustomGroupLimiter({"bad": "not a dict", "good": {
            "endpoints": ["*"],
            "max_inflight": 1,
        }})
        self.assertEqual(gl.get_groups(), ["good"])

    def test_missing_endpoints_is_skipped(self):
        gl = CustomGroupLimiter({"bad": {"max_inflight": 1}})
        self.assertEqual(gl.get_groups(), [])

    def test_empty_endpoints_list_is_skipped(self):
        gl = CustomGroupLimiter({"bad": {"endpoints": [], "max_inflight": 1}})
        self.assertEqual(gl.get_groups(), [])

    def test_endpoints_string_instead_of_list_is_skipped(self):
        # A user might mistakenly write a string. Don't accept it
        # (str is iterable so we don't want to match character-by-char).
        gl = CustomGroupLimiter({"bad": {"endpoints": "*", "max_inflight": 1}})
        self.assertEqual(gl.get_groups(), [])

    def test_custom_groups_wrapper_accepted(self):
        """Both raw groups dict and a {"custom_groups": ...} wrapper
        are accepted, since QueueConfig forwards the latter shape."""
        gl = CustomGroupLimiter({"custom_groups": {
            "g1": {"endpoints": ["*"], "max_inflight": 1},
        }})
        self.assertEqual(gl.get_groups(), ["g1"])


# ---------------------------------------------------------------------
# Glob matching
# ---------------------------------------------------------------------


class TestExtractGroupMatching(unittest.TestCase):
    def test_simple_glob_matches(self):
        gl = _make_limiter(**{
            "premium-video": {
                "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
                "max_inflight": 1,
            },
        })
        self.assertEqual(
            gl.extract_group("https://kamui-code.ai/t2v/fal/veo3-pro"),
            "premium-video",
        )
        self.assertEqual(
            gl.extract_group("https://kamui-code.ai/t2v/fal/veo3-fast"),
            "premium-video",
        )

    def test_no_match_returns_none(self):
        gl = _make_limiter(**{
            "premium-video": {
                "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
                "max_inflight": 1,
            },
        })
        self.assertIsNone(
            gl.extract_group("https://kamui-code.ai/t2i/fal/flux-lora"),
        )

    def test_first_match_wins_in_declaration_order(self):
        """When two groups would match, the first declared wins."""
        gl = _make_limiter(**{
            "specific": {
                "endpoints": ["https://kamui-code.ai/t2v/fal/veo3-pro"],
                "max_inflight": 1,
            },
            "broad": {
                "endpoints": ["https://kamui-code.ai/t2v/*"],
                "max_inflight": 5,
            },
        })
        # The specific pattern is declared first → it wins
        self.assertEqual(
            gl.extract_group("https://kamui-code.ai/t2v/fal/veo3-pro"),
            "specific",
        )

    def test_case_sensitive(self):
        """fnmatchcase is case-sensitive — URLs are case-sensitive in
        practice for these endpoints."""
        gl = _make_limiter(**{
            "g": {"endpoints": ["*/Veo*"], "max_inflight": 1},
        })
        self.assertIsNone(gl.extract_group("https://x/y/veo"))
        self.assertEqual(gl.extract_group("https://x/y/Veo3"), "g")

    def test_match_cache_reuses_results(self):
        gl = _make_limiter(**{
            "g1": {"endpoints": ["*/veo*"], "max_inflight": 1},
        })
        ep = "https://kamui-code.ai/t2v/fal/veo3"
        first = gl.extract_group(ep)
        # Second call must return the same result without re-running
        # the pattern loop. We can't observe that directly, but we can
        # at least confirm the cache holds a value.
        with gl._lock:
            self.assertIn(ep, gl._match_cache)
        self.assertEqual(gl.extract_group(ep), first)

    def test_match_cache_caches_misses_too(self):
        gl = _make_limiter(**{
            "g1": {"endpoints": ["*/veo*"], "max_inflight": 1},
        })
        ep_no_match = "https://kamui-code.ai/t2i/fal/flux-lora"
        self.assertIsNone(gl.extract_group(ep_no_match))
        with gl._lock:
            # Cache must record None, not be absent — otherwise we'd
            # re-walk the pattern list every time for non-matching
            # endpoints.
            self.assertIsNone(gl._match_cache[ep_no_match])


class TestExtractGroupConcurrentSmoke(unittest.TestCase):
    """PHASE1_PLAN_v3 fix #13: ``_match_cache`` reads + writes happen
    under ``self._lock``, so a single-threaded baseline and a racing
    multi-threaded run must produce the same results.

    This is a smoke test, not a stress test — its job is to catch a
    regression that drops the lock, not to prove thread safety
    rigorously."""

    def test_extract_group_concurrent_smoke(self):
        gl = _make_limiter(**{
            "g_video": {
                "endpoints": ["*/veo*", "*/sora*"],
                "max_inflight": 1,
            },
            "g_image": {
                "endpoints": ["*/seedream*"],
                "max_inflight": 1,
            },
        })
        endpoints = [
            "https://kamui-code.ai/t2v/fal/veo3-pro",
            "https://kamui-code.ai/t2v/fal/sora-1",
            "https://kamui-code.ai/t2i/fal/seedream-v4",
            "https://kamui-code.ai/t2i/fal/unknown-model",
        ] * 200

        # Baseline (single-threaded)
        baseline = [gl.extract_group(ep) for ep in endpoints]

        # Re-create so the cache starts empty
        gl2 = _make_limiter(**{
            "g_video": {
                "endpoints": ["*/veo*", "*/sora*"],
                "max_inflight": 1,
            },
            "g_image": {
                "endpoints": ["*/seedream*"],
                "max_inflight": 1,
            },
        })

        results: list = []
        results_lock = threading.Lock()

        def worker():
            local: list = []
            for ep in endpoints:
                local.append(gl2.extract_group(ep))
            with results_lock:
                results.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]

        # Every concurrent result must be one of the baseline outcomes
        # for its endpoint. We don't assert ordering (threads append
        # independently), only that no thread saw a corrupted answer.
        baseline_set = set(baseline)
        self.assertTrue(set(results).issubset(baseline_set))


# ---------------------------------------------------------------------
# Submit gating
# ---------------------------------------------------------------------


class TestPerGroupIsolation(unittest.TestCase):
    def test_inflight_per_group_does_not_leak(self):
        gl = _make_limiter(**{
            "g_a": {"endpoints": ["*"], "max_inflight": 2},
            "g_b": {"endpoints": ["*"], "max_inflight": 3},
        })
        # Saturate g_a
        self.assertTrue(gl.acquire_inflight("g_a"))
        self.assertTrue(gl.acquire_inflight("g_a"))
        self.assertFalse(gl.acquire_inflight("g_a"))  # full
        # g_b unaffected
        self.assertTrue(gl.acquire_inflight("g_b"))
        self.assertTrue(gl.acquire_inflight("g_b"))
        self.assertTrue(gl.acquire_inflight("g_b"))
        self.assertFalse(gl.acquire_inflight("g_b"))


class TestUnknownGroupRejection(unittest.TestCase):
    """PHASE1_PLAN_v3 fix #1: parity with CategoryLimiter — unknown
    keys return False from both can_submit and acquire_inflight, and
    no inflight state is created."""

    def test_unknown_group_returns_false_from_can_submit(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        self.assertFalse(gl.can_submit("nope"))

    def test_unknown_group_returns_false_from_acquire_inflight(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        self.assertFalse(gl.acquire_inflight("nope"))
        # State must not be created
        with gl._lock:
            self.assertNotIn("nope", gl._inflight)

    def test_unknown_group_release_inflight_is_noop(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        # No exception, no state change
        gl.release_inflight("nope", success=True)


class TestNoneKeyHandling(unittest.TestCase):
    """``key=None`` (= category-less and unmatched-by-group) must let
    the dispatcher pass through. CategoryLimiter behaves the same."""

    def test_none_key_can_submit_returns_true(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        self.assertTrue(gl.can_submit(None))

    def test_none_key_acquire_inflight_returns_false(self):
        # No accounting state for None
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        self.assertFalse(gl.acquire_inflight(None))


# ---------------------------------------------------------------------
# Hardcoded fallback
# ---------------------------------------------------------------------


class TestHardcodedFallback(unittest.TestCase):
    """When a group spec omits a key, the limiter falls back to module
    defaults — same safety net as CategoryLimiter."""

    def test_missing_max_inflight_uses_default(self):
        gl = _make_limiter(**{
            "g": {"endpoints": ["*"]},  # max_inflight intentionally missing
        })
        self.assertEqual(gl.get_max_inflight("g"), HARDCODED_DEFAULT_MAX_INFLIGHT)

    def test_missing_min_interval_uses_default(self):
        gl = _make_limiter(**{
            "g": {"endpoints": ["*"], "max_inflight": 5},
        })
        self.assertEqual(gl.get_min_interval("g"), HARDCODED_DEFAULT_MIN_INTERVAL)

    def test_missing_exhaust_cooldown_uses_default(self):
        gl = _make_limiter(**{
            "g": {"endpoints": ["*"], "max_inflight": 5},
        })
        self.assertEqual(
            gl.get_exhaust_cooldown("g"), HARDCODED_DEFAULT_EXHAUST_COOLDOWN,
        )

    def test_unknown_group_getters_return_hardcoded_defaults(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 9}})
        self.assertEqual(
            gl.get_max_inflight("nope"), HARDCODED_DEFAULT_MAX_INFLIGHT,
        )
        self.assertEqual(
            gl.get_min_interval("nope"), HARDCODED_DEFAULT_MIN_INTERVAL,
        )
        self.assertEqual(
            gl.get_exhaust_cooldown("nope"), HARDCODED_DEFAULT_EXHAUST_COOLDOWN,
        )


# ---------------------------------------------------------------------
# Pause / resume / cooldown
# ---------------------------------------------------------------------


class TestPauseAndCooldown(unittest.TestCase):
    def test_pause_blocks_submit(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        gl.pause_group("g")
        self.assertFalse(gl.can_submit("g"))
        self.assertTrue(gl.is_paused("g"))

    def test_resume_unblocks_submit(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        gl.pause_group("g")
        gl.resume_group("g")
        self.assertTrue(gl.can_submit("g"))
        self.assertFalse(gl.is_paused("g"))

    def test_pause_unknown_group_is_noop(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        gl.pause_group("nope")
        self.assertFalse(gl.is_paused("nope"))

    def test_force_cooldown_blocks_submit(self):
        gl = _make_limiter(**{
            "g": {"endpoints": ["*"], "max_inflight": 1, "exhaust_cooldown": 1800},
        })
        gl.force_cooldown("g")
        # Still in cooldown — can_submit returns False
        self.assertFalse(gl.can_submit("g"))
        # And get_all_status reports remaining time
        status = gl.get_all_status()
        self.assertGreater(status["g"]["cooldown_remaining_s"], 1700)

    def test_resume_clears_cooldown(self):
        gl = _make_limiter(**{
            "g": {"endpoints": ["*"], "max_inflight": 1, "exhaust_cooldown": 1800},
        })
        gl.force_cooldown("g")
        gl.resume_group("g")
        self.assertTrue(gl.can_submit("g"))


# ---------------------------------------------------------------------
# Setters
# ---------------------------------------------------------------------


class TestRuntimeSetters(unittest.TestCase):
    def test_set_max_inflight_updates_only_target(self):
        gl = _make_limiter(**{
            "g_a": {"endpoints": ["*"], "max_inflight": 1},
            "g_b": {"endpoints": ["*"], "max_inflight": 1},
        })
        gl.set_max_inflight("g_a", 7)
        self.assertEqual(gl.get_max_inflight("g_a"), 7)
        self.assertEqual(gl.get_max_inflight("g_b"), 1)

    def test_set_unknown_group_warns_and_is_ignored(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        with self.assertLogs("job_queue.custom_group_limiter", level="WARNING") as cm:
            gl.set_max_inflight("nope", 99)
        self.assertTrue(any("unknown group" in m for m in cm.output))


# ---------------------------------------------------------------------
# Status reporting
# ---------------------------------------------------------------------


class TestStatusReporting(unittest.TestCase):
    def test_get_all_status_includes_endpoints_and_limits(self):
        gl = _make_limiter(**{
            "g": {
                "endpoints": ["*/veo*"],
                "max_inflight": 1,
                "min_interval": 30,
                "exhaust_cooldown": 7200,
            },
        })
        status = gl.get_all_status()["g"]
        self.assertEqual(status["endpoints"], ["*/veo*"])
        self.assertEqual(status["max_inflight"], 1)
        self.assertEqual(status["min_interval"], 30)
        self.assertEqual(status["exhaust_cooldown"], 7200)
        self.assertEqual(status["paused"], False)
        self.assertEqual(status["inflight"], 0)

    def test_get_config_round_trips_constructor(self):
        gl = _make_limiter(**{
            "g": {
                "endpoints": ["*/veo*"],
                "max_inflight": 1,
                "min_interval": 30,
                "exhaust_cooldown": 7200,
            },
        })
        cfg = gl.get_config()["custom_groups"]["g"]
        self.assertEqual(cfg["endpoints"], ["*/veo*"])
        self.assertEqual(cfg["max_inflight"], 1)
        self.assertEqual(cfg["min_interval"], 30)
        self.assertEqual(cfg["exhaust_cooldown"], 7200)


class TestTouchSubmitUnknownGuard(unittest.TestCase):
    """``touch_submit`` must respect the same "unknown keys create no
    state" contract as ``can_submit`` / ``acquire_inflight``.

    Mirror of CategoryLimiter's same-named test class. Without the
    subclass guard the LimiterStateMixin would happily insert an
    entry into ``_last_submit`` for any key, growing the dict
    unboundedly if a caller fed it raw endpoint URLs by mistake.
    """

    def test_touch_submit_unknown_creates_no_state(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        gl.touch_submit("nope")
        self.assertNotIn("nope", gl._last_submit)

    def test_touch_submit_none_is_noop(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        gl.touch_submit(None)
        self.assertEqual(gl._last_submit, {})

    def test_touch_submit_known_records_timestamp(self):
        gl = _make_limiter(**{"g": {"endpoints": ["*"], "max_inflight": 1}})
        gl.touch_submit("g")
        self.assertIn("g", gl._last_submit)
        self.assertGreater(gl._last_submit["g"], 0)


if __name__ == "__main__":
    unittest.main()
