# -*- coding: utf-8 -*-
"""MCP session manager with per-endpoint caching and single-flight re-init.

Caches ``Mcp-Session-Id`` per (endpoint, auth-context) so that multiple
jobs sharing the same endpoint reuse one session instead of calling
``initialize`` for every request.

When the server returns **HTTP 404** (session expired per MCP spec), the
caller should call :meth:`invalidate` with the *generation* it received
from :meth:`get_session`.  The next :meth:`get_session` call will
re-initialize transparently, and only one thread performs the actual
network request (single-flight pattern via ``threading.Condition``).
"""
import logging
import threading
import time
import uuid
from typing import Any

import requests

logger = logging.getLogger(__name__)


class _SessionEntry:
    """In-memory state for one (endpoint, auth-context) pair."""

    __slots__ = ("session_id", "generation", "refreshing", "last_used", "endpoint", "headers")

    def __init__(self, endpoint: str, headers: dict):
        self.session_id: str | None = None
        self.generation: int = 0
        self.refreshing: bool = False
        self.last_used: float = 0.0
        self.endpoint = endpoint
        self.headers = dict(headers)


def _cache_key(endpoint: str, headers: dict | None) -> tuple:
    """Build a hashable key from endpoint + auth headers."""
    if headers is None:
        return (endpoint,)
    # Only include auth-relevant headers in the key
    auth_items = tuple(sorted(
        (k, v) for k, v in headers.items()
        if k.lower() not in ("content-type", "mcp-session-id")
    ))
    return (endpoint, auth_items)


class SessionManager:
    """Thread-safe MCP session cache with single-flight re-initialization."""

    def __init__(self):
        self._sessions: dict[tuple, _SessionEntry] = {}
        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)

    def get_session(self, endpoint: str, headers: dict | None = None) -> tuple[str, int]:
        """Return ``(session_id, generation)`` for the endpoint.

        If no valid session exists, performs ``initialize`` (only one thread
        does so; others wait).  Raises on initialize failure.
        """
        key = _cache_key(endpoint, headers)

        with self._condition:
            entry = self._sessions.get(key)
            if entry is None:
                entry = _SessionEntry(endpoint, headers or {})
                self._sessions[key] = entry

            # Fast path: valid session exists
            if entry.session_id is not None:
                entry.last_used = time.monotonic()
                return entry.session_id, entry.generation

            # Another thread is already refreshing — wait for it
            if entry.refreshing:
                while entry.refreshing:
                    self._condition.wait(timeout=120)
                if entry.session_id is not None:
                    entry.last_used = time.monotonic()
                    return entry.session_id, entry.generation
                raise RuntimeError(
                    f"Session initialize failed for {endpoint} (waited for another thread)"
                )

            # We are the refresh owner
            entry.refreshing = True

        # --- Outside lock: perform network I/O ---
        try:
            session_id = self._do_initialize(endpoint, headers or {})
        except Exception:
            with self._condition:
                entry.refreshing = False
                self._condition.notify_all()
            raise

        with self._condition:
            entry.session_id = session_id
            entry.generation += 1
            entry.refreshing = False
            entry.last_used = time.monotonic()
            gen = entry.generation
            self._condition.notify_all()

        logger.info("[SessionManager] Session initialized for %s (gen=%d)", endpoint, gen)
        return session_id, gen

    def invalidate(self, endpoint: str, headers: dict | None, generation: int):
        """Mark the session as expired if *generation* matches the current one.

        This prevents a stale failure from invalidating a freshly acquired
        session (the generation will have advanced).
        """
        key = _cache_key(endpoint, headers)
        with self._condition:
            entry = self._sessions.get(key)
            if entry is None:
                return
            if entry.generation == generation:
                entry.session_id = None
                logger.info(
                    "[SessionManager] Session invalidated for %s (gen=%d)",
                    endpoint, generation,
                )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _do_initialize(endpoint: str, headers: dict) -> str:
        """Send MCP ``initialize`` and return the server-assigned session ID."""
        initial_session_id = str(uuid.uuid4())
        req_headers = {
            "Content-Type": "application/json",
            **headers,
            "Mcp-Session-Id": initial_session_id,
        }

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-async-client", "version": "1.0.0"},
            },
        }

        resp = requests.post(endpoint, json=payload, headers=req_headers, timeout=60)
        resp.raise_for_status()

        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"Initialize Error: {result['error']}")

        session_id = resp.headers.get("Mcp-Session-Id", initial_session_id)
        return session_id
