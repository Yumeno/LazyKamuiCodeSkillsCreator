# -*- coding: utf-8 -*-
"""Tests for the queue client (queue_client.py).

Tests the 3 client modes: --submit-only, --wait, --blocking.
Uses a real worker instance for integration testing.
"""
import sys
import os
import json
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

from job_queue import db, worker
from job_queue import client as queue_client


def get_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ClientTestBase(unittest.TestCase):
    """Base class that starts a worker with a mock executor."""

    def setUp(self):
        self.port = get_free_port()
        self.worker_url = f"http://127.0.0.1:{self.port}"

        def mock_executor(job):
            store = self.worker_app.store
            job_data = store.get_job(job["id"])
            store.update_status(
                job["id"],
                "polling",
                session_id="mock-session",
                remote_job_id="mock-remote-123",
            )
            time.sleep(0.1)  # Simulate processing
            store.update_status(
                job["id"],
                "completed",
                result=json.dumps({"urls": ["https://example.com/output.png"]}),
            )

        self.worker_app = worker.WorkerApp(
            host="127.0.0.1",
            port=self.port,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 5,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=mock_executor,
            idle_timeout=0,
        )
        self.worker_app.start()
        # Wait for server ready
        for _ in range(50):
            try:
                requests.get(f"{self.worker_url}/api/health", timeout=0.5)
                break
            except requests.ConnectionError:
                time.sleep(0.05)

    def tearDown(self):
        self.worker_app.stop()


class TestSubmitOnly(ClientTestBase):
    """--submit-only mode: submit job and return job_id immediately."""

    def test_submit_returns_job_id(self):
        result = queue_client.submit_job(
            worker_url=self.worker_url,
            endpoint="http://mcp-server:8000/sse",
            submit_tool="generate_image",
            args={"prompt": "a cat"},
            status_tool="check_status",
            result_tool="get_result",
        )
        self.assertIn("job_id", result)
        self.assertEqual(result["status"], "pending")

    def test_submit_with_headers(self):
        result = queue_client.submit_job(
            worker_url=self.worker_url,
            endpoint="http://mcp-server:8000/sse",
            submit_tool="generate_image",
            args={"prompt": "a cat"},
            headers={"Authorization": "Bearer test-token"},
        )
        self.assertIn("job_id", result)


class TestWaitMode(ClientTestBase):
    """--wait mode: query job status."""

    def test_wait_returns_status(self):
        # Submit first
        submit_result = queue_client.submit_job(
            worker_url=self.worker_url,
            endpoint="http://mcp-server:8000/sse",
            submit_tool="gen",
            args={},
        )
        job_id = submit_result["job_id"]

        # Wait for it
        result = queue_client.wait_job(
            worker_url=self.worker_url,
            job_id=job_id,
        )
        self.assertEqual(result["job_id"], job_id)
        self.assertIn("status", result)

    def test_wait_nonexistent_job(self):
        result = queue_client.wait_job(
            worker_url=self.worker_url,
            job_id="nonexistent",
        )
        self.assertIn("error", result)


class TestBlockingMode(ClientTestBase):
    """--blocking mode: submit → poll → return completed result."""

    def test_blocking_returns_completed(self):
        result = queue_client.blocking_job(
            worker_url=self.worker_url,
            endpoint="http://mcp-server:8000/sse",
            submit_tool="generate_image",
            args={"prompt": "a cat"},
            status_tool="check_status",
            result_tool="get_result",
            poll_interval=0.1,
            max_polls=50,
        )
        self.assertEqual(result["status"], "completed")
        self.assertIn("result", result)

    def test_blocking_timeout(self):
        """Blocking mode should raise if max_polls exceeded."""
        # Use a slow executor that never completes
        port2 = get_free_port()

        def never_complete(job):
            pass  # Never updates status

        app2 = worker.WorkerApp(
            host="127.0.0.1",
            port=port2,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 5,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=never_complete,
            idle_timeout=0,
        )
        app2.start()
        time.sleep(0.3)

        try:
            with self.assertRaises(TimeoutError):
                queue_client.blocking_job(
                    worker_url=f"http://127.0.0.1:{port2}",
                    endpoint="http://mcp-server:8000/sse",
                    submit_tool="gen",
                    args={},
                    poll_interval=0.05,
                    max_polls=3,
                )
        finally:
            app2.stop()


class TestAutoStartDetection(unittest.TestCase):
    """Client should detect when worker is not running."""

    def test_detect_worker_not_running(self):
        port = get_free_port()
        is_running = queue_client.is_worker_running(f"http://127.0.0.1:{port}")
        self.assertFalse(is_running)

    def test_detect_worker_running(self):
        port = get_free_port()
        app = worker.WorkerApp(
            host="127.0.0.1",
            port=port,
            db_path=":memory:",
            idle_timeout=0,
        )
        app.start()
        time.sleep(0.3)
        try:
            is_running = queue_client.is_worker_running(f"http://127.0.0.1:{port}")
            self.assertTrue(is_running)
        finally:
            app.stop()


if __name__ == "__main__":
    unittest.main()
