# -*- coding: utf-8 -*-
"""Tests for retry helpers (_is_retryable, _get_retry_after, _with_retry).

Uses mock HTTP exceptions to verify retry behavior without network calls.
"""
import sys
import os
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from mcp_worker_daemon import _is_retryable, _get_retry_after, _with_retry


class _FakeResponse:
    """Minimal fake requests.Response for testing."""

    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeHTTPError(Exception):
    """Fake requests.HTTPError with a response attribute."""

    def __init__(self, response=None):
        super().__init__(f"HTTP {response.status_code}" if response else "HTTP Error")
        self.response = response


class _FakeConnectionError(Exception):
    """Fake requests.ConnectionError."""
    pass


class TestIsRetryable(unittest.TestCase):
    """Verify _is_retryable correctly identifies retryable errors."""

    def test_connection_error_is_retryable(self):
        exc = _FakeConnectionError("Connection refused")
        self.assertTrue(
            _is_retryable(exc, connection_error_cls=_FakeConnectionError,
                          http_error_cls=_FakeHTTPError)
        )

    def test_http_429_is_retryable(self):
        resp = _FakeResponse(429)
        exc = _FakeHTTPError(response=resp)
        self.assertTrue(
            _is_retryable(exc, connection_error_cls=_FakeConnectionError,
                          http_error_cls=_FakeHTTPError)
        )

    def test_http_503_is_retryable(self):
        resp = _FakeResponse(503)
        exc = _FakeHTTPError(response=resp)
        self.assertTrue(
            _is_retryable(exc, connection_error_cls=_FakeConnectionError,
                          http_error_cls=_FakeHTTPError)
        )

    def test_http_504_is_retryable(self):
        resp = _FakeResponse(504)
        exc = _FakeHTTPError(response=resp)
        self.assertTrue(
            _is_retryable(exc, connection_error_cls=_FakeConnectionError,
                          http_error_cls=_FakeHTTPError)
        )

    def test_http_400_not_retryable(self):
        resp = _FakeResponse(400)
        exc = _FakeHTTPError(response=resp)
        self.assertFalse(
            _is_retryable(exc, connection_error_cls=_FakeConnectionError,
                          http_error_cls=_FakeHTTPError)
        )

    def test_http_500_not_retryable(self):
        resp = _FakeResponse(500)
        exc = _FakeHTTPError(response=resp)
        self.assertFalse(
            _is_retryable(exc, connection_error_cls=_FakeConnectionError,
                          http_error_cls=_FakeHTTPError)
        )

    def test_generic_exception_not_retryable(self):
        exc = ValueError("something else")
        self.assertFalse(
            _is_retryable(exc, connection_error_cls=_FakeConnectionError,
                          http_error_cls=_FakeHTTPError)
        )

    def test_http_error_without_response_not_retryable(self):
        exc = _FakeHTTPError(response=None)
        self.assertFalse(
            _is_retryable(exc, connection_error_cls=_FakeConnectionError,
                          http_error_cls=_FakeHTTPError)
        )


class TestGetRetryAfter(unittest.TestCase):
    """Verify _get_retry_after extracts Retry-After header value."""

    def test_429_with_retry_after_header(self):
        resp = _FakeResponse(429, headers={"Retry-After": "5"})
        exc = _FakeHTTPError(response=resp)
        result = _get_retry_after(exc, http_error_cls=_FakeHTTPError)
        self.assertEqual(result, 5.0)

    def test_429_with_large_retry_after_capped(self):
        resp = _FakeResponse(429, headers={"Retry-After": "120"})
        exc = _FakeHTTPError(response=resp)
        result = _get_retry_after(exc, http_error_cls=_FakeHTTPError)
        self.assertEqual(result, 60.0)

    def test_429_without_retry_after_returns_none(self):
        resp = _FakeResponse(429, headers={})
        exc = _FakeHTTPError(response=resp)
        result = _get_retry_after(exc, http_error_cls=_FakeHTTPError)
        self.assertIsNone(result)

    def test_503_returns_none(self):
        resp = _FakeResponse(503, headers={"Retry-After": "10"})
        exc = _FakeHTTPError(response=resp)
        result = _get_retry_after(exc, http_error_cls=_FakeHTTPError)
        self.assertIsNone(result)

    def test_non_http_error_returns_none(self):
        exc = ValueError("not http")
        result = _get_retry_after(exc, http_error_cls=_FakeHTTPError)
        self.assertIsNone(result)

    def test_invalid_retry_after_value_returns_none(self):
        resp = _FakeResponse(429, headers={"Retry-After": "not-a-number"})
        exc = _FakeHTTPError(response=resp)
        result = _get_retry_after(exc, http_error_cls=_FakeHTTPError)
        self.assertIsNone(result)

    def test_http_error_without_response_returns_none(self):
        exc = _FakeHTTPError(response=None)
        result = _get_retry_after(exc, http_error_cls=_FakeHTTPError)
        self.assertIsNone(result)


