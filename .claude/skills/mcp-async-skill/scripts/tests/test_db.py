# -*- coding: utf-8 -*-
"""Tests for the job queue database layer (db.py).

Uses in-memory SQLite (:memory:) for fast, isolated testing.
"""
import sys
import os
import unittest
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue import db


class TestInitSchema(unittest.TestCase):
    """Schema creation and basic connectivity."""

    def test_create_tables(self):
        store = db.JobStore(":memory:")
        # jobs table must exist
        cur = store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
        )
        self.assertEqual(cur.fetchone()[0], "jobs")

    def test_create_tables_idempotent(self):
        store = db.JobStore(":memory:")
        # calling init again should not raise — both the
        # ``CREATE TABLE IF NOT EXISTS`` and the migration runner are
        # designed to be safe to re-invoke on an already-initialised DB.
        store._init_schema_and_migrations()


class TestInsertJob(unittest.TestCase):
    """Job insertion (POST /api/jobs equivalent)."""

    def setUp(self):
        self.store = db.JobStore(":memory:")

    def test_insert_returns_job_id(self):
        job_id = self.store.insert_job(
            endpoint="http://localhost:8000",
            submit_tool="generate_image",
            args='{"prompt":"cat"}',
        )
        self.assertIsInstance(job_id, str)
        self.assertTrue(len(job_id) > 0)

    def test_insert_stores_correct_fields(self):
        job_id = self.store.insert_job(
            endpoint="http://localhost:8000",
            submit_tool="generate_image",
            args='{"prompt":"cat"}',
            status_tool="check_status",
            result_tool="get_result",
            headers='{"Authorization":"Bearer xxx"}',
        )
        job = self.store.get_job(job_id)
        self.assertEqual(job["endpoint"], "http://localhost:8000")
        self.assertEqual(job["submit_tool"], "generate_image")
        self.assertEqual(job["args"], '{"prompt":"cat"}')
        self.assertEqual(job["status_tool"], "check_status")
        self.assertEqual(job["result_tool"], "get_result")
        self.assertEqual(job["headers"], '{"Authorization":"Bearer xxx"}')
        self.assertEqual(job["status"], "pending")

    def test_insert_default_status_is_pending(self):
        job_id = self.store.insert_job(
            endpoint="http://localhost:8000",
            submit_tool="gen",
            args="{}",
        )
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "pending")

    def test_insert_multiple_jobs(self):
        ids = set()
        for i in range(5):
            ids.add(
                self.store.insert_job(
                    endpoint="http://localhost:8000",
                    submit_tool="gen",
                    args=f'{{"i":{i}}}',
                )
            )
        self.assertEqual(len(ids), 5)


class TestGetJob(unittest.TestCase):
    """Job retrieval (GET /api/jobs/{id} equivalent)."""

    def setUp(self):
        self.store = db.JobStore(":memory:")

    def test_get_nonexistent_returns_none(self):
        self.assertIsNone(self.store.get_job("nonexistent-id"))

    def test_get_returns_dict(self):
        job_id = self.store.insert_job(
            endpoint="http://localhost:8000",
            submit_tool="gen",
            args="{}",
        )
        job = self.store.get_job(job_id)
        self.assertIsInstance(job, dict)
        self.assertIn("id", job)
        self.assertIn("status", job)
        self.assertIn("created_at", job)
        self.assertIn("updated_at", job)


