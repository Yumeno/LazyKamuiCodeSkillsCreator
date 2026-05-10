# -*- coding: utf-8 -*-
"""Tests for Dispatcher._run_job rate-limit handling.

Verifies that 429 errors cause jobs to be requeued and that
pause_endpoint is called with the per-category cooldown duration.

Spec history (important — do not "restore" the old behaviour without
re-evaluating it against kamuicode MCP):

* PR #40 (commit 64424c1) introduced the original 429 handler that read
  the ``Retry-After`` header (capped at 60s), defaulted to 30s, and
  routed jobs with ``remote_job_id`` to ``recovering``.
* **PR #47 (commit 742527a) intentionally rewrote this** to:
    - **Ignore the ``Retry-After`` header.** kamuicode MCP does NOT
      return reliable Retry-After values (e.g. daily-limit 429s come
      back as ordinary 429s with no distinguishing header), so trusting
      the server's self-reported wait time is unsafe.
    - **Use the per-category rolling-window cooldown** (default 3600s)
      to suppress the entire category, since 429 responses do not count
      toward the server's own rate window — overshooting the wait costs
      the client nothing while undershooting causes more 429s.
    - **Always re-queue to ``pending``**, regardless of ``remote_job_id``,
      because the dispatcher's category-cooldown guard is what protects
      the next dispatch.
* Tests below were updated in PR #59 (lazy-v2.11.0) to reflect this
  intent. The tests had been failing silently since PR #47 because the
  old expectations were never adjusted.
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
    """Minimal fake requests.Response."""

    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeHTTPError(Exception):
    """Fake requests.HTTPError with a response attribute."""

    def __init__(self, response=None):
        super().__init__(f"HTTP {response.status_code}" if response else "HTTP Error")
        self.response = response


class TestRunJob429Handling(unittest.TestCase):
    """Verify _run_job requeues jobs on 429 instead of failing them."""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = JobStore(self.tmp.name)
        self.config = QueueConfig()

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def _make_dispatcher(self, executor):
        return Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=executor,
        )

    def test_run_job_429_requeues_to_pending(self):
        """A 429 on a job without remote_job_id should requeue to pending."""
        resp = _FakeResponse(429, headers={"Retry-After": "5"})

        def failing_executor(job):
            raise _FakeHTTPError(response=resp)

        dispatcher = self._make_dispatcher(failing_executor)

        job_id = self.store.insert_job(
            endpoint="https://example.com/mcp",
            submit_tool="submit",
            args="{}",
        )
        job = self.store.get_job(job_id)
        self.store.update_status(job_id, "running")

        dispatcher._run_job(job)

        updated = self.store.get_job(job_id)
        self.assertEqual(updated["status"], "pending")

    def test_run_job_429_requeues_to_pending_regardless_of_remote_id(self):
        """A 429 always re-queues to ``pending``, even when the job was
        already polling with a ``remote_job_id`` (PR #47 behaviour).

        Rationale: the per-category cooldown takes care of throttling, so
        the dispatcher does not need to distinguish "submit again" from
        "resume polling". Treating both uniformly keeps the recovery
        path simple and avoids polling a remote job that the server may
        have dropped while we were rate-limited.
        """
        resp = _FakeResponse(429)

        def failing_executor(job):
            raise _FakeHTTPError(response=resp)

        dispatcher = self._make_dispatcher(failing_executor)

        job_id = self.store.insert_job(
            endpoint="https://example.com/mcp",
            submit_tool="submit",
            args="{}",
        )
        self.store.update_status(
            job_id, "polling",
            remote_job_id="remote-123",
            session_id="sess-1",
        )
        job = self.store.get_job(job_id)

        dispatcher._run_job(job)

        updated = self.store.get_job(job_id)
        self.assertEqual(updated["status"], "pending")

    def test_run_job_429_pauses_endpoint_with_per_category_cooldown(self):
        """A 429 pauses the endpoint for the per-category cooldown duration.

        ``Retry-After`` is intentionally ignored (see module docstring).
        For an endpoint without a recognised category the default cooldown
        falls back to ``HARDCODED_DEFAULT_EXHAUST_COOLDOWN`` (3600s).
        """
        resp = _FakeResponse(429, headers={"Retry-After": "15"})
        # Use a kamuicode-style URL so extract_category() returns a real
        # category (`t2i`). This anchors the test to the real per-category
        # cooldown lookup path rather than the unknown-category fallback.
        endpoint = "https://kamui-code.ai/t2i/fal/flux-lora"

        def failing_executor(job):
            raise _FakeHTTPError(response=resp)

        dispatcher = self._make_dispatcher(failing_executor)
        expected_cooldown = dispatcher.category_limiter.get_exhaust_cooldown("t2i")

        job_id = self.store.insert_job(
            endpoint=endpoint,
            submit_tool="submit",
            args="{}",
        )
        job = self.store.get_job(job_id)
        self.store.update_status(job_id, "running")

        dispatcher._run_job(job)

        # pause_until should be set for this endpoint
        self.assertIn(endpoint, dispatcher._pause_until)
        remaining = dispatcher._pause_until[endpoint] - time.monotonic()
        # Retry-After=15 is ignored; pause is the per-category cooldown.
        self.assertGreater(remaining, expected_cooldown - 5.0)
        self.assertLessEqual(remaining, expected_cooldown)

    def test_run_job_429_default_pause_uses_hardcoded_when_unknown_category(self):
        """A 429 from an unknown-category endpoint pauses for the
        hardcoded default cooldown (3600s)."""
        from job_queue.category_limiter import HARDCODED_DEFAULT_EXHAUST_COOLDOWN

        resp = _FakeResponse(429)
        endpoint = "https://example.com/mcp"  # no t2i/i2i/t2v/i2v prefix

        def failing_executor(job):
            raise _FakeHTTPError(response=resp)

        dispatcher = self._make_dispatcher(failing_executor)

        job_id = self.store.insert_job(
            endpoint=endpoint,
            submit_tool="submit",
            args="{}",
        )
        job = self.store.get_job(job_id)
        self.store.update_status(job_id, "running")

        dispatcher._run_job(job)

        remaining = dispatcher._pause_until[endpoint] - time.monotonic()
        self.assertGreater(remaining, HARDCODED_DEFAULT_EXHAUST_COOLDOWN - 5.0)
        self.assertLessEqual(remaining, HARDCODED_DEFAULT_EXHAUST_COOLDOWN)

    def test_run_job_non_429_marks_failed(self):
        """A non-429 error should mark the job as failed (existing behavior)."""
        resp = _FakeResponse(500)

        def failing_executor(job):
            raise _FakeHTTPError(response=resp)

        dispatcher = self._make_dispatcher(failing_executor)

        job_id = self.store.insert_job(
            endpoint="https://example.com/mcp",
            submit_tool="submit",
            args="{}",
        )
        job = self.store.get_job(job_id)
        self.store.update_status(job_id, "running")

        dispatcher._run_job(job)

        updated = self.store.get_job(job_id)
        self.assertEqual(updated["status"], "failed")
        self.assertIn("500", updated["error"])

    def test_run_job_generic_exception_marks_failed(self):
        """A generic exception (no response attr) should mark failed."""
        def failing_executor(job):
            raise RuntimeError("something broke")

        dispatcher = self._make_dispatcher(failing_executor)

        job_id = self.store.insert_job(
            endpoint="https://example.com/mcp",
            submit_tool="submit",
            args="{}",
        )
        job = self.store.get_job(job_id)
        self.store.update_status(job_id, "running")

        dispatcher._run_job(job)

        updated = self.store.get_job(job_id)
        self.assertEqual(updated["status"], "failed")
        self.assertIn("something broke", updated["error"])


class TestUnknownCategoryEndpointDispatch(unittest.TestCase):
    """An endpoint whose URL doesn't carry a recognised category prefix
    (i.e. ``extract_category()`` returns None) must still be dispatchable
    — the limiter is bypassed entirely for category-less endpoints.

    This pins down the dispatcher contract that PR #59's CategoryLimiter
    relies on: ``can_submit(None) == True`` plus ``acquire_inflight(None)
    == False`` together mean "let the dispatcher schedule this job, but
    don't create category-level inflight state for it". Without this
    test, a future change that makes ``can_submit(None)`` return False
    (or that mistakenly funnels None through the category accounting
    path) could silently strand category-less jobs in pending forever.
    """

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.store = JobStore(self.tmp.name)
        self.config = QueueConfig()
        self.executed: list[str] = []

    def tearDown(self):
        self.store.close()
        os.unlink(self.tmp.name)

    def _make_dispatcher_recording(self):
        def recording_executor(job):
            self.executed.append(job["id"])

        return Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=recording_executor,
            loop_interval=0.01,
        )

    def test_category_less_endpoint_gets_dispatched(self):
        """A pending job on an endpoint with no t2i/i2i/t2v/i2v prefix
        must transition out of pending in a single dispatch round."""
        dispatcher = self._make_dispatcher_recording()

        # Sanity: this URL has no recognised category prefix
        endpoint = "https://example.com/some/random/path"
        self.assertIsNone(
            dispatcher.category_limiter.extract_category(endpoint),
            "Test premise: endpoint must have no recognised category",
        )

        job_id = self.store.insert_job(
            endpoint=endpoint,
            submit_tool="submit",
            args="{}",
        )

        # One dispatch round should pick it up.
        dispatched = dispatcher.dispatch_once()

        self.assertEqual(dispatched, 1,
                         "Category-less endpoint must be dispatchable")
        # And the executor should eventually run (give the thread pool a moment)
        for _ in range(50):
            if self.executed:
                break
            time.sleep(0.02)
        self.assertEqual(self.executed, [job_id])

        dispatcher.stop()

    def test_category_less_endpoint_creates_no_category_inflight_state(self):
        """Dispatching a category-less endpoint must not leave any
        per-category inflight state behind. Only acquire_inflight(None)
        is called, which returns False without touching the dict."""
        dispatcher = self._make_dispatcher_recording()
        endpoint = "https://example.com/some/random/path"

        job_id = self.store.insert_job(
            endpoint=endpoint, submit_tool="submit", args="{}",
        )
        dispatcher.dispatch_once()

        # Wait for the executor to finish
        for _ in range(50):
            if self.executed:
                break
            time.sleep(0.02)

        # No category accounting created
        self.assertEqual(dispatcher.category_limiter._inflight, {})

        dispatcher.stop()


if __name__ == "__main__":
    unittest.main()
