# -*- coding: utf-8 -*-
"""Tests for worker version handshake (PR2 / #62).

Covers:

* ``X-Worker-Version`` header is present on **every** response, not
  only ``200``: legacy dashboards / older clients may hit a 404 first
  while feature-detecting endpoints, and the version still needs to
  reach them. The same applies to 400 responses (invalid JSON in
  PATCH bodies).
* ``GET /api/version`` returns ``version``, ``api_compatible_versions``,
  and ``server_time_utc``. ``api_compatible_versions`` includes the
  worker's own version (Phase 1 baseline; the field exists so a
  future client can downgrade the mismatch warning to debug).
* The header value matches ``job_queue.__version__``, the same
  constant the client compares against.
"""
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

from job_queue import __version__ as WORKER_VERSION
from job_queue import worker


def get_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _WorkerBase(unittest.TestCase):
    def setUp(self):
        self.port = get_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

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
            job_executor=lambda job: None,
            idle_timeout=0,
        )
        self.worker_app.start()

        for _ in range(50):
            try:
                requests.get(f"{self.base_url}/api/health", timeout=0.5)
                break
            except requests.ConnectionError:
                time.sleep(0.05)

    def tearDown(self):
        self.worker_app.stop()


class TestXWorkerVersionHeader(_WorkerBase):
    """The header is attached to every response shape worker.py emits."""

    def test_header_present_on_200_health(self):
        resp = requests.get(f"{self.base_url}/api/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Worker-Version"), WORKER_VERSION)

    def test_header_present_on_200_config(self):
        resp = requests.get(f"{self.base_url}/api/config")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Worker-Version"), WORKER_VERSION)

    def test_header_present_on_404(self):
        """A request to an unknown path must still carry the version
        header. Old clients feature-detect with HEAD/GET probes and
        rely on the header to learn the worker version even when the
        probed path doesn't exist."""
        resp = requests.get(f"{self.base_url}/api/this-path-does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers.get("X-Worker-Version"), WORKER_VERSION)

    def test_header_present_on_400_invalid_json_patch(self):
        """``PATCH /api/config`` with a malformed body returns 400.
        The header must still be set so the client can warn about a
        version mismatch even when its request was rejected."""
        resp = requests.patch(
            f"{self.base_url}/api/config",
            data=b"this is not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.headers.get("X-Worker-Version"), WORKER_VERSION)


class TestApiVersionEndpoint(_WorkerBase):
    def test_returns_version(self):
        resp = requests.get(f"{self.base_url}/api/version")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["version"], WORKER_VERSION)

    def test_includes_api_compatible_versions(self):
        """The compat list must at least include the worker's own
        version so a future client can short-circuit on exact match
        via the same code path."""
        body = requests.get(f"{self.base_url}/api/version").json()
        self.assertIn("api_compatible_versions", body)
        self.assertIsInstance(body["api_compatible_versions"], list)
        self.assertIn(WORKER_VERSION, body["api_compatible_versions"])

    def test_includes_server_time_utc(self):
        body = requests.get(f"{self.base_url}/api/version").json()
        self.assertIn("server_time_utc", body)
        # ISO 8601 with Z suffix, e.g. "2026-05-10T03:14:15.123456Z"
        self.assertTrue(body["server_time_utc"].endswith("Z"),
                        f"Expected Z suffix, got {body['server_time_utc']!r}")

    def test_version_endpoint_also_advertises_header(self):
        """Belt-and-suspenders: the body and the header agree."""
        resp = requests.get(f"{self.base_url}/api/version")
        self.assertEqual(resp.headers.get("X-Worker-Version"), WORKER_VERSION)
        self.assertEqual(resp.json()["version"], WORKER_VERSION)


class TestHealthProbeTriggersVersionCheck(_WorkerBase):
    """``is_worker_running()`` is called by ``_ensure_worker_running``
    on every CLI invocation. It must run the same X-Worker-Version
    check as the job-submission paths, otherwise the most common
    entry point would silently miss a stale-worker skew until the
    user submitted an actual job.
    """

    def test_is_worker_running_calls_check_worker_version(self):
        from job_queue import client as client_mod
        from job_queue import versioning

        # Reset the one-shot guard so the warning would fire on mismatch
        versioning.reset_warned_for_tests()

        captured = []

        def spy(response):
            captured.append(response)

        # Replace the helper, call is_worker_running, restore.
        original = client_mod._check_worker_version
        client_mod._check_worker_version = spy
        try:
            self.assertTrue(client_mod.is_worker_running(self.base_url))
        finally:
            client_mod._check_worker_version = original

        self.assertEqual(len(captured), 1,
                         "is_worker_running must invoke _check_worker_version "
                         "exactly once per probe")


class TestVersionConstantWiring(unittest.TestCase):
    """The version string the worker advertises must come from the
    same module-level constant the client compares against. This pins
    down the contract that a single source of truth is used."""

    def test_worker_imports_version_from_job_queue_init(self):
        from job_queue.worker import WORKER_VERSION as worker_const
        from job_queue import __version__ as init_const
        self.assertEqual(worker_const, init_const)

    def test_client_module_imports_same_version(self):
        # mcp_async_call.py and job_queue/client.py both alias
        # job_queue.__version__ as CLIENT_VERSION; verify the alias.
        import importlib
        # mcp_async_call sits one level up in scripts/
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        mcp_mod = importlib.import_module("mcp_async_call")
        from job_queue import __version__ as init_const
        self.assertEqual(mcp_mod.CLIENT_VERSION, init_const)

        from job_queue import client as client_mod
        self.assertEqual(client_mod.CLIENT_VERSION, init_const)


if __name__ == "__main__":
    unittest.main()