class TestUpdateStatus(unittest.TestCase):
    """Status transitions: pending → running → polling → completed / failed."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.job_id = self.store.insert_job(
            endpoint="http://localhost:8000",
            submit_tool="gen",
            args="{}",
        )

    def test_update_to_running(self):
        self.store.update_status(self.job_id, "running")
        job = self.store.get_job(self.job_id)
        self.assertEqual(job["status"], "running")

    def test_update_to_polling_with_session_and_remote_id(self):
        self.store.update_status(
            self.job_id,
            "polling",
            session_id="sess-123",
            remote_job_id="rj-456",
        )
        job = self.store.get_job(self.job_id)
        self.assertEqual(job["status"], "polling")
        self.assertEqual(job["session_id"], "sess-123")
        self.assertEqual(job["remote_job_id"], "rj-456")

    def test_update_to_completed_with_result(self):
        self.store.update_status(
            self.job_id,
            "completed",
            result='{"urls":["https://example.com/img.png"]}',
        )
        job = self.store.get_job(self.job_id)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(
            job["result"], '{"urls":["https://example.com/img.png"]}'
        )

    def test_update_to_failed_with_error(self):
        self.store.update_status(
            self.job_id,
            "failed",
            error="429 Too Many Requests",
        )
        job = self.store.get_job(self.job_id)
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"], "429 Too Many Requests")

    def test_updated_at_changes(self):
        job_before = self.store.get_job(self.job_id)
        import time

        time.sleep(0.05)
        self.store.update_status(self.job_id, "running")
        job_after = self.store.get_job(self.job_id)
        self.assertNotEqual(job_before["updated_at"], job_after["updated_at"])


class TestQueryPendingJobs(unittest.TestCase):
    """Querying pending jobs for the dispatcher."""

    def setUp(self):
        self.store = db.JobStore(":memory:")

    def test_pending_endpoints_empty(self):
        endpoints = self.store.get_pending_endpoints()
        self.assertEqual(endpoints, [])

    def test_pending_endpoints_returns_unique(self):
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.insert_job(endpoint="http://b:8001", submit_tool="g", args="{}")
        endpoints = self.store.get_pending_endpoints()
        self.assertEqual(sorted(endpoints), ["http://a:8000", "http://b:8001"])

    def test_get_oldest_pending_for_endpoint(self):
        id1 = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args='{"i":1}'
        )
        _id2 = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args='{"i":2}'
        )
        oldest = self.store.get_oldest_pending(endpoint="http://a:8000")
        self.assertIsNotNone(oldest)
        self.assertEqual(oldest["id"], id1)

    def test_get_oldest_pending_skips_running(self):
        id1 = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args='{"i":1}'
        )
        id2 = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args='{"i":2}'
        )
        self.store.update_status(id1, "running")
        oldest = self.store.get_oldest_pending(endpoint="http://a:8000")
        self.assertEqual(oldest["id"], id2)

    def test_count_active_jobs(self):
        id1 = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        id2 = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        id3 = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        self.store.update_status(id1, "running")
        self.store.update_status(id2, "polling")
        # id3 remains pending - should not be counted as active
        count = self.store.count_active_jobs(endpoint="http://a:8000")
        self.assertEqual(count, 2)

    def test_count_active_jobs_different_endpoints(self):
        id1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        id2 = self.store.insert_job(endpoint="http://b:8001", submit_tool="g", args="{}")
        self.store.update_status(id1, "running")
        self.store.update_status(id2, "running")
        self.assertEqual(self.store.count_active_jobs(endpoint="http://a:8000"), 1)
        self.assertEqual(self.store.count_active_jobs(endpoint="http://b:8001"), 1)

    def test_total_active_jobs(self):
        id1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        id2 = self.store.insert_job(endpoint="http://b:8001", submit_tool="g", args="{}")
        self.store.update_status(id1, "running")
        self.store.update_status(id2, "polling")
        self.assertEqual(self.store.count_all_active_jobs(), 2)


class TestGetAllJobs(unittest.TestCase):
    """Job listing (GET /api/jobs equivalent)."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        # Insert jobs with various statuses and endpoints
        self.id1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="gen", args='{"i":1}')
        self.id2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="gen", args='{"i":2}')
        self.id3 = self.store.insert_job(endpoint="http://b:8001", submit_tool="gen", args='{"i":3}')
        self.store.update_status(self.id1, "running")
        self.store.update_status(self.id2, "completed", result='{"url":"https://example.com/img.png"}')

    def test_get_all_jobs_returns_list(self):
        jobs = self.store.get_all_jobs()
        self.assertIsInstance(jobs, list)
        self.assertEqual(len(jobs), 3)

    def test_get_all_jobs_filter_by_status(self):
        jobs = self.store.get_all_jobs(status="pending")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], self.id3)

    def test_get_all_jobs_filter_by_endpoint(self):
        jobs = self.store.get_all_jobs(endpoint="http://a:8000")
        self.assertEqual(len(jobs), 2)

    def test_get_all_jobs_filter_both(self):
        jobs = self.store.get_all_jobs(status="running", endpoint="http://a:8000")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], self.id1)

    def test_get_all_jobs_empty(self):
        store = db.JobStore(":memory:")
        jobs = store.get_all_jobs()
        self.assertEqual(jobs, [])

    def test_get_all_jobs_ordered_by_created_at(self):
        jobs = self.store.get_all_jobs()
        # Newest first (descending)
        self.assertEqual(jobs[0]["id"], self.id3)
        self.assertEqual(jobs[2]["id"], self.id1)

    def test_get_all_jobs_with_limit(self):
        jobs = self.store.get_all_jobs(limit=2)
        self.assertEqual(len(jobs), 2)

    def test_get_all_jobs_returns_dict_rows(self):
        jobs = self.store.get_all_jobs()
        for job in jobs:
            self.assertIsInstance(job, dict)
            self.assertIn("id", job)
            self.assertIn("status", job)
            self.assertIn("endpoint", job)


