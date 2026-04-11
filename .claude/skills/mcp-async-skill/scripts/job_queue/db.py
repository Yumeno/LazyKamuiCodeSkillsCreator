# -*- coding: utf-8 -*-
"""Job queue database layer using SQLite.

Provides CRUD operations for the job queue. All methods are synchronous
and designed for use from the worker daemon's HTTP handler and dispatcher.
"""
import sqlite3
import threading
import uuid
from datetime import datetime, timezone


def _utc_now_iso() -> str:
    """Return current UTC time as ISO 8601 with Z suffix.

    Example: ``2026-04-11T01:23:45.678901Z``
    The Z suffix explicitly marks the timestamp as UTC so that LLMs and
    other consumers can correctly convert to local time.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class JobStore:
    """SQLite-backed job store for the queue system."""

    def __init__(self, db_path: str = "jobs.db"):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.Lock()
        self._init_schema()

    def _init_schema(self):
        with self._lock:
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
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
                    created_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now')),
                    updated_at TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%f', 'now'))
                )
            """)
            self.conn.commit()

    def insert_job(
        self,
        endpoint: str,
        submit_tool: str,
        args: str,
        status_tool: str | None = None,
        result_tool: str | None = None,
        headers: str | None = None,
    ) -> str:
        """Insert a new job and return its ID."""
        with self._lock:
            job_id = str(uuid.uuid4())
            now = _utc_now_iso()
            self.conn.execute(
                """
                INSERT INTO jobs (id, endpoint, submit_tool, args, status_tool, result_tool, headers, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, endpoint, submit_tool, args, status_tool, result_tool, headers, now, now),
            )
            self.conn.commit()
            return job_id

    def get_job(self, job_id: str) -> dict | None:
        """Get a job by ID. Returns dict or None."""
        with self._lock:
            cur = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
            row = cur.fetchone()
            if row is None:
                return None
            return dict(row)

    def update_status(
        self,
        job_id: str,
        status: str,
        session_id: str | None = None,
        remote_job_id: str | None = None,
        result: str | None = None,
        error: str | None = None,
    ):
        """Update job status and optional fields."""
        with self._lock:
            fields = ["status = ?", "updated_at = ?"]
            params: list = [status, _utc_now_iso()]

            if session_id is not None:
                fields.append("session_id = ?")
                params.append(session_id)
            if remote_job_id is not None:
                fields.append("remote_job_id = ?")
                params.append(remote_job_id)
            if result is not None:
                fields.append("result = ?")
                params.append(result)
            if error is not None:
                fields.append("error = ?")
                params.append(error)

            params.append(job_id)
            self.conn.execute(
                f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            self.conn.commit()

    def get_pending_endpoints(self) -> list[str]:
        """Return unique endpoints that have pending jobs."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT DISTINCT endpoint FROM jobs WHERE status = 'pending'"
            )
            return [row[0] for row in cur.fetchall()]

    def get_oldest_pending(self, endpoint: str) -> dict | None:
        """Get the oldest pending job for a given endpoint."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM jobs WHERE endpoint = ? AND status = 'pending' ORDER BY created_at ASC LIMIT 1",
                (endpoint,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            return dict(row)

    def count_active_jobs(self, endpoint: str) -> int:
        """Count running or polling jobs for a given endpoint."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE endpoint = ? AND status IN ('running', 'polling')",
                (endpoint,),
            )
            return cur.fetchone()[0]

    def count_all_active_jobs(self) -> int:
        """Count all running or polling jobs across all endpoints."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN ('running', 'polling')"
            )
            return cur.fetchone()[0]

    def count_all_pending_jobs(self) -> int:
        """Count all pending jobs across all endpoints."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE status = 'pending'"
            )
            return cur.fetchone()[0]

    def get_all_jobs(
        self,
        status: str | None = None,
        endpoint: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """Get all jobs, optionally filtered by status and/or endpoint.

        Returns list of dicts ordered by created_at descending (newest first).
        """
        with self._lock:
            clauses = []
            params: list = []
            if status:
                clauses.append("status = ?")
                params.append(status)
            if endpoint:
                clauses.append("endpoint = ?")
                params.append(endpoint)

            where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
            query = f"SELECT * FROM jobs{where} ORDER BY created_at DESC, rowid DESC"
            if limit:
                query += " LIMIT ?"
                params.append(limit)

            cur = self.conn.execute(query, params)
            return [dict(row) for row in cur.fetchall()]

    def get_stats_by_endpoint(self) -> list[dict]:
        """Get per-endpoint job statistics.

        Returns list of dicts with endpoint, total, and per-status counts.
        """
        with self._lock:
            cur = self.conn.execute("""
                SELECT
                    endpoint,
                    COUNT(*) AS total,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending,
                    SUM(CASE WHEN status IN ('running', 'polling') THEN 1 ELSE 0 END) AS running,
                    SUM(CASE WHEN status IN ('completed', 'done', 'success') THEN 1 ELSE 0 END) AS completed,
                    SUM(CASE WHEN status IN ('failed', 'error') THEN 1 ELSE 0 END) AS failed
                FROM jobs
                GROUP BY endpoint
                ORDER BY endpoint
            """)
            return [
                {
                    "endpoint": row[0],
                    "total": row[1],
                    "pending": row[2],
                    "running": row[3],
                    "completed": row[4],
                    "failed": row[5],
                }
                for row in cur.fetchall()
            ]

    def get_stale_jobs(self, statuses: list[str]) -> list[dict]:
        """Return all jobs with the given statuses (for zombie recovery on startup).

        Args:
            statuses: List of status values to match (e.g. ["running", "polling"]).

        Returns:
            List of job dicts ordered by created_at ascending (oldest first).
        """
        with self._lock:
            placeholders = ",".join("?" * len(statuses))
            cur = self.conn.execute(
                f"SELECT * FROM jobs WHERE status IN ({placeholders}) ORDER BY created_at ASC",
                statuses,
            )
            return [dict(row) for row in cur.fetchall()]

    def purge_old_jobs(self, retention_seconds: float = 86400.0) -> int:
        """Delete completed/failed jobs older than retention_seconds.

        Args:
            retention_seconds: How long to keep terminal jobs (default 24h).

        Returns:
            Number of deleted jobs.
        """
        with self._lock:
            cur = self.conn.execute(
                """
                DELETE FROM jobs
                WHERE status IN ('completed', 'failed')
                  AND (julianday('now') - julianday(updated_at)) * 86400.0 >= ?
                """,
                (retention_seconds,),
            )
            self.conn.commit()
            return cur.rowcount

    def get_recovering_endpoints(self) -> list[str]:
        """Return unique endpoints that have recovering jobs."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT DISTINCT endpoint FROM jobs WHERE status = 'recovering'"
            )
            return [row[0] for row in cur.fetchall()]

    def get_oldest_recovering(self, endpoint: str) -> dict | None:
        """Get the oldest recovering job for a given endpoint."""
        with self._lock:
            cur = self.conn.execute(
                "SELECT * FROM jobs WHERE endpoint = ? AND status = 'recovering' "
                "ORDER BY created_at ASC LIMIT 1",
                (endpoint,),
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def close(self):
        """Close the database connection."""
        self.conn.close()
