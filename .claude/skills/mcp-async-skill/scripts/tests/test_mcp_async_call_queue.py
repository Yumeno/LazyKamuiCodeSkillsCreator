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
                id_param_name="request_id",
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
                id_param_name="request_id",
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
                id_param_name="request_id",
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
                id_param_name="request_id",
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
                id_param_name="request_id",
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
                id_param_name="request_id",
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


if __name__ == "__main__":
    unittest.main()