class TestGetStatsByEndpoint(unittest.TestCase):
    """Per-endpoint statistics (GET /api/stats equivalent)."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        # endpoint A: 1 running, 1 completed, 1 failed
        id1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="gen", args="{}")
        id2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="gen", args="{}")
        id3 = self.store.insert_job(endpoint="http://a:8000", submit_tool="gen", args="{}")
        self.store.update_status(id1, "running")
        self.store.update_status(id2, "completed")
        self.store.update_status(id3, "failed", error="timeout")
        # endpoint B: 2 pending
        self.store.insert_job(endpoint="http://b:8001", submit_tool="gen", args="{}")
        self.store.insert_job(endpoint="http://b:8001", submit_tool="gen", args="{}")

    def test_stats_returns_list(self):
        stats = self.store.get_stats_by_endpoint()
        self.assertIsInstance(stats, list)

    def test_stats_has_both_endpoints(self):
        stats = self.store.get_stats_by_endpoint()
        endpoints = [s["endpoint"] for s in stats]
        self.assertIn("http://a:8000", endpoints)
        self.assertIn("http://b:8001", endpoints)

    def test_stats_counts_for_endpoint_a(self):
        stats = self.store.get_stats_by_endpoint()
        a_stats = next(s for s in stats if s["endpoint"] == "http://a:8000")
        self.assertEqual(a_stats["total"], 3)
        self.assertEqual(a_stats["pending"], 0)
        self.assertEqual(a_stats["running"], 1)
        self.assertEqual(a_stats["completed"], 1)
        self.assertEqual(a_stats["failed"], 1)

    def test_stats_counts_for_endpoint_b(self):
        stats = self.store.get_stats_by_endpoint()
        b_stats = next(s for s in stats if s["endpoint"] == "http://b:8001")
        self.assertEqual(b_stats["total"], 2)
        self.assertEqual(b_stats["pending"], 2)
        self.assertEqual(b_stats["running"], 0)
        self.assertEqual(b_stats["completed"], 0)
        self.assertEqual(b_stats["failed"], 0)

    def test_stats_empty(self):
        store = db.JobStore(":memory:")
        stats = store.get_stats_by_endpoint()
        self.assertEqual(stats, [])


class TestGetStaleJobs(unittest.TestCase):
    """Querying stale (zombie) jobs for recovery on startup."""

    def setUp(self):
        self.store = db.JobStore(":memory:")

    def test_returns_running_jobs(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid, "running")
        stale = self.store.get_stale_jobs(["running", "polling"])
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["id"], jid)

    def test_returns_polling_jobs(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid, "polling", session_id="s1", remote_job_id="r1")
        stale = self.store.get_stale_jobs(["running", "polling"])
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["session_id"], "s1")
        self.assertEqual(stale[0]["remote_job_id"], "r1")

    def test_excludes_pending_and_completed(self):
        jid1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid2, "completed", result="{}")
        stale = self.store.get_stale_jobs(["running", "polling"])
        self.assertEqual(len(stale), 0)

    def test_empty_db(self):
        stale = self.store.get_stale_jobs(["running", "polling"])
        self.assertEqual(stale, [])

    def test_ordered_by_created_at_asc(self):
        jid1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args='{"i":1}')
        jid2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args='{"i":2}')
        self.store.update_status(jid1, "running")
        self.store.update_status(jid2, "running")
        stale = self.store.get_stale_jobs(["running"])
        self.assertEqual(stale[0]["id"], jid1)
        self.assertEqual(stale[1]["id"], jid2)

    def test_single_status_filter(self):
        jid1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid1, "running")
        self.store.update_status(jid2, "polling")
        stale = self.store.get_stale_jobs(["running"])
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["id"], jid1)


class TestPurgeOldJobs(unittest.TestCase):
    """Purging old completed/failed jobs on startup."""

    def setUp(self):
        self.store = db.JobStore(":memory:")

    def test_purge_with_zero_retention_deletes_completed(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid, "completed", result="{}")
        count = self.store.purge_old_jobs(retention_seconds=0)
        self.assertEqual(count, 1)
        self.assertIsNone(self.store.get_job(jid))

    def test_purge_with_zero_retention_deletes_failed(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid, "failed", error="test error")
        count = self.store.purge_old_jobs(retention_seconds=0)
        self.assertEqual(count, 1)
        self.assertIsNone(self.store.get_job(jid))

    def test_purge_keeps_recent_jobs(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid, "completed", result="{}")
        count = self.store.purge_old_jobs(retention_seconds=9999)
        self.assertEqual(count, 0)
        self.assertIsNotNone(self.store.get_job(jid))

    def test_purge_does_not_touch_pending(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        count = self.store.purge_old_jobs(retention_seconds=0)
        self.assertEqual(count, 0)
        self.assertIsNotNone(self.store.get_job(jid))

    def test_purge_does_not_touch_running(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid, "running")
        count = self.store.purge_old_jobs(retention_seconds=0)
        self.assertEqual(count, 0)
        self.assertIsNotNone(self.store.get_job(jid))

    def test_purge_does_not_touch_polling(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid, "polling")
        count = self.store.purge_old_jobs(retention_seconds=0)
        self.assertEqual(count, 0)

    def test_purge_empty_db(self):
        count = self.store.purge_old_jobs(retention_seconds=0)
        self.assertEqual(count, 0)

    def test_purge_mixed_statuses(self):
        jid1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid3 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid4 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid1, "completed", result="{}")
        self.store.update_status(jid2, "failed", error="err")
        self.store.update_status(jid3, "running")
        # jid4 stays pending
        count = self.store.purge_old_jobs(retention_seconds=0)
        self.assertEqual(count, 2)  # only completed and failed
        self.assertIsNone(self.store.get_job(jid1))
        self.assertIsNone(self.store.get_job(jid2))
        self.assertIsNotNone(self.store.get_job(jid3))
        self.assertIsNotNone(self.store.get_job(jid4))


class TestLegacyTimestampCompat(unittest.TestCase):
    """Pre-95ebebb DB rows used SQL-side ``strftime('%Y-%m-%dT%H:%M:%f', 'now')``
    which produces NO trailing ``Z``. Post-95ebebb rows go through Python's
    ``_utc_now_iso()`` and DO carry ``Z``. ``purge_old_jobs`` and
    ``get_stale_polling`` must accept BOTH formats so that a worker that
    upgrades over an existing DB doesn't suddenly start failing to purge
    or detect stale jobs on its old rows.

    Regression context: ``purge_old_jobs`` previously mixed
    ``julianday('now')`` (second precision) with Python's
    microsecond-precision timestamps, and ``get_stale_polling`` used
    ``REPLACE(updated_at, 'Z', '')`` to strip the suffix. PR #59
    rewrote both to pass the Python-side ``_utc_now_iso()`` as a bind
    parameter. SQLite's ``julianday`` itself accepts the ``Z`` form
    natively, so legacy rows without the suffix continue to parse too.
    These tests pin both shapes down explicitly.
    """

    def setUp(self):
        self.store = db.JobStore(":memory:")

    def _insert_with_raw_updated_at(self, jid: str, status: str,
                                    updated_at: str):
        """Force a specific updated_at value into the DB, simulating a
        row written by an older worker version."""
        with self.store._lock:
            self.store.conn.execute(
                "UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, updated_at, jid),
            )
            self.store.conn.commit()

    def test_purge_handles_legacy_no_z_timestamp(self):
        """A legacy row without a Z suffix must still be purged when its
        age exceeds retention_seconds."""
        jid = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}",
        )
        # Pre-95ebebb shape: no Z, written by SQL strftime
        self._insert_with_raw_updated_at(jid, "completed",
                                         "2020-01-01T00:00:00.000")

        count = self.store.purge_old_jobs(retention_seconds=60)
        self.assertEqual(count, 1)
        self.assertIsNone(self.store.get_job(jid))

    def test_purge_handles_post_95ebebb_z_timestamp(self):
        """A modern row WITH a Z suffix must also be purged when old."""
        jid = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}",
        )
        # Post-95ebebb shape: trailing Z
        self._insert_with_raw_updated_at(jid, "completed",
                                         "2020-01-01T00:00:00.000000Z")

        count = self.store.purge_old_jobs(retention_seconds=60)
        self.assertEqual(count, 1)
        self.assertIsNone(self.store.get_job(jid))

    def test_get_stale_polling_handles_legacy_no_z_timestamp(self):
        """A polling row with a legacy (no-Z) updated_at must still be
        flagged stale once its age exceeds the timeout."""
        jid = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}",
        )
        self._insert_with_raw_updated_at(jid, "polling",
                                         "2020-01-01T00:00:00.000")

        stale = self.store.get_stale_polling(timeout_seconds=60)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["id"], jid)

    def test_get_stale_polling_handles_post_95ebebb_z_timestamp(self):
        """A polling row with a Z-suffixed updated_at must also be
        flagged stale once its age exceeds the timeout."""
        jid = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}",
        )
        self._insert_with_raw_updated_at(jid, "polling",
                                         "2020-01-01T00:00:00.000000Z")

        stale = self.store.get_stale_polling(timeout_seconds=60)
        self.assertEqual(len(stale), 1)
        self.assertEqual(stale[0]["id"], jid)


class TestRecoveringEndpoints(unittest.TestCase):
    """Querying recovering jobs for the dispatcher."""

    def setUp(self):
        self.store = db.JobStore(":memory:")

    def test_get_recovering_endpoints_empty(self):
        endpoints = self.store.get_recovering_endpoints()
        self.assertEqual(endpoints, [])

    def test_get_recovering_endpoints_returns_unique(self):
        jid1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid3 = self.store.insert_job(endpoint="http://b:8001", submit_tool="g", args="{}")
        self.store.update_status(jid1, "recovering")
        self.store.update_status(jid2, "recovering")
        self.store.update_status(jid3, "recovering")
        endpoints = self.store.get_recovering_endpoints()
        self.assertEqual(sorted(endpoints), ["http://a:8000", "http://b:8001"])

    def test_get_recovering_endpoints_excludes_other_statuses(self):
        jid1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid2 = self.store.insert_job(endpoint="http://b:8001", submit_tool="g", args="{}")
        self.store.update_status(jid1, "pending")
        self.store.update_status(jid2, "running")
        endpoints = self.store.get_recovering_endpoints()
        self.assertEqual(endpoints, [])

    def test_get_oldest_recovering(self):
        jid1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args='{"i":1}')
        jid2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args='{"i":2}')
        self.store.update_status(jid1, "recovering")
        self.store.update_status(jid2, "recovering")
        oldest = self.store.get_oldest_recovering("http://a:8000")
        self.assertIsNotNone(oldest)
        self.assertEqual(oldest["id"], jid1)

    def test_get_oldest_recovering_none_for_other_endpoint(self):
        jid = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid, "recovering")
        result = self.store.get_oldest_recovering("http://b:8001")
        self.assertIsNone(result)

    def test_get_oldest_recovering_empty(self):
        result = self.store.get_oldest_recovering("http://a:8000")
        self.assertIsNone(result)

    def test_count_active_excludes_recovering(self):
        """recovering should NOT be counted as active (running+polling)."""
        jid1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid2 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        jid3 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(jid1, "running")
        self.store.update_status(jid2, "recovering")
        self.store.update_status(jid3, "polling")
        self.assertEqual(self.store.count_active_jobs("http://a:8000"), 2)  # running + polling only
        self.assertEqual(self.store.count_all_active_jobs(), 2)


class TestClose(unittest.TestCase):
    """Connection lifecycle."""

    def test_close(self):
        store = db.JobStore(":memory:")
        store.close()
        with self.assertRaises(Exception):
            store.get_job("x")


if __name__ == "__main__":
    unittest.main()
