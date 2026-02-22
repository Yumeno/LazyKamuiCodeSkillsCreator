# -*- coding: utf-8 -*-
"""Tests for queue integration in mcp_async_call.py.

Covers CLI argument parsing, worker URL resolution, routing logic,
and E2E flows using a real worker instance.
"""
import sys
import os
import json
import time
import unittest
from unittest import mock
from io import StringIO

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests
from job_queue import worker


def get_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── 5-1. CLI Argument Parsing ──────────────────────────────────────────

class TestParseArgsQueue(unittest.TestCase):
    """New queue-related CLI arguments are accepted by argparse."""

    def _parse(self, args_list: list[str]):
        """Helper: import and call the parser from mcp_async_call."""
        from mcp_async_call import build_parser
        parser = build_parser()
        return parser.parse_args(args_list)

    def test_parse_args_queue_config(self):
        args = self._parse([
            "--submit-tool", "gen",
            "--queue-config", "queue_config.json",
        ])
        self.assertEqual(args.queue_config, "queue_config.json")

    def test_parse_args_worker_url(self):
        args = self._parse([
            "--submit-tool", "gen",
            "--worker-url", "http://127.0.0.1:55555",
        ])
        self.assertEqual(args.worker_url, "http://127.0.0.1:55555")

    def test_parse_args_submit_only(self):
        args = self._parse([
            "--submit-tool", "gen",
            "--queue-config", "q.json",
            "--submit-only",
        ])
        self.assertTrue(args.submit_only)

    def test_parse_args_wait(self):
        args = self._parse([
            "--wait", "abc-123",
        ])
        self.assertEqual(args.wait, "abc-123")

    def test_parse_args_status_tool_not_required(self):
        """--status-tool and --result-tool should not be required in queue mode."""
        # Should not raise SystemExit
        args = self._parse([
            "--submit-tool", "gen",
            "--queue-config", "q.json",
            "--submit-only",
        ])
        self.assertIsNone(getattr(args, "status_tool", None))


# ── 5-2. Worker URL Resolution ─────────────────────────────────────────

class TestResolveWorkerUrl(unittest.TestCase):
    """Resolve worker URL from --worker-url, --queue-config, or None."""

    def test_resolve_from_worker_url_arg(self):
        from mcp_async_call import resolve_worker_url
        url = resolve_worker_url(
            worker_url="http://127.0.0.1:55555",
            queue_config_path=None,
        )
        self.assertEqual(url, "http://127.0.0.1:55555")

    def test_resolve_from_queue_config(self):
        import tempfile
        from mcp_async_call import resolve_worker_url
        config = {"host": "127.0.0.1", "port": 12345}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(config, f)
            f.flush()
            url = resolve_worker_url(worker_url=None, queue_config_path=f.name)
        os.unlink(f.name)
        self.assertEqual(url, "http://127.0.0.1:12345")

    def test_resolve_default_none(self):
        from mcp_async_call import resolve_worker_url
        url = resolve_worker_url(worker_url=None, queue_config_path=None)
        self.assertIsNone(url)


# ── 5-3. Routing Logic ─────────────────────────────────────────────────