class TestWithRetry(unittest.TestCase):
    """Verify _with_retry exponential backoff behavior."""

    def test_success_on_first_attempt(self):
        fn = MagicMock(return_value="ok")
        result = _with_retry(
            fn, max_retries=3,
            connection_error_cls=_FakeConnectionError,
            http_error_cls=_FakeHTTPError,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(fn.call_count, 1)

    def test_success_after_retries(self):
        call_count = [0]

        def failing_then_ok():
            call_count[0] += 1
            if call_count[0] < 3:
                raise _FakeConnectionError("retry me")
            return "ok"

        result = _with_retry(
            failing_then_ok, max_retries=3,
            backoff_base=[0.01, 0.01, 0.01],
            connection_error_cls=_FakeConnectionError,
            http_error_cls=_FakeHTTPError,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(call_count[0], 3)

    def test_non_retryable_raises_immediately(self):
        fn = MagicMock(side_effect=ValueError("not retryable"))
        with self.assertRaises(ValueError):
            _with_retry(
                fn, max_retries=3,
                connection_error_cls=_FakeConnectionError,
                http_error_cls=_FakeHTTPError,
            )
        self.assertEqual(fn.call_count, 1)

    def test_max_retries_exceeded_raises(self):
        fn = MagicMock(side_effect=_FakeConnectionError("always fail"))
        with self.assertRaises(_FakeConnectionError):
            _with_retry(
                fn, max_retries=2,
                backoff_base=[0.01, 0.01],
                connection_error_cls=_FakeConnectionError,
                http_error_cls=_FakeHTTPError,
            )
        self.assertEqual(fn.call_count, 3)  # 1 initial + 2 retries

    def test_429_uses_retry_after(self):
        resp = _FakeResponse(429, headers={"Retry-After": "0.05"})
        call_count = [0]

        def failing_then_ok():
            call_count[0] += 1
            if call_count[0] < 2:
                raise _FakeHTTPError(response=resp)
            return "ok"

        start = time.monotonic()
        result = _with_retry(
            failing_then_ok, max_retries=3,
            backoff_base=[10.0, 10.0, 10.0],  # Large backoff to prove Retry-After is used
            connection_error_cls=_FakeConnectionError,
            http_error_cls=_FakeHTTPError,
        )
        elapsed = time.monotonic() - start
        self.assertEqual(result, "ok")
        # Should have waited ~0.05s (Retry-After), not 10s (backoff)
        self.assertLess(elapsed, 1.0)

    def test_on_retry_callback(self):
        retry_info = []

        def on_retry(attempt, exc, wait):
            retry_info.append({"attempt": attempt, "exc_type": type(exc).__name__, "wait": wait})

        fn = MagicMock(side_effect=[
            _FakeConnectionError("fail1"),
            _FakeConnectionError("fail2"),
            "ok",
        ])
        result = _with_retry(
            fn, max_retries=3,
            backoff_base=[0.01, 0.01, 0.01],
            on_retry=on_retry,
            connection_error_cls=_FakeConnectionError,
            http_error_cls=_FakeHTTPError,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(len(retry_info), 2)
        self.assertEqual(retry_info[0]["attempt"], 0)
        self.assertEqual(retry_info[1]["attempt"], 1)


if __name__ == "__main__":
    unittest.main()
