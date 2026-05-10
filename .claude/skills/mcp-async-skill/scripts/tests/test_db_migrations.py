# -*- coding: utf-8 -*-
"""Tests for the SQLite migration framework (PR3 / #63).

Covers:

* Fresh DB created via ``JobStore`` ends up at ``user_version=1``
  thanks to the framework running on top of the base schema.
* Pre-PR3 DBs with ``user_version=0`` and complete column set are
  upgraded to ``user_version=1`` on next worker boot — without any
  data loss.
* Pre-PR3 DBs that are missing a required column are rejected with a
  ``RuntimeError`` (we do not silently mutate user data).
* Extra columns on the table (forward-compat: a future migration may
  add columns) only emit a warning; the worker still boots.
* The migration is idempotent — calling
  ``_init_schema_and_migrations`` repeatedly does not re-run
  migrations or bump the pragma further.
* Migration 001 only validates **column names** — it does NOT inspect
  types, NOT NULL, or DEFAULT clauses (PHASE1_PLAN_v3 fix #7). This
  is pinned so a well-meaning future change doesn't accidentally
  tighten the check and reject existing v0 DBs.
* The migration registry has the explicit
  ``Callable[[sqlite3.Connection], None]`` type expected by
  PHASE1_PLAN_v3 fix #15, so type-checkers catch signature drift on
  future migrations.
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue import db, migrations


class _TmpDBMixin:
    """Provides ``self.db_path`` pointing at a freshly-created tempfile
    that is removed in ``tearDown``. Tests use this when they need to
    open the same DB twice (simulating a worker restart).

    Tests should track every connection / store they open in
    ``self._opened`` so ``tearDown`` can guarantee they are closed
    before the file is deleted (Windows holds file locks until then)."""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.db_path)  # JobStore will recreate
        self._opened: list = []

    def _track(self, opened):
        """Register a connection/store so tearDown can close it."""
        self._opened.append(opened)
        return opened

    def tearDown(self):
        for opened in self._opened:
            try:
                opened.close()
            except Exception:
                pass
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(self.db_path + suffix)
            except (FileNotFoundError, PermissionError):
                pass


class TestFreshDBStartsAtVersion1(_TmpDBMixin, unittest.TestCase):
    def test_fresh_db_is_stamped_v1(self):
        store = self._track(db.JobStore(self.db_path))
        version = store.conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)

    def test_in_memory_db_is_stamped_v1(self):
        """``:memory:`` DBs (used heavily in the test suite) follow the
        same code path and should also end at user_version=1."""
        store = db.JobStore(":memory:")
        version = store.conn.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 1)


class TestPreV3DBUpgrade(_TmpDBMixin, unittest.TestCase):
    """A DB that was created by lazy-v2.10.x (pre-PR3) has a complete
    column set but ``user_version=0``. Booting a JobStore against it
    must upgrade it in place without touching data."""

    def _create_legacy_v0_db(self) -> str:
        """Mimic the schema lazy-v2.10.x ships, with ``user_version=0``."""
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                submit_tool TEXT NOT NULL,
                args TEXT NOT NULL,
                status_tool TEXT,
                result_tool TEXT,
                headers TEXT,
                session_id TEXT,
                remote_job_id TEXT,
                status TEXT DEFAULT 'pending',
                result TEXT,
                error TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.execute(
            "INSERT INTO jobs (id, endpoint, submit_tool, args, status, "
            "created_at, updated_at) VALUES "
            "('job-legacy-1', 'http://a:8000', 'gen', '{}', 'completed', "
            "'2026-04-01T00:00:00.000', '2026-04-01T00:00:01.000')"
        )
        conn.commit()
        # Confirm the precondition before opening JobStore over it
        self.assertEqual(
            conn.execute("PRAGMA user_version").fetchone()[0], 0,
            "Legacy DB precondition: user_version must start at 0",
        )
        conn.close()
        return self.db_path

    def test_legacy_v0_db_upgrades_to_v1_without_data_loss(self):
        self._create_legacy_v0_db()

        store = self._track(db.JobStore(self.db_path))
        self.assertEqual(
            store.conn.execute("PRAGMA user_version").fetchone()[0], 1,
        )
        # Existing row is untouched
        row = store.conn.execute(
            "SELECT id, status FROM jobs WHERE id = 'job-legacy-1'"
        ).fetchone()
        self.assertIsNotNone(row, "Legacy data must survive the upgrade")
        self.assertEqual(row[0], "job-legacy-1")
        self.assertEqual(row[1], "completed")

    def test_already_v1_db_is_not_re_migrated(self):
        """Booting a JobStore against a DB already at v1 must not call
        migration 001 again. We assert this by checking the pragma
        stays at 1 and no exceptions fire from a re-invocation."""
        store1 = db.JobStore(self.db_path)
        store1.close()

        store2 = self._track(db.JobStore(self.db_path))
        self.assertEqual(
            store2.conn.execute("PRAGMA user_version").fetchone()[0], 1,
        )


class TestMissingColumnRejection(_TmpDBMixin, unittest.TestCase):
    """A pre-PR3 DB that is *missing* a required column must NOT be
    silently mutated. Phase 1's design choice is to refuse the upgrade
    and tell the user to start over with a fresh DB rather than risk
    losing job data through automated schema mutation."""

    def _create_broken_v0_db(self) -> str:
        # Missing ``remote_job_id`` and ``error``
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                submit_tool TEXT NOT NULL,
                args TEXT NOT NULL,
                status_tool TEXT,
                result_tool TEXT,
                headers TEXT,
                session_id TEXT,
                status TEXT DEFAULT 'pending',
                result TEXT,
                created_at TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
        conn.close()
        return self.db_path

    def test_missing_column_raises(self):
        self._create_broken_v0_db()

        with self.assertRaises(RuntimeError) as ctx:
            db.JobStore(self.db_path)

        msg = str(ctx.exception)
        self.assertIn("schema drift", msg)
        # The user-facing message must list the missing columns so the
        # operator can confirm what's wrong before discarding the DB.
        self.assertIn("remote_job_id", msg)
        self.assertIn("error", msg)
        self.assertIn("Back up the DB", msg)


class TestExtraColumnsAllowedWithWarning(_TmpDBMixin, unittest.TestCase):
    """Forward-compat hook: a DB that has ALL required columns plus
    extras (e.g. created by a future worker version) must boot
    successfully. A warning is logged so an operator running an old
    worker against a newer DB knows."""

    def _create_v0_db_with_extra_columns(self) -> str:
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE jobs (
                id TEXT PRIMARY KEY,
                endpoint TEXT NOT NULL,
                submit_tool TEXT NOT NULL,
                args TEXT NOT NULL,
                status_tool TEXT,
                result_tool TEXT,
                headers TEXT,
                session_id TEXT,
                remote_job_id TEXT,
                status TEXT DEFAULT 'pending',
                result TEXT,
                error TEXT,
                created_at TEXT,
                updated_at TEXT,
                future_priority INTEGER DEFAULT 0,
                future_tags TEXT
            )
        """)
        conn.commit()
        conn.close()
        return self.db_path

    def test_extra_columns_warn_but_do_not_block_startup(self):
        self._create_v0_db_with_extra_columns()

        with self.assertLogs("job_queue.migrations", level="WARNING") as cm:
            store = self._track(db.JobStore(self.db_path))
            # Confirm startup actually completed
            self.assertEqual(
                store.conn.execute("PRAGMA user_version").fetchone()[0], 1,
            )

        warning_text = "\n".join(cm.output)
        self.assertIn("extra columns", warning_text)
        self.assertIn("future_priority", warning_text)
        self.assertIn("future_tags", warning_text)