class TestRouting(unittest.TestCase):
    """Queue mode routing: direct vs submit-only vs wait vs blocking."""

    def test_route_direct_mode_without_queue(self):
        """No queue args → run_async_mcp_job is called."""
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call.run_async_mcp_job") as m:
            m.return_value = {"status": "completed"}
            result = route_execution(
                worker_url=None,
                queue_config_path=None,
                submit_only=False,
                wait_job_id=None,
                endpoint="http://mcp:8000",
                submit_tool="gen",
                submit_args={"prompt": "cat"},
                status_tool="status",
                result_tool="result",
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,

                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m.assert_called_once()
            self.assertEqual(result["status"], "completed")

    def test_route_queue_submit_only(self):
        """--queue-config + --submit-only → client.submit_job()."""
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_submit_only") as m:
            m.return_value = {"job_id": "abc", "status": "pending"}
            result = route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=True,
                wait_job_id=None,
                endpoint="http://mcp:8000",
                submit_tool="gen",
                submit_args={"prompt": "cat"},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,

                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m.assert_called_once()
            self.assertEqual(result["status"], "pending")

    def test_route_queue_wait(self):
        """--wait JOB_ID → client.wait_job()."""
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_wait") as m:
            m.return_value = {"job_id": "abc", "status": "completed"}
            result = route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id="abc",
                endpoint=None,
                submit_tool=None,
                submit_args={},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,

                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m.assert_called_once()

    def test_route_queue_blocking(self):
        """--queue-config only → client.blocking_job()."""
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_blocking") as m:
            m.return_value = {"job_id": "abc", "status": "completed"}
            result = route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id=None,
                endpoint="http://mcp:8000",
                submit_tool="gen",
                submit_args={"prompt": "cat"},
                status_tool="status",
                result_tool="result",
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,

                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m.assert_called_once()


# ── 5-4. E2E Integration Tests ─────────────────────────────────────────

class TestE2EQueueMode(unittest.TestCase):
    """End-to-end tests with a real worker instance."""

    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.worker_url = f"http://127.0.0.1:{cls.port}"

        def mock_executor(job):
            cls.worker_app.store.update_status(
                job["id"], "completed",
                result=json.dumps({"urls": ["https://example.com/img.png"]}),
            )

        cls.worker_app = worker.WorkerApp(
            host="127.0.0.1",
            port=cls.port,
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
        cls.worker_app.start()
        for _ in range(50):
            try:
                requests.get(f"{cls.worker_url}/api/health", timeout=0.5)
                break
            except requests.ConnectionError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.worker_app.stop()

    def test_e2e_submit_only(self):
        from mcp_async_call import _queue_submit_only
        result = _queue_submit_only(
            worker_url=self.worker_url,
            endpoint="http://mcp:8000/sse",
            submit_tool="generate_image",
            submit_args={"prompt": "cat"},
            status_tool="check_status",
            result_tool="get_result",
            headers=None,
        )
        self.assertIn("job_id", result)
        self.assertEqual(result["status"], "pending")

    def test_e2e_wait(self):
        from mcp_async_call import _queue_submit_only, _queue_wait
        submit_result = _queue_submit_only(
            worker_url=self.worker_url,
            endpoint="http://mcp:8000/sse",
            submit_tool="gen",
            submit_args={},
            status_tool=None,
            result_tool=None,
            headers=None,
        )
        job_id = submit_result["job_id"]
        time.sleep(0.5)
        result = _queue_wait(self.worker_url, job_id)
        self.assertEqual(result["job_id"], job_id)
        self.assertIn(result["status"], ["pending", "running", "completed"])

    def test_e2e_blocking(self):
        from mcp_async_call import _queue_blocking
        result = _queue_blocking(
            worker_url=self.worker_url,
            endpoint="http://mcp:8000/sse",
            submit_tool="gen",
            submit_args={},
            status_tool=None,
            result_tool=None,
            headers=None,
            poll_interval=0.1,
            max_polls=50,
        )
        self.assertEqual(result["status"], "completed")


# ── 5-5. CLI --list / --stats Tests ───────────────────────────────────

class TestParseArgsListStats(unittest.TestCase):
    """CLI arguments --list and --stats are accepted."""

    def _parse(self, args_list: list[str]):
        from mcp_async_call import build_parser
        parser = build_parser()
        return parser.parse_args(args_list)

    def test_parse_args_list(self):
        args = self._parse(["--list", "--queue-config", "q.json"])
        self.assertTrue(args.list)

    def test_parse_args_stats(self):
        args = self._parse(["--stats", "--queue-config", "q.json"])
        self.assertTrue(args.stats)

    def test_parse_args_list_with_status_filter(self):
        args = self._parse(["--list", "--queue-config", "q.json", "--filter-status", "pending"])
        self.assertEqual(args.filter_status, "pending")


class TestRouteListStats(unittest.TestCase):
    """Routing for --list and --stats modes."""

    def test_route_list(self):
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_list") as m:
            m.return_value = {"total": 0, "jobs": []}
            result = route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id=None,
                list_jobs=True,
                show_stats=False,
                filter_status=None,
                endpoint=None,
                submit_tool=None,
                submit_args={},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,

                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m.assert_called_once()

    def test_route_stats(self):
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_stats") as m:
            m.return_value = {"endpoints": []}
            result = route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id=None,
                list_jobs=False,
                show_stats=True,
                filter_status=None,
                endpoint=None,
                submit_tool=None,
                submit_args={},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,

                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m.assert_called_once()


class TestE2EListStats(unittest.TestCase):
    """E2E tests for --list and --stats with a real worker."""

    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.worker_url = f"http://127.0.0.1:{cls.port}"

        def mock_executor(job):
            cls.worker_app.store.update_status(
                job["id"], "completed",
                result=json.dumps({"urls": ["https://example.com/img.png"]}),
            )

        cls.worker_app = worker.WorkerApp(
            host="127.0.0.1",
            port=cls.port,
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
        cls.worker_app.start()
        for _ in range(50):
            try:
                requests.get(f"{cls.worker_url}/api/health", timeout=0.5)
                break
            except requests.ConnectionError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.worker_app.stop()

    def test_e2e_list_empty(self):
        from mcp_async_call import _queue_list
        result = _queue_list(self.worker_url, status_filter=None)
        self.assertIn("total", result)
        self.assertIn("jobs", result)

    def test_e2e_stats_empty(self):
        from mcp_async_call import _queue_stats
        result = _queue_stats(self.worker_url)
        self.assertIn("endpoints", result)

    def test_e2e_list_after_submit(self):
        from mcp_async_call import _queue_submit_only, _queue_list
        _queue_submit_only(
            worker_url=self.worker_url,
            endpoint="http://mcp:8000/sse",
            submit_tool="gen",
            submit_args={"prompt": "test"},
            status_tool=None,
            result_tool=None,
            headers=None,
        )
        time.sleep(0.5)
        result = _queue_list(self.worker_url, status_filter=None)
        self.assertGreaterEqual(result["total"], 1)


class TestRateLimitsPassthrough(unittest.TestCase):
    """Verify rate_limits are passed through submit_job to the POST body."""

    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.worker_url = f"http://127.0.0.1:{cls.port}"
        cls.worker_app = worker.WorkerApp(
            host="127.0.0.1",
            port=cls.port,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 5,
                    "min_interval_seconds": 0.0,
                },
            },
            job_executor=lambda job: None,
            idle_timeout=0,
        )
        cls.worker_app.start()
        for _ in range(50):
            try:
                requests.get(f"{cls.worker_url}/api/health", timeout=0.5)
                break
            except requests.ConnectionError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.worker_app.stop()

    def test_submit_with_rate_limits(self):
        """submit_job with rate_limits should register them in dispatcher."""
        from job_queue.client import submit_job
        result = submit_job(
            worker_url=self.worker_url,
            endpoint="http://rate-test:8000",
            submit_tool="gen",
            args={"prompt": "test"},
            rate_limits={"max_concurrent_jobs": 1, "min_interval_seconds": 5.0},
        )
        self.assertIn("job_id", result)
        # Check that limits were registered
        max_c, min_i = self.worker_app.dispatcher.config.get_limits(
            "http://rate-test:8000"
        )
        self.assertEqual(max_c, 1)
        self.assertEqual(min_i, 5.0)

    def test_submit_without_rate_limits_backward_compat(self):
        """submit_job without rate_limits should work as before."""
        from job_queue.client import submit_job
        result = submit_job(
            worker_url=self.worker_url,
            endpoint="http://no-limits:8000",
            submit_tool="gen",
            args={"prompt": "test"},
        )
        self.assertIn("job_id", result)


# ── 5-6. SQLite Fallback Tests ────────────────────────────────────────

class TestSqliteFallbackList(unittest.TestCase):
    """_queue_list falls back to SQLite when the worker is unreachable."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        # Create a .claude/queue/ structure with a real jobs.db
        self.queue_dir = os.path.join(self.tmpdir, ".claude", "queue")
        os.makedirs(self.queue_dir)
        self.db_path = os.path.join(self.queue_dir, "jobs.db")

        from job_queue.db import JobStore
        store = JobStore(self.db_path)
        store.insert_job(
            endpoint="http://mcp:8000",
            submit_tool="gen",
            args='{"prompt":"hello"}',
            status_tool="status",
            result_tool="result",
        )
        store.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fallback_returns_jobs(self):
        from mcp_async_call import _queue_list
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=self.db_path):
                result = _queue_list("http://127.0.0.1:54321",
                                     queue_config_path=None)
        self.assertIn("jobs", result)
        self.assertEqual(result["total"], 1)
        self.assertTrue(result.get("_fallback"))
        self.assertNotIn("args", result["jobs"][0])

    def test_fallback_with_include_args(self):
        from mcp_async_call import _queue_list
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=self.db_path):
                result = _queue_list("http://127.0.0.1:54321",
                                     include_args=True,
                                     queue_config_path=None)
        self.assertIn("args", result["jobs"][0])
        self.assertEqual(result["jobs"][0]["args"]["prompt"], "hello")

    def test_fallback_with_status_filter(self):
        from mcp_async_call import _queue_list
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=self.db_path):
                result = _queue_list("http://127.0.0.1:54321",
                                     status_filter="completed",
                                     queue_config_path=None)
        # No completed jobs, so list should be empty
        self.assertEqual(result["total"], 0)


class TestSqliteFallbackStats(unittest.TestCase):
    """_queue_stats falls back to SQLite when the worker is unreachable."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.queue_dir = os.path.join(self.tmpdir, ".claude", "queue")
        os.makedirs(self.queue_dir)
        self.db_path = os.path.join(self.queue_dir, "jobs.db")

        from job_queue.db import JobStore
        store = JobStore(self.db_path)
        store.insert_job(
            endpoint="http://mcp:8000",
            submit_tool="gen",
            args='{"prompt":"hello"}',
        )
        store.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fallback_returns_stats(self):
        from mcp_async_call import _queue_stats
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=self.db_path):
                result = _queue_stats("http://127.0.0.1:54321",
                                      queue_config_path=None)
        self.assertIn("endpoints", result)
        self.assertTrue(result.get("_fallback"))
        self.assertGreaterEqual(len(result["endpoints"]), 1)
        self.assertEqual(result["endpoints"][0]["endpoint"], "http://mcp:8000")


