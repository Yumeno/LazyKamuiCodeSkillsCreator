# -*- coding: utf-8 -*-
"""SQLite schema migrations driven by ``PRAGMA user_version``.

This module is the migration scaffold for the queue-system DB. It does
not yet add or remove any columns — its first migration only stamps
existing v0 databases as ``user_version=1`` after a quick sanity check
on column names. The point is to establish the framework now so that
when a real schema change is needed (PR3 ships the foundation, not the
schema change itself), the rollout can be expressed as a small Python
function and runs deterministically against every existing DB on
worker startup.

Pattern
-------

* Each migration is a Python function ``migrate_NNN(conn)`` taking a
  ``sqlite3.Connection`` and applying its DDL/DML.
* :data:`MIGRATIONS` is an ordered ``[(target_version, function), ...]``
  list. Tuples are processed in order on every startup;
  ``apply_migrations`` runs each whose ``target_version`` is greater
  than the current ``PRAGMA user_version`` and bumps the pragma after
  the function returns.

Lifecycle contract (PR3 / PHASE1_PLAN_v3 fix #6)
------------------------------------------------

``JobStore`` runs ``CREATE TABLE IF NOT EXISTS jobs (...)`` *before*
calling :func:`apply_migrations`. Migration ``001_initial`` therefore
relies on the ``jobs`` table existing when it inspects
``PRAGMA table_info(jobs)``. There is no graceful "table not present"
branch — that case is treated as a hard initialization bug and raised.

Phase 1 scope (PR3 / PHASE1_PLAN_v3 fix #7)
-------------------------------------------

Migration 001 only verifies that the **required column names exist**
on the ``jobs`` table. It does NOT validate column types, NOT NULL
constraints, or DEFAULT clauses. Tightening that check is left to a
future migration when an actual reason arises; what matters now is
that the framework picks up new migrations without having to rewrite
this file.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Migration 001 — bootstrap existing v0 DBs to user_version=1
# ---------------------------------------------------------------------

# Required columns on the ``jobs`` table after the initial
# ``CREATE TABLE IF NOT EXISTS`` runs in ``db.py``. We check NAMES only
# (PHASE1_PLAN_v3 fix #7); types, NOT NULL, and DEFAULT clauses are
# intentionally NOT validated by Phase 1's migration 001.
EXPECTED_JOBS_COLUMNS: frozenset[str] = frozenset({
    "id", "endpoint", "submit_tool", "args",
    "status_tool", "result_tool", "headers",
    "session_id", "remote_job_id",
    "status", "result", "error",
    "created_at", "updated_at",
})


def migrate_001_initial(conn: sqlite3.Connection) -> None:
    """Stamp existing or freshly-created DBs as ``user_version=1``.

    Precondition (PHASE1_PLAN_v3 fix #6):
        :class:`~job_queue.db.JobStore` MUST have already executed
        ``CREATE TABLE IF NOT EXISTS jobs (...)`` before calling this.
        We therefore expect the ``jobs`` table to exist and only check
        that its column names cover :data:`EXPECTED_JOBS_COLUMNS`.

    Behaviour:
        * If the ``jobs`` table is missing required columns →
          raise :class:`RuntimeError` ("schema drift detected").
        * If extra unknown columns exist (e.g. from a future migration
          we don't know about) → log a warning but allow startup.
          This is the forward-compat hook: a newer worker can add
          columns and an older worker still boots, just without using
          them.

    Note (PHASE1_PLAN_v3 fix #7):
        "Required columns exist" is the entire scope of this check.
        Column types, NOT NULL, and DEFAULT clauses are NOT validated.
        Future migrations may tighten this, but migration 001 is
        deliberately lenient so that old DBs created by SQLite's
        type-affinity quirks still pass.
    """
    cur = conn.execute("PRAGMA table_info(jobs)")
    actual_columns = {row[1] for row in cur.fetchall()}

    # The precondition above says JobStore created the table. If somehow
    # it didn't, that is a hard error rather than a graceful skip — the
    # framework's whole correctness story depends on this contract.
    if not actual_columns:
        raise RuntimeError(
            "[migrations] jobs table does not exist after "
            "CREATE TABLE IF NOT EXISTS. This indicates a serious DB "
            "initialization bug — please file an issue."
        )

    missing = EXPECTED_JOBS_COLUMNS - actual_columns
    extra = actual_columns - EXPECTED_JOBS_COLUMNS

    if missing:
        raise RuntimeError(
            f"[migrations] DB schema drift detected: jobs table is "
            f"missing required columns {sorted(missing)}. "
            f"This DB was likely created by an unsupported version. "
            f"Back up the DB, move it aside, then restart the worker "
            f"to create a fresh DB."
        )
    if extra:
        # Forward-compat: log but allow. A future migration may have
        # added columns that this worker version doesn't know about.
        logger.warning(
            "[migrations] jobs table has extra columns not known to "
            "this worker version: %s",
            sorted(extra),
        )


# ---------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------

# Explicit ``Callable`` annotation (PHASE1_PLAN_v3 fix #15) so static
# checkers and IDEs catch signature drift on future migrations.
MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, migrate_001_initial),
]


# ---------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------


def get_user_version(conn: sqlite3.Connection) -> int:
    """Read ``PRAGMA user_version`` from the connection."""
    cur = conn.execute("PRAGMA user_version")
    return cur.fetchone()[0]


def set_user_version(conn: sqlite3.Connection, version: int) -> None:
    """Write ``PRAGMA user_version``.

    SQLite's ``PRAGMA`` does not bind ``?`` parameters, so the integer
    is interpolated. ``int(version)`` makes that safe.
    """
    conn.execute(f"PRAGMA user_version = {int(version)}")


def apply_migrations(conn: sqlite3.Connection) -> None:
    """Run every migration whose target > current user_version.

    Called by :class:`~job_queue.db.JobStore` during ``__init__``,
    *after* the base ``CREATE TABLE IF NOT EXISTS`` so that
    :func:`migrate_001_initial` can inspect the table.

    Each migration commits via :func:`set_user_version` only after its
    body returns successfully. A raised exception leaves the
    ``user_version`` pragma at the previous value, so re-running on
    the next worker start re-attempts the same migration rather than
    silently skipping it.
    """
    current = get_user_version(conn)
    for target, fn in MIGRATIONS:
        if target <= current:
            continue
        logger.info(
            "[migrations] Applying migration %03d (%s)",
            target, fn.__name__,
        )
        fn(conn)
        set_user_version(conn, target)
        conn.commit()
        logger.info("[migrations] user_version is now %d", target)
