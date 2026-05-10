# -*- coding: utf-8 -*-
"""Tests for dispatcher integration of CustomGroupLimiter (PR4 / #60).

Covers:

* ``_resolve_limiter()`` returns the correct limiter for an endpoint:
  custom group on match, category on category prefix match, ``(None,
  None)`` on truly unknown endpoint.
* When an endpoint matches a custom group, accounting goes to the
  group limiter and the category limiter is NOT charged. (This is the
  whole point of custom_groups — special-case throttling without
  inflating the parent category's quota.)
* When an endpoint matches no group but has a recognised category
  prefix, accounting goes to the category limiter (unchanged
  pre-PR4 behaviour).
* When an endpoint matches no group AND no category, the dispatcher
  schedules it without any per-key accounting (parity with the
  pre-PR4 ``test_dispatcher_rate_limit::TestUnknownCategoryEndpointDispatch``).
* 429 / non-429 error handling routes through the resolved limiter.
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue.db import JobStore
from job_queue.dispatcher import Dispatcher, QueueConfig


class _FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeHTTPError(Exception):
    def __init__(self, response=None):
        super().__init__(f"HTTP {response.status_code}" if response else "HTTP Error")
        self.response = response


class _ResolveLimiterBase(unittest.TestCase):
    """Builds a Dispatcher with the configured custom_groups and a
    no-op executor. Tests can call ``self.dispatcher._resolve_limiter``
    directly without going through the dispatch loop."""

    custom_groups: dict = {}

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = JobStore(self.tmp.name)
        self.config = QueueConfig.from_dict({
            "custom_groups": self.custom_groups,
            # Provide a known-category list so extract_category works
            "category_rate_limits": {
                "categories": ["t2i", "i2i", "t2v", "i2v"],
                "limits": {
                    "t2i": {"max_inflight": 3, "min_interval": 0.0,
                             "exhaust_cooldown": 60},
                    "i2i": {"max_inflight": 2, "min_interval": 0.0,
                             "exhaust_cooldown": 60},
                    "t2v": {"max_inflight": 1, "min_interval": 0.0,
                             "exhaust_cooldown": 60},
                    "i2v": {"max_inflight": 1, "min_interval": 0.0,
                             "exhaust_cooldown": 60},
                },
            },
        })
        self.executed: list[str] = []

        def recording_executor(job):
            self.executed.append(job["id"])

        self.dispatcher = Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=recording_executor,
            loop_interval=0.01,
        )

    def tearDown(self):
        try:
            self.dispatcher.stop()
        except Exception:
            pass
        self.store.close()
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------------
# _resolve_limiter() shape
# ---------------------------------------------------------------------


class TestResolveLimiterMatching(_ResolveLimiterBase):
    custom_groups = {
        "premium-video": {
            "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
            "max_inflight": 1,
        },
    }

    def test_group_match_returns_group_limiter(self):
        limiter, key = self.dispatcher._resolve_limiter(
            "https://kamui-code.ai/t2v/fal/veo3-pro",
        )
        self.assertIs(limiter, self.dispatcher.group_limiter)
        self.assertEqual(key, "premium-video")

    def test_category_match_returns_category_limiter(self):
        limiter, key = self.dispatcher._resolve_limiter(
            "https://kamui-code.ai/t2i/fal/flux-lora",
        )
        self.assertIs(limiter, self.dispatcher.category_limiter)
        self.assertEqual(key, "t2i")

    def test_unknown_endpoint_returns_none_none(self):
        limiter, key = self.dispatcher._resolve_limiter(
            "https://example.com/some/random/path",
        )
        self.assertIsNone(limiter)
        self.assertIsNone(key)

    def test_group_takes_precedence_over_category(self):
        """An endpoint that BOTH matches a group AND has a recognised
        category prefix must go to the group, not the category."""
        ep = "https://kamui-code.ai/t2v/fal/veo3-pro"
        # Sanity: the endpoint also extracts a category
        self.assertEqual(
            self.dispatcher.category_limiter.extract_category(ep), "t2v",
        )
        limiter, key = self.dispatcher._resolve_limiter(ep)
        self.assertIs(limiter, self.dispatcher.group_limiter)
        self.assertEqual(key, "premium-video")


# ---------------------------------------------------------------------
# Dispatch goes through the resolved limiter
# ---------------------------------------------------------------------


class TestGroupAccountingIsolation(_ResolveLimiterBase):
    """When an endpoint matches a group, the category limiter must not
    be charged for it. This is the headline reason custom_groups
    exists."""

    custom_groups = {
        "premium-video": {
            "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
            "max_inflight": 1,
            "min_interval": 0.0,
            "exhaust_cooldown": 60,
        },
    }

    def test_group_endpoint_creates_no_category_inflight(self):
        ep = "https://kamui-code.ai/t2v/fal/veo3-pro"

        def failing_executor(job):
            raise _FakeHTTPError(response=_FakeResponse(429))

        self.dispatcher.job_executor = failing_executor

        job_id = self.store.insert_job(
            endpoint=ep, submit_tool="submit", args="{}",
        )
        job = self.store.get_job(job_id)
        self.store.update_status(job_id, "running")
        self.dispatcher._run_job(job)

        # Group counter saw the 429
        gstatus = self.dispatcher.group_limiter.get_all_status()["premium-video"]
        self.assertGreater(gstatus["consecutive_429"], 0)

        # Category counter must NOT have been charged for the same hit
        cstatus = self.dispatcher.category_limiter.get_all_status()["t2v"]
        self.assertEqual(cstatus["consecutive_429"], 0)
        self.assertEqual(cstatus["inflight"], 0)


class TestCategoryEndpointStillUsesCategoryLimiter(_ResolveLimiterBase):
    """An endpoint with a category prefix and no group match must
    still go through the category limiter (no regression vs PR1)."""

    custom_groups = {
        "premium-video": {
            "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
            "max_inflight": 1,
        },
    }

    def test_t2i_endpoint_charges_category_only(self):
        ep = "https://kamui-code.ai/t2i/fal/flux-lora"

        def failing_executor(job):
            raise _FakeHTTPError(response=_FakeResponse(429))

        self.dispatcher.job_executor = failing_executor

        job_id = self.store.insert_job(
            endpoint=ep, submit_tool="submit", args="{}",
        )
        job = self.store.get_job(job_id)
        self.store.update_status(job_id, "running")
        self.dispatcher._run_job(job)

        cstatus = self.dispatcher.category_limiter.get_all_status()["t2i"]
        self.assertGreater(cstatus["consecutive_429"], 0)

        gstatus = self.dispatcher.group_limiter.get_all_status()
        # premium-video group must not have been charged
        self.assertEqual(gstatus["premium-video"]["consecutive_429"], 0)


class TestUnknownEndpointStillDispatches(_ResolveLimiterBase):
    """Pre-PR4 behaviour preserved: an endpoint matching neither a
    group nor a recognised category dispatches without any per-key
    accounting being created. This is the PR1
    ``TestUnknownCategoryEndpointDispatch`` contract."""

    custom_groups = {
        "premium-video": {
            "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
            "max_inflight": 1,
        },
    }

    def test_unknown_endpoint_dispatches(self):
        ep = "https://example.com/some/random/path"
        # Sanity precondition
        limiter, key = self.dispatcher._resolve_limiter(ep)
        self.assertIsNone(limiter)
        self.assertIsNone(key)

        job_id = self.store.insert_job(
            endpoint=ep, submit_tool="submit", args="{}",
        )
        dispatched = self.dispatcher.dispatch_once()
        self.assertEqual(dispatched, 1)

        # Wait for executor
        for _ in range(50):
            if self.executed:
                break
            time.sleep(0.02)
        self.assertEqual(self.executed, [job_id])

        # No per-key accounting created in either limiter
        with self.dispatcher.category_limiter._lock:
            self.assertEqual(self.dispatcher.category_limiter._inflight, {})
        with self.dispatcher.group_limiter._lock:
            self.assertEqual(self.dispatcher.group_limiter._inflight, {})


class TestNoGroupConfigurationFallsBackToCategory(unittest.TestCase):
    """When no ``custom_groups`` block is present at all, every
    endpoint with a category prefix still routes to the category
    limiter — same as pre-PR4."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = JobStore(self.tmp.name)
        self.config = QueueConfig.from_dict({
            "category_rate_limits": {
                "categories": ["t2i", "i2i", "t2v", "i2v"],
                "limits": {
                    "t2v": {"max_inflight": 1, "min_interval": 0.0,
                             "exhaust_cooldown": 60},
                },
            },
            # No custom_groups key at all
        })
        self.dispatcher = Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=lambda job: None,
            loop_interval=0.01,
        )

    def tearDown(self):
        try:
            self.dispatcher.stop()
        except Exception:
            pass
        self.store.close()
        try:
            os.unlink(self.tmp.name)
        except FileNotFoundError:
            pass

    def test_t2v_endpoint_routes_to_category(self):
        ep = "https://kamui-code.ai/t2v/fal/veo3-pro"
        limiter, key = self.dispatcher._resolve_limiter(ep)
        self.assertIs(limiter, self.dispatcher.category_limiter)
        self.assertEqual(key, "t2v")

    def test_group_limiter_is_present_but_empty(self):
        # PR4 always creates the group_limiter for shape consistency
        self.assertIsNotNone(self.dispatcher.group_limiter)
        self.assertEqual(self.dispatcher.group_limiter.get_groups(), [])


if __name__ == "__main__":
    unittest.main()
