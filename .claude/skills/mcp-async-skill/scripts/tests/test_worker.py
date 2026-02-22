# -*- coding: utf-8 -*-
"""Tests for the worker HTTP daemon (mcp_worker_daemon.py).

Starts an actual HTTP server on a test port and exercises the REST API.
"""
import sys
import os
import json
import time
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

from job_queue import db
from job_queue import worker


def get_free_port():
    """Get a free port on localhost."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class WorkerTestBase(unittest.TestCase):
    """Base class that starts/stops a worker on a random port."""

    def setUp(self):
        self.port = get_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

        # Use a no-op executor for basic API tests
        self.executed_jobs = []

        def mock_executor(job):
            self.executed_jobs.append(job["id"])
            # Simulate quick completion
            self.worker_app.store.update_status(
                job["id"], "completed", result='{"urls":["https://example.com/img.png"]}'
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
            idle_timeout=0,  # Disable idle timeout for tests
        )
        self.worker_app.start()
        # Wait for server to be ready
        for _ in range(50):
            try:
                requests.get(f"{self.base_url}/api/health", timeout=0.5)
                break
            except requests.ConnectionError:
                time.sleep(0.05)

    def tearDown(self):
        self.worker_app.stop()


class TestHealthEndpoint(WorkerTestBase):
    """GET /api/health."""

    def test_health_returns_ok(self):
        resp = requests.get(f"{self.base_url}/api/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("active_jobs", data)
        self.assertIn("pending_jobs", data)


class TestJobSubmission(WorkerTestBase):
    """POST /api/jobs."""

    def test_submit_job_returns_job_id(self):
        resp = requests.post(
            f"{self.base_url}/api/jobs",
            json={
                "endpoint": "http://mcp-server:8000/sse",
                "submit_tool": "generate_image",
                "args": {"prompt": "a cat"},
                "status_tool": "check_status",
                "result_tool": "get_result",
            },
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("job_id", data)
        self.assertEqual(data["status"], "pending")

    def test_submit_job_missing_required_fields(self):
        resp = requests.post(
            f"{self.base_url}/api/jobs",
            json={"endpoint": "http://mcp-server:8000/sse"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_submit_multiple_jobs(self):
        ids = set()
        for i in range(3):
            resp = requests.post(
                f"{self.base_url}/api/jobs",
                json={
                    "endpoint": "http://mcp-server:8000/sse",
                    "submit_tool": "gen",
                    "args": {"i": i},
                },
            )
            self.assertEqual(resp.status_code, 200)
            ids.add(resp.json()["job_id"])
        self.assertEqual(len(ids), 3)


class TestJobQuery(WorkerTestBase):
    """GET /api/jobs/{job_id}."""

    def test_query_nonexistent_job(self):
        resp = requests.get(f"{self.base_url}/api/jobs/nonexistent-id")
        self.assertEqual(resp.status_code, 404)

    def test_query_existing_job(self):
        # Submit first
        submit_resp = requests.post(
            f"{self.base_url}/api/jobs",
            json={
                "endpoint": "http://mcp-server:8000/sse",
                "submit_tool": "gen",
                "args": {},
            },
        )
        job_id = submit_resp.json()["job_id"]

        # Query
        resp = requests.get(f"{self.base_url}/api/jobs/{job_id}")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["job_id"], job_id)
        self.assertIn(data["status"], ["pending", "running", "completed"])

    def test_job_gets_dispatched_and_completed(self):
        submit_resp = requests.post(
            f"{self.base_url}/api/jobs",
            json={
                "endpoint": "http://mcp-server:8000/sse",
                "submit_tool": "gen",
                "args": {},
            },
        )
        job_id = submit_resp.json()["job_id"]

        # Wait for dispatcher to pick it up
        for _ in range(20):
            resp = requests.get(f"{self.base_url}/api/jobs/{job_id}")
            data = resp.json()
            if data["status"] == "completed":
                break
            time.sleep(0.1)

        self.assertEqual(data["status"], "completed")
        self.assertIn("result", data)


class TestIdleTimeout(unittest.TestCase):
    """Worker should auto-stop after idle timeout."""

    def test_idle_timeout_stops_worker(self):
        port = get_free_port()

        app = worker.WorkerApp(
            host="127.0.0.1",
            port=port,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 2,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=lambda job: None,
            idle_timeout=1,  # 1 second timeout for test
        )
        app.start()

        # Verify it's running
        time.sleep(0.2)
        try:
            resp = requests.get(f"http://127.0.0.1:{port}/api/health", timeout=1)
            self.assertEqual(resp.status_code, 200)
        except requests.ConnectionError:
            self.fail("Worker should be running")

        # Wait for idle timeout
        time.sleep(2.0)

        # Should have shut down (ConnectionError or ReadTimeout)
        with self.assertRaises((requests.ConnectionError, requests.ReadTimeout)):
            requests.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)


class TestJobListEndpoint(WorkerTestBase):
    """GET /api/jobs (all jobs list)."""

    def test_list_jobs_empty(self):
        resp = requests.get(f"{self.base_url}/api/jobs")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("jobs", data)
        self.assertEqual(data["jobs"], [])

    def test_list_jobs_returns_submitted_jobs(self):
        # Submit 3 jobs
        for i in range(3):
            requests.post(
                f"{self.base_url}/api/jobs",
                json={"endpoint": "http://mcp:8000", "submit_tool": "gen", "args": {"i": i}},
            )
        resp = requests.get(f"{self.base_url}/api/jobs")
        data = resp.json()
        self.assertGreaterEqual(len(data["jobs"]), 3)

    def test_list_jobs_filter_by_status(self):
        # Submit a job and wait for it to complete
        submit_resp = requests.post(
            f"{self.base_url}/api/jobs",
            json={"endpoint": "http://mcp:8000", "submit_tool": "gen", "args": {}},
        )
        job_id = submit_resp.json()["job_id"]
        for _ in range(20):
            r = requests.get(f"{self.base_url}/api/jobs/{job_id}")
            if r.json()["status"] == "completed":
                break
            time.sleep(0.1)

        resp = requests.get(f"{self.base_url}/api/jobs?status=completed")
        data = resp.json()
        for job in data["jobs"]:
            self.assertEqual(job["status"], "completed")

    def test_list_jobs_includes_total_count(self):
        for i in range(2):
            requests.post(
                f"{self.base_url}/api/jobs",
                json={"endpoint": "http://mcp:8000", "submit_tool": "gen", "args": {}},
            )
        resp = requests.get(f"{self.base_url}/api/jobs")
        data = resp.json()
        self.assertIn("total", data)
        self.assertGreaterEqual(data["total"], 2)


class TestStatsEndpoint(WorkerTestBase):
    """GET /api/stats (per-endpoint statistics)."""

    def test_stats_empty(self):
        resp = requests.get(f"{self.base_url}/api/stats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("endpoints", data)
        self.assertEqual(data["endpoints"], [])

    def test_stats_after_submissions(self):
        # Submit jobs to two different endpoints
        for ep in ["http://a:8000", "http://b:8001"]:
            requests.post(
                f"{self.base_url}/api/jobs",
                json={"endpoint": ep, "submit_tool": "gen", "args": {}},
            )
        time.sleep(0.5)  # Let dispatcher run
        resp = requests.get(f"{self.base_url}/api/stats")
        data = resp.json()
        endpoints = [s["endpoint"] for s in data["endpoints"]]
        self.assertIn("http://a:8000", endpoints)
        self.assertIn("http://b:8001", endpoints)

    def test_stats_has_counts(self):
        requests.post(
            f"{self.base_url}/api/jobs",
            json={"endpoint": "http://mcp:8000", "submit_tool": "gen", "args": {}},
        )
        time.sleep(0.5)
        resp = requests.get(f"{self.base_url}/api/stats")
        data = resp.json()
        if data["endpoints"]:
            stat = data["endpoints"][0]
            self.assertIn("total", stat)
            self.assertIn("pending", stat)
            self.assertIn("running", stat)
            self.assertIn("completed", stat)
            self.assertIn("failed", stat)


class TestNotFoundRoute(WorkerTestBase):
    """Unknown routes return 404."""

    def test_unknown_route(self):
        resp = requests.get(f"{self.base_url}/api/unknown")
        self.assertEqual(resp.status_code, 404)


class TestRateLimitRegistration(WorkerTestBase):
    """POST /api/jobs with rate_limits field triggers dynamic registration."""

    def test_rate_limits_registered_on_submit(self):
        """Submitting a job with rate_limits should register limits in dispatcher."""
        resp = requests.post(
            f"{self.base_url}/api/jobs",
            json={
                "endpoint": "http://new-endpoint:8000",
                "submit_tool": "gen",
                "args": {},
                "rate_limits": {
                    "max_concurrent_jobs": 1,
                    "min_interval_seconds": 5.0,
                },
            },
        )
        self.assertEqual(resp.status_code, 200)

        # Verify the limits were registered in the dispatcher config
        max_c, min_i = self.worker_app.dispatcher.config.get_limits(
            "http://new-endpoint:8000"
        )
        self.assertEqual(max_c, 1)
        self.assertEqual(min_i, 5.0)

    def test_rate_limits_not_overwrite_existing(self):
        """If endpoint already has limits, rate_limits in POST should not overwrite."""
        # Pre-register limits
        self.worker_app.dispatcher.register_endpoint_limits(
            "http://existing:8000", 2, 3.0
        )

        resp = requests.post(
            f"{self.base_url}/api/jobs",
            json={
                "endpoint": "http://existing:8000",
                "submit_tool": "gen",
                "args": {},
                "rate_limits": {
                    "max_concurrent_jobs": 99,
                    "min_interval_seconds": 0.0,
                },
            },
        )
        self.assertEqual(resp.status_code, 200)

        # Should keep original values
        max_c, min_i = self.worker_app.dispatcher.config.get_limits(
            "http://existing:8000"
        )
        self.assertEqual(max_c, 2)
        self.assertEqual(min_i, 3.0)

    def test_submit_without_rate_limits_works(self):
        """Submitting without rate_limits should still work (backward compatible)."""
        resp = requests.post(
            f"{self.base_url}/api/jobs",
            json={
                "endpoint": "http://no-limits:8000",
                "submit_tool": "gen",
                "args": {},
            },
        )
        self.assertEqual(resp.status_code, 200)
        # Should use defaults
        max_c, min_i = self.worker_app.dispatcher.config.get_limits(
            "http://no-limits:8000"
        )
        self.assertEqual(max_c, 5)  # default from setUp
        self.assertEqual(min_i, 0.0)


class TestStartupRollbackAndPurge(unittest.TestCase):
    """WorkerApp.start() should execute rollback_fn and purge on startup."""

    def test_rollback_fn_called_on_start(self):
        """rollback_fn should be called with the store during start()."""
        port = get_free_port()
        rollback_called_with = []

        def my_rollback(store):
            rollback_called_with.append(store)

        app = worker.WorkerApp(
            host="127.0.0.1",
            port=port,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 2,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=lambda job: None,
            idle_timeout=0,
        )
        app.start(rollback_fn=my_rollback)
        time.sleep(0.2)
        app.stop()

        self.assertEqual(len(rollback_called_with), 1)
        self.assertIs(rollback_called_with[0], app.store)

    def test_purge_runs_on_start(self):
        """Old completed/failed jobs should be purged on start."""
        port = get_free_port()
        import tempfile, os
        db_file = os.path.join(tempfile.mkdtemp(), "test_purge.db")

        app = worker.WorkerApp(
            host="127.0.0.1",
            port=port,
            db_path=db_file,
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 2,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=lambda job: None,
            idle_timeout=0,
        )

        # Manually insert an old completed job with an old timestamp
        app.store.conn.execute(
            """INSERT INTO jobs (id, endpoint, submit_tool, args, status, updated_at)
               VALUES ('old-job', 'http://a:8000', 'g', '{}', 'completed',
                       strftime('%Y-%m-%dT%H:%M:%f', 'now', '-2 days'))"""
        )
        app.store.conn.commit()

        # Verify it exists
        self.assertIsNotNone(app.store.get_job("old-job"))

        # Start with 1-second retention (the job is 2 days old)
        app.start(retention_seconds=1.0)
        time.sleep(0.2)
        app.stop()

        # Re-open the DB to verify purge happened (stop() closes the connection)
        verify_store = db.JobStore(db_file)
        self.assertIsNone(verify_store.get_job("old-job"))
        verify_store.close()
        os.unlink(db_file)

    def test_start_without_rollback_or_purge(self):
        """start() without rollback_fn should work (backward compatible)."""
        port = get_free_port()

        app = worker.WorkerApp(
            host="127.0.0.1",
            port=port,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 2,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=lambda job: None,
            idle_timeout=0,
        )
        # Should not raise
        app.start()
        time.sleep(0.2)
        app.stop()


class TestResultsDir(unittest.TestCase):
    """WorkerApp should accept and expose results_dir."""

    def test_results_dir_stored(self):
        port = get_free_port()
        app = worker.WorkerApp(
            host="127.0.0.1",
            port=port,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 2,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=lambda job: None,
            idle_timeout=0,
            results_dir="/tmp/test_results",
        )
        self.assertEqual(app.results_dir, "/tmp/test_results")

    def test_results_dir_defaults_to_none(self):
        port = get_free_port()
        app = worker.WorkerApp(
            host="127.0.0.1",
            port=port,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 2,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=lambda job: None,
            idle_timeout=0,
        )
        self.assertIsNone(app.results_dir)


if __name__ == "__main__":
    unittest.main()
