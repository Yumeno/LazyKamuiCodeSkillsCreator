# -*- coding: utf-8 -*-
"""Worker / client version handshake helpers.

Used by both ``mcp_async_call.py`` (subprocess client) and
``job_queue/client.py`` (in-process client) so the warning logic lives
in one place. (PR2 / #62; PHASE1_PLAN_v3 fix #5 / #18.)

Why this exists
---------------

The lazy-v2.x skill is distributed as a ``tar.gz`` extracted directly
into a project's ``.claude/skills/`` directory. A common upgrade path
is "overwrite-extract on top of an existing installation while the
previous worker daemon is still running in the background". The new
``.py`` files take effect for the *next* dispatch cycle, but the
already-running worker process keeps serving the API on port 54321.

If the new client speaks the new schema (e.g. PR1 introduced the
``category.limits.{cat}.{key}`` PATCH form) and the old worker silently
drops the unknown fields, the user has no way to notice that their
PATCH never landed. This module gives the client a way to detect the
skew and tell the user how to fix it (shut down the stale worker so a
fresh one spawns at the new version).

Phase 1 design choices
----------------------

* Warning, not failure: the client keeps working. Many requests on
  unchanged code paths still succeed against an older worker, and we
  do not want to break those for users who have not upgraded yet.
* Stderr ``print``, not logging: end-user CLI users typically do not
  configure logging handlers, and the warning needs to be visible.
* One-shot per process: avoid spamming a user who runs many small
  requests in a single Python session.
* ``api_compatible_versions`` is a forward-looking field: in the
  future we may decide that ``2.11.1`` clients are silently compatible
  with ``2.11.0`` workers. Phase 1 still warns on every mismatch, but
  the field is reserved in the wire format so we can widen the
  silent set without another breaking change.
"""
from __future__ import annotations

import logging
import sys
from typing import Iterable

logger = logging.getLogger(__name__)

# Process-local one-shot guard so we don't spam stderr.
_warned: bool = False


def reset_warned_for_tests() -> None:
    """Test helper: reset the one-shot guard.

    Tests need to be able to call ``warn_if_version_mismatch`` repeatedly
    inside the same Python process. Production code should never need
    this.
    """
    global _warned
    _warned = False


def get_worker_version(response_headers) -> str | None:
    """Extract ``X-Worker-Version`` from a response-headers-like mapping.

    Accepts either a ``requests.structures.CaseInsensitiveDict`` (which
    is what ``requests.Response.headers`` returns) or a plain dict.
    Returns ``None`` when the header is absent — typically because the
    worker pre-dates lazy-v2.11.0 and does not advertise its version.
    """
    if response_headers is None:
        return None
    try:
        return response_headers.get("X-Worker-Version")
    except Exception:
        return None


def warn_if_version_mismatch(
    worker_version: str | None,
    client_version: str,
    api_compatible_versions: Iterable[str] | None = None,
) -> None:
    """Emit a one-shot stderr warning if worker / client versions disagree.

    Behaviour matrix:

    +-----------------------------------+-------------------------------+
    | worker_version                    | what we do                    |
    +===================================+===============================+
    | ``None``                          | "did not advertise" warning — |
    |                                   | likely a pre-v2.11.0 worker   |
    +-----------------------------------+-------------------------------+
    | == client_version                 | silent (the happy path)       |
    +-----------------------------------+-------------------------------+
    | != client_version,                | warning + debug breadcrumb;   |
    | client_version in                 | the field is reserved for a   |
    | api_compatible_versions           | future "silent set" widening  |
    +-----------------------------------+-------------------------------+
    | != client_version, not compatible | "stale worker" warning        |
    +-----------------------------------+-------------------------------+

    The warning fires at most once per process lifetime to avoid noise
    when many requests are made in a single session.

    Note on the compat axis:
        ``api_compatible_versions`` comes from the WORKER and lists the
        client versions the worker considers compatible. We therefore
        check ``client_version in api_compatible_versions``, not the
        worker version. The wire format is currently filled in by
        ``GET /api/version`` only; the per-response ``X-Worker-Version``
        header carries just the version string.
    """
    global _warned
    if _warned:
        return

    if worker_version is None:
        print(
            f"[mcp-async-skill] WARNING: worker did not advertise "
            f"X-Worker-Version. You may be running a pre-v2.11.0 worker "
            f"against a v{client_version} client.\n"
            f"  Fix: curl -X POST http://127.0.0.1:54321/api/worker/shutdown\n"
            f"  Then re-run the client; a fresh worker will spawn at the "
            f"new version.",
            file=sys.stderr,
        )
        _warned = True
        return

    if worker_version == client_version:
        return  # exact match, silent

    api_set = set(api_compatible_versions or ())
    if client_version in api_set:
        # Reserved for a future "silent set" widening. Phase 1 still
        # warns, but we leave a debug breadcrumb so the wire format
        # is exercised end-to-end.
        logger.debug(
            "[mcp-async-skill] worker %s advertises client %s as "
            "api-compatible (suppressed-future warning hook)",
            worker_version, client_version,
        )

    print(
        f"[mcp-async-skill] WARNING: worker version {worker_version} != "
        f"client version {client_version}. The worker process may be "
        f"stale (e.g. you upgraded the skill but the previous worker "
        f"daemon is still running).\n"
        f"  Fix: curl -X POST http://127.0.0.1:54321/api/worker/shutdown\n"
        f"  Then re-run the client; a fresh worker will spawn at the new "
        f"version.",
        file=sys.stderr,
    )
    _warned = True
