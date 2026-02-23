# -*- coding: utf-8 -*-
"""Tests for Dispatcher._run_job rate-limit handling.

Verifies that 429 errors cause jobs to be requeued (pending/recovering)
instead of marked as failed, and that pause_endpoint is called.
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

    def test_run_job_429_with_remote_id_requeues_to_recovering(self):
        """A 429 on a job with remote_job_id should requeue to recovering."""
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
        self.assertEqual(updated["status"], "recovering")

    def test_run_job_429_pauses_endpoint(self):
        """A 429 should call pause_endpoint with Retry-After duration."""
        resp = _FakeResponse(429, headers={"Retry-After": "15"})
        endpoint = "https://example.com/mcp"

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

        # pause_until should be set for this endpoint
        self.assertIn(endpoint, dispatcher._pause_until)
        remaining = dispatcher._pause_until[endpoint] - time.monotonic()
        # Should be roughly 15 seconds (allow some tolerance)
        self.assertGreater(remaining, 10.0)
        self.assertLessEqual(remaining, 15.0)

    def test_run_job_429_default_pause_without_retry_after(self):
        """A 429 without Retry-After should pause for 30 seconds."""
        resp = _FakeResponse(429)
        endpoint = "https://example.com/mcp"

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
        self.assertGreater(remaining, 25.0)
        self.assertLessEqual(remaining, 30.0)

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


if __name__ == "__main__":
    unittest.main()