class TestColumnNameOnlyValidation(_TmpDBMixin, unittest.TestCase):
    """PHASE1_PLAN_v3 fix #7: migration 001 must validate column names
    only — types / NOT NULL / DEFAULT must NOT be inspected. This pins
    the design so a future change doesn't accidentally tighten the
    check and reject existing user DBs.

    We give the columns the wrong types (e.g. ``id INTEGER`` instead of
    ``id TEXT``), drop NOT NULL constraints, and remove DEFAULT
    clauses. Migration 001 must still accept the table.
    """

    def _create_v0_db_with_typed_drift(self) -> str:
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY,
                endpoint BLOB,
                submit_tool BLOB,
                args BLOB,
                status_tool TEXT,
                result_tool TEXT,
                headers TEXT,
                session_id TEXT,
                remote_job_id TEXT,
                status TEXT,
                result TEXT,
                error TEXT,
                created_at REAL,
                updated_at REAL
            )
        """)
        conn.commit()
        conn.close()
        return self.db_path

    def test_type_drift_does_not_block_startup(self):
        self._create_v0_db_with_typed_drift()

        # No exception, no missing-column error
        store = self._track(db.JobStore(self.db_path))
        self.assertEqual(
            store.conn.execute("PRAGMA user_version").fetchone()[0], 1,
        )


class TestMigrationsRegistry(unittest.TestCase):
    """The registry is the one thing that future migrations append to.
    Pin its shape so a typo on a future migration trips a test rather
    than silently failing to apply."""

    def test_registry_has_001_initial(self):
        targets = [t for (t, _) in migrations.MIGRATIONS]
        self.assertEqual(targets, [1])

    def test_registry_callables_take_a_connection(self):
        """The Callable annotation says ``Callable[[sqlite3.Connection],
        None]``. We verify the runtime contract by inspecting each
        callable accepts exactly one positional argument."""
        import inspect
        for target, fn in migrations.MIGRATIONS:
            sig = inspect.signature(fn)
            params = list(sig.parameters.values())
            self.assertEqual(
                len(params), 1,
                f"Migration {target} ({fn.__name__}) must take exactly "
                f"one positional argument (sqlite3.Connection), got "
                f"{len(params)}",
            )

    def test_targets_strictly_increasing(self):
        """Migrations must be applied in order. A duplicate or
        decreasing target indicates a registry mistake."""
        targets = [t for (t, _) in migrations.MIGRATIONS]
        for prev, cur in zip(targets, targets[1:]):
            self.assertLess(prev, cur,
                            f"MIGRATIONS targets must strictly increase: "
                            f"{prev} >= {cur}")


class TestApplyMigrationsDriver(unittest.TestCase):
    """The ``apply_migrations`` driver behaviour, exercised directly on
    a connection without going through JobStore."""

    def test_failed_migration_does_not_bump_pragma(self):
        """If a migration raises, ``user_version`` must NOT be advanced
        — the next startup should re-attempt it."""
        conn = sqlite3.connect(":memory:")
        # Bypass the lifecycle contract so migrate_001_initial can fail
        # naturally because there's no ``jobs`` table.
        try:
            with self.assertRaises(RuntimeError):
                migrations.apply_migrations(conn)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 0,
                "Pragma must stay at 0 when migration raises",
            )
        finally:
            conn.close()

    def test_already_up_to_date_is_a_noop(self):
        """When user_version is already at the latest target, no
        migration should run (no exception even though the DB is
        empty)."""
        conn = sqlite3.connect(":memory:")
        try:
            migrations.set_user_version(conn, 1)
            # Should not raise even without a jobs table
            migrations.apply_migrations(conn)
            self.assertEqual(
                conn.execute("PRAGMA user_version").fetchone()[0], 1,
            )
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