class TestSqliteFallbackWait(unittest.TestCase):
    """_queue_wait falls back to SQLite when the worker is unreachable."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()
        self.queue_dir = os.path.join(self.tmpdir, ".claude", "queue")
        os.makedirs(self.queue_dir)
        self.db_path = os.path.join(self.queue_dir, "jobs.db")

        from job_queue.db import JobStore
        store = JobStore(self.db_path)
        self.job_id = store.insert_job(
            endpoint="http://mcp:8000",
            submit_tool="gen",
            args='{"prompt":"test_wait"}',
        )
        store.close()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fallback_returns_job(self):
        from mcp_async_call import _queue_wait
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=self.db_path):
                result = _queue_wait("http://127.0.0.1:54321",
                                     self.job_id,
                                     queue_config_path=None)
        self.assertEqual(result["job_id"], self.job_id)
        self.assertEqual(result["status"], "pending")
        self.assertTrue(result.get("_fallback"))

    def test_fallback_with_include_args(self):
        from mcp_async_call import _queue_wait
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=self.db_path):
                result = _queue_wait("http://127.0.0.1:54321",
                                     self.job_id,
                                     include_args=True,
                                     queue_config_path=None)
        self.assertIn("args", result)
        self.assertEqual(result["args"]["prompt"], "test_wait")

    def test_fallback_nonexistent_job(self):
        from mcp_async_call import _queue_wait
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=self.db_path):
                result = _queue_wait("http://127.0.0.1:54321",
                                     "nonexistent-id",
                                     queue_config_path=None)
        self.assertIn("error", result)
        self.assertEqual(result["error"], "Job not found")


class TestSqliteFallbackNoDb(unittest.TestCase):
    """Fallback when jobs.db does not exist."""

    def test_list_fallback_no_db(self):
        from mcp_async_call import _queue_list
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=None):
                result = _queue_list("http://127.0.0.1:54321",
                                     queue_config_path=None)
        self.assertEqual(result["total"], 0)
        self.assertIn("error", result)

    def test_stats_fallback_no_db(self):
        from mcp_async_call import _queue_stats
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=None):
                result = _queue_stats("http://127.0.0.1:54321",
                                      queue_config_path=None)
        self.assertEqual(result["endpoints"], [])
        self.assertIn("error", result)

    def test_wait_fallback_no_db(self):
        from mcp_async_call import _queue_wait
        with mock.patch("mcp_async_call.requests.get", side_effect=requests.ConnectionError("refused")):
            with mock.patch("mcp_async_call._resolve_db_path", return_value=None):
                result = _queue_wait("http://127.0.0.1:54321",
                                     "some-id",
                                     queue_config_path=None)
        self.assertIn("error", result)


# ── 5-7. --show-args CLI Parsing ─────────────────────────────────────

class TestShowArgsFlag(unittest.TestCase):
    """--show-args flag is accepted and wired to route_execution."""

    def _parse(self, args_list: list[str]):
        from mcp_async_call import build_parser
        return build_parser().parse_args(args_list)

    def test_show_args_default_false(self):
        args = self._parse(["--list", "--queue-config", "q.json"])
        self.assertFalse(args.show_args)

    def test_show_args_true(self):
        args = self._parse(["--list", "--queue-config", "q.json", "--show-args"])
        self.assertTrue(args.show_args)

    def test_show_args_with_wait(self):
        args = self._parse(["--wait", "abc", "--show-args"])
        self.assertTrue(args.show_args)

    def test_route_list_passes_show_args(self):
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_list") as m:
            m.return_value = {"total": 0, "jobs": []}
            route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id=None,
                list_jobs=True,
                show_args=True,
                endpoint=None,
                submit_tool=None,
                submit_args={},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,
                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            # Verify include_args=True was passed
            call_kwargs = m.call_args
            self.assertTrue(call_kwargs[1].get("include_args") or
                            (len(call_kwargs[0]) > 2 and call_kwargs[0][2]))

    def test_route_wait_passes_show_args(self):
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_wait") as m:
            m.return_value = {"job_id": "abc", "status": "completed"}
            route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id="abc",
                show_args=True,
                endpoint=None,
                submit_tool=None,
                submit_args={},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,
                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            call_kwargs = m.call_args
            self.assertTrue(call_kwargs[1].get("include_args") or
                            (len(call_kwargs[0]) > 2 and call_kwargs[0][2]))


# ── 5-8. Read-only operations do NOT auto-start worker ───────────────

class TestReadOnlyNoAutoStart(unittest.TestCase):
    """--list/--stats/--wait should NOT call _ensure_worker_running."""

    def test_list_no_autostart(self):
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_list") as m_list, \
             mock.patch("mcp_async_call._ensure_worker_running") as m_ensure:
            m_list.return_value = {"total": 0, "jobs": []}
            route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id=None,
                list_jobs=True,
                endpoint=None,
                submit_tool=None,
                submit_args={},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,
                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m_ensure.assert_not_called()

    def test_stats_no_autostart(self):
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_stats") as m_stats, \
             mock.patch("mcp_async_call._ensure_worker_running") as m_ensure:
            m_stats.return_value = {"endpoints": []}
            route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id=None,
                show_stats=True,
                endpoint=None,
                submit_tool=None,
                submit_args={},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,
                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m_ensure.assert_not_called()

    def test_wait_no_autostart(self):
        from mcp_async_call import route_execution
        with mock.patch("mcp_async_call._queue_wait") as m_wait, \
             mock.patch("mcp_async_call._ensure_worker_running") as m_ensure:
            m_wait.return_value = {"job_id": "abc", "status": "pending"}
            route_execution(
                worker_url="http://127.0.0.1:54321",
                queue_config_path="q.json",
                submit_only=False,
                wait_job_id="abc",
                endpoint=None,
                submit_tool=None,
                submit_args={},
                status_tool=None,
                result_tool=None,
                headers={},
                output_dir="./output",
                output_file=None,
                auto_filename=False,
                poll_interval=2.0,
                max_polls=300,
                save_logs_to_dir=False,
                save_logs_inline=False,
            )
            m_ensure.assert_not_called()


# ── 5-9. E2E: --show-args with live worker ──────────────────────────

class TestE2EShowArgs(unittest.TestCase):
    """E2E test for include_args with a running worker."""

    @classmethod
    def setUpClass(cls):
        cls.port = get_free_port()
        cls.worker_url = f"http://127.0.0.1:{cls.port}"

        def mock_executor(job):
            cls.worker_app.store.update_status(
                job["id"], "completed",
                result=json.dumps({"urls": ["https://example.com/img.png"]}),
            )

        cls.worker_app = worker.WorkerApp(
            host="127.0.0.1",
            port=cls.port,
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
        cls.worker_app.start()
        for _ in range(50):
            try:
                requests.get(f"{cls.worker_url}/api/health", timeout=0.5)
                break
            except requests.ConnectionError:
                time.sleep(0.05)

    @classmethod
    def tearDownClass(cls):
        cls.worker_app.stop()

    def test_e2e_list_with_show_args(self):
        from mcp_async_call import _queue_submit_only, _queue_list
        _queue_submit_only(
            worker_url=self.worker_url,
            endpoint="http://mcp:8000/sse",
            submit_tool="gen",
            submit_args={"prompt": "e2e_args_test"},
            status_tool=None,
            result_tool=None,
            headers=None,
        )
        time.sleep(0.5)
        result = _queue_list(self.worker_url, include_args=True)
        self.assertGreaterEqual(result["total"], 1)
        found = any(
            j.get("args", {}).get("prompt") == "e2e_args_test"
            for j in result["jobs"]
        )
        self.assertTrue(found)

    def test_e2e_wait_with_show_args(self):
        from mcp_async_call import _queue_submit_only, _queue_wait
        submit_result = _queue_submit_only(
            worker_url=self.worker_url,
            endpoint="http://mcp:8000/sse",
            submit_tool="gen",
            submit_args={"prompt": "e2e_wait_args"},
            status_tool=None,
            result_tool=None,
            headers=None,
        )
        job_id = submit_result["job_id"]
        time.sleep(0.5)
        result = _queue_wait(self.worker_url, job_id, include_args=True)
        self.assertIn("args", result)
        self.assertEqual(result["args"]["prompt"], "e2e_wait_args")


class TestFindProjectRoot(unittest.TestCase):
    """Verify find_project_root() traverses upward to find .claude/ dir."""

    def setUp(self):
        import tempfile
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_finds_claude_dir(self):
        from mcp_worker_daemon import find_project_root
        # Create .claude/ at tmpdir level
        os.makedirs(os.path.join(self.tmpdir, ".claude"))
        deep = os.path.join(self.tmpdir, "a", "b", "c")
        os.makedirs(deep)
        result = find_project_root(os.path.join(deep, "dummy.py"))
        self.assertEqual(os.path.normpath(result), os.path.normpath(self.tmpdir))

    def test_finds_closest_claude_dir(self):
        from mcp_worker_daemon import find_project_root
        # Create .claude/ at two levels; should find closest one
        os.makedirs(os.path.join(self.tmpdir, ".claude"))
        nested = os.path.join(self.tmpdir, "sub")
        os.makedirs(os.path.join(nested, ".claude"))
        deep = os.path.join(nested, "a", "b")
        os.makedirs(deep)
        result = find_project_root(os.path.join(deep, "dummy.py"))
        self.assertEqual(os.path.normpath(result), os.path.normpath(nested))


if __name__ == "__main__":
    unittest.main()
