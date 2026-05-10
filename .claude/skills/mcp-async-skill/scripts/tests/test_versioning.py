# -*- coding: utf-8 -*-
"""Tests for ``job_queue.versioning`` (PR2 / #62).

Covers:

* ``get_worker_version`` extracts the ``X-Worker-Version`` header from
  both ``requests``-style ``CaseInsensitiveDict`` and plain ``dict``
  shapes; returns ``None`` when missing.
* ``warn_if_version_mismatch`` is silent when versions match exactly.
* It emits a "did not advertise" warning when the header is absent
  (pre-v2.11.0 worker scenario).
* It emits a "stale worker" warning when versions differ.
* The warning fires at most once per process. ``reset_warned_for_tests``
  is the documented escape hatch for repeated assertions.
* The ``api_compatible_versions`` field is currently informational
  (Phase 1 still warns) but a debug breadcrumb is logged when the
  client version is in the worker's compatible set.
"""
import io
import logging
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue import versioning  # noqa: E402


class _CaseInsensitiveDict(dict):
    """Tiny stand-in for ``requests.structures.CaseInsensitiveDict`` so
    we don't pull in the real dependency just for header lookups."""

    def __init__(self, data):
        super().__init__()
        self._lower_to_real = {}
        for k, v in data.items():
            self._lower_to_real[k.lower()] = k
            self[k] = v

    def get(self, key, default=None):
        real = self._lower_to_real.get(key.lower())
        if real is None:
            return default
        return self[real]


class TestGetWorkerVersion(unittest.TestCase):
    def test_extracts_from_plain_dict(self):
        self.assertEqual(
            versioning.get_worker_version({"X-Worker-Version": "lazy-v2.11.0"}),
            "lazy-v2.11.0",
        )

    def test_extracts_from_case_insensitive_dict(self):
        # requests' CaseInsensitiveDict accepts lookups in any case.
        headers = _CaseInsensitiveDict({"x-worker-version": "lazy-v2.11.0"})
        self.assertEqual(
            versioning.get_worker_version(headers), "lazy-v2.11.0",
        )

    def test_returns_none_when_header_absent(self):
        self.assertIsNone(versioning.get_worker_version({}))

    def test_returns_none_when_headers_is_none(self):
        self.assertIsNone(versioning.get_worker_version(None))

    def test_returns_none_on_unsupported_object(self):
        # An object without ``.get`` should not crash the client.
        class NoGet:
            pass

        self.assertIsNone(versioning.get_worker_version(NoGet()))


class TestWarnIfVersionMismatch(unittest.TestCase):
    def setUp(self):
        versioning.reset_warned_for_tests()
        self._stderr = io.StringIO()
        self._stderr_patch = patch("sys.stderr", self._stderr)
        self._stderr_patch.start()

    def tearDown(self):
        self._stderr_patch.stop()
        versioning.reset_warned_for_tests()

    def test_silent_on_exact_match(self):
        versioning.warn_if_version_mismatch("2.11.0", "2.11.0")
        self.assertEqual(self._stderr.getvalue(), "")

    def test_warns_when_worker_version_absent(self):
        """``None`` worker version → "did not advertise" warning."""
        versioning.warn_if_version_mismatch(None, "2.11.0")
        out = self._stderr.getvalue()
        self.assertIn("did not advertise", out)
        self.assertIn("2.11.0", out)
        self.assertIn("/api/worker/shutdown", out)

    def test_warns_on_mismatch(self):
        versioning.warn_if_version_mismatch("2.10.1", "2.11.0")
        out = self._stderr.getvalue()
        self.assertIn("2.10.1", out)
        self.assertIn("2.11.0", out)
        self.assertIn("stale", out.lower())
        self.assertIn("/api/worker/shutdown", out)

    def test_warning_fires_at_most_once_per_process(self):
        """Subsequent calls — even with different values — are silent
        until ``reset_warned_for_tests`` is called."""
        versioning.warn_if_version_mismatch("2.10.1", "2.11.0")
        first = self._stderr.getvalue()
        self.assertNotEqual(first, "")

        versioning.warn_if_version_mismatch(None, "2.11.0")
        versioning.warn_if_version_mismatch("2.10.0", "2.11.0")
        second = self._stderr.getvalue()
        self.assertEqual(second, first,
                         "Warning must fire only once per process")

    def test_reset_helper_re_arms_the_guard(self):
        versioning.warn_if_version_mismatch("2.10.1", "2.11.0")
        self.assertNotEqual(self._stderr.getvalue(), "")

        versioning.reset_warned_for_tests()
        before = self._stderr.getvalue()
        versioning.warn_if_version_mismatch("2.10.1", "2.11.0")
        after = self._stderr.getvalue()
        self.assertGreater(len(after), len(before),
                           "Reset must allow a second warning")

    def test_api_compatible_versions_logs_debug_breadcrumb(self):
        """When client_version is in the worker's compatible set, a
        debug breadcrumb is logged. Phase 1 still emits the warning."""
        with self.assertLogs("job_queue.versioning", level="DEBUG") as cm:
            versioning.warn_if_version_mismatch(
                worker_version="2.11.1",
                client_version="2.11.0",
                api_compatible_versions=["2.11.0", "2.11.1"],
            )

        debug_lines = [m for m in cm.output if "DEBUG" in m]
        self.assertTrue(any("api-compatible" in m for m in debug_lines),
                        f"Expected api-compatible debug log, got: {cm.output}")
        # Phase 1: warning still goes to stderr
        self.assertIn("WARNING", self._stderr.getvalue())

    def test_api_compatible_versions_does_not_break_when_empty(self):
        """An empty / None compat list must not crash the comparison."""
        versioning.warn_if_version_mismatch(
            worker_version="2.10.1",
            client_version="2.11.0",
            api_compatible_versions=None,
        )
        # Just needs to not raise; the warning content is covered above.
        self.assertIn("stale", self._stderr.getvalue().lower())


class TestVersionConstantPresent(unittest.TestCase):
    """The ``__version__`` constant must exist on ``job_queue`` so that
    both ``mcp_async_call.py`` and ``client.py`` can import a single
    canonical client version string."""

    def test_version_attribute_exists(self):
        from job_queue import __version__
        self.assertIsInstance(__version__, str)
        self.assertGreater(len(__version__), 0)


if __name__ == "__main__":
    unittest.main()
