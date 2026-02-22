# -*- coding: utf-8 -*-
"""Tests for execute_job refactoring, worker-side download, and zombie recovery.

Uses mocked MCPAsyncClient to test the full job execution pipeline
without actual network calls.
"""
import sys
import os
import json
import tempfile
import shutil
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue import db
from mcp_worker_daemon import (
    _download_results,
    create_rollback_fn,
)


class TestDownloadResults(unittest.TestCase):
    """Verify _download_results saves files and result.json."""

    def setUp(self):
        self.results_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.results_dir, ignore_errors=True)

    def test_saves_result_json(self):
        """result.json should be saved to {results_dir}/{job_id}/."""
        result_resp = {"status": "ok", "data": "hello"}
        info = _download_results(result_resp, "job-001", self.results_dir)

        result_path = os.path.join(self.results_dir, "job-001", "result.json")
        self.assertTrue(os.path.exists(result_path))
        with open(result_path, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["remote_result"], result_resp)

    @patch("mcp_worker_daemon.download_file")
    def test_downloads_urls(self, mock_dl):
        """URLs in the result should be downloaded."""
        mock_dl.return_value = "/tmp/img.png"
        result_resp = {"url": "https://example.com/img.png"}
        info = _download_results(result_resp, "job-002", self.results_dir)

        mock_dl.assert_called_once()
        self.assertIn("/tmp/img.png", info["local_files"])

    @patch("mcp_worker_daemon.download_file")
    def test_download_failure_non_fatal(self, mock_dl):
        """Download failure should not raise; errors are recorded."""
        mock_dl.side_effect = Exception("network error")
        result_resp = {"url": "https://example.com/img.png"}
        info = _download_results(result_resp, "job-003", self.results_dir)

        self.assertEqual(len(info["local_files"]), 0)
        self.assertEqual(len(info["download_errors"]), 1)
        self.assertIn("network error", info["download_errors"][0])

    def test_no_urls_still_saves_result(self):
        """Even without URLs, result.json should be saved."""
        result_resp = {"text": "no urls here"}
        info = _download_results(result_resp, "job-004", self.results_dir)

        result_path = os.path.join(self.results_dir, "job-004", "result.json")
        self.assertTrue(os.path.exists(result_path))
        self.assertEqual(info["local_files"], [])
        self.assertEqual(info["download_errors"], [])

    def test_results_dir_none_skips_download(self):
        """If results_dir is None, download is skipped entirely."""
        result_resp = {"url": "https://example.com/img.png"}
        info = _download_results(result_resp, "job-005", None)

        self.assertEqual(info["local_files"], [])
        self.assertIsNone(info.get("download_errors"))


class TestCreateRollbackFn(unittest.TestCase):
    """Verify create_rollback_fn correctly transitions zombie jobs."""

    def setUp(self):
        self.store = db.JobStore(":memory:")

    def test_running_without_remote_id_to_failed(self):
        """running job without remote_job_id → failed (submit never completed)."""
        job_id = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        self.store.update_status(job_id, "running")

        rollback = create_rollback_fn()
        rollback(self.store)

        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "failed")
        self.assertIn("Worker restarted", job["error"])

    def test_polling_with_remote_id_to_recovering(self):
        """polling job with remote_job_id → recovering (recovery possible)."""
        job_id = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        self.store.update_status(
            job_id, "polling",
            session_id="sess-1", remote_job_id="remote-1",
        )

        rollback = create_rollback_fn()
        rollback(self.store)

        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "recovering")
        # session_id and remote_job_id should be preserved
        self.assertEqual(job["session_id"], "sess-1")
        self.assertEqual(job["remote_job_id"], "remote-1")

    def test_polling_without_remote_id_to_failed(self):
        """polling job without remote_job_id → failed (no request to recover)."""
        job_id = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        self.store.update_status(job_id, "polling")

        rollback = create_rollback_fn()
        rollback(self.store)

        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "failed")

    def test_completed_jobs_not_affected(self):
        """completed/failed jobs should not be touched."""
        job_id = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        self.store.update_status(job_id, "completed", result='{"ok":true}')

        rollback = create_rollback_fn()
        rollback(self.store)

        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "completed")

    def test_pending_jobs_not_affected(self):
        """pending jobs should not be touched."""
        job_id = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )

        rollback = create_rollback_fn()
        rollback(self.store)

        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "pending")

    def test_multiple_zombie_jobs(self):
        """Multiple zombie jobs of different types should be handled correctly."""
        # running → failed
        id1 = self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.update_status(id1, "running")

        # polling with remote_id → recovering
        id2 = self.store.insert_job(endpoint="http://b:8000", submit_tool="g", args="{}")
        self.store.update_status(id2, "polling", session_id="s2", remote_job_id="r2")

        # polling without remote_id → failed
        id3 = self.store.insert_job(endpoint="http://c:8000", submit_tool="g", args="{}")
        self.store.update_status(id3, "polling")

        rollback = create_rollback_fn()
        rollback(self.store)

        self.assertEqual(self.store.get_job(id1)["status"], "failed")
        self.assertEqual(self.store.get_job(id2)["status"], "recovering")
        self.assertEqual(self.store.get_job(id3)["status"], "failed")


class TestExecuteJobWithRetry(unittest.TestCase):
    """Test that execute_job integrates retry and download functionality."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.results_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.results_dir, ignore_errors=True)

    @patch("mcp_worker_daemon.MCPAsyncClient")
    @patch("mcp_worker_daemon.parse_status_response")
    @patch("mcp_worker_daemon.download_file")
    def test_normal_job_flow(self, mock_dl, mock_parse, MockClient):
        """Normal job: submit → poll → result → download → completed."""
        from mcp_worker_daemon import create_mcp_job_executor

        client_instance = MockClient.return_value
        client_instance.session_id = "sess-test"
        client_instance.submit.return_value = "req-123"
        mock_parse.return_value = ("completed", {})
        client_instance.get_result.return_value = {
            "url": "https://example.com/img.png"
        }
        mock_dl.return_value = os.path.join(self.results_dir, "job-x", "img.png")

        executor = create_mcp_job_executor(results_dir=self.results_dir)
        executor._store = self.store

        job_id = self.store.insert_job(
            endpoint="http://a:8000",
            submit_tool="gen",
            args='{"prompt":"cat"}',
            status_tool="check",
            result_tool="get",
        )
        job = self.store.get_job(job_id)
        executor(job)

        final = self.store.get_job(job_id)
        self.assertEqual(final["status"], "completed")
        self.assertIsNotNone(final["result"])
        result_data = json.loads(final["result"])
        self.assertIn("local_files", result_data)

    @patch("mcp_worker_daemon.MCPAsyncClient")
    @patch("mcp_worker_daemon.parse_status_response")
    def test_recovery_job_skips_submit(self, mock_parse, MockClient):
        """Recovery job (has remote_job_id): should skip submit, start from poll."""
        from mcp_worker_daemon import create_mcp_job_executor

        client_instance = MockClient.return_value
        mock_parse.return_value = ("completed", {})
        client_instance.get_result.return_value = {"text": "result data"}

        executor = create_mcp_job_executor(results_dir=self.results_dir)
        executor._store = self.store

        job_id = self.store.insert_job(
            endpoint="http://a:8000",
            submit_tool="gen",
            args="{}",
            status_tool="check",
            result_tool="get",
        )
        self.store.update_status(
            job_id, "polling",
            session_id="old-sess", remote_job_id="old-req",
        )
        self.store.update_status(job_id, "recovering")

        job = self.store.get_job(job_id)
        executor(job)

        # submit should NOT have been called
        client_instance.submit.assert_not_called()
        # session_id should be set from the job
        self.assertEqual(client_instance.session_id, "old-sess")
        # get_result should have been called
        client_instance.get_result.assert_called_once()

        final = self.store.get_job(job_id)
        self.assertEqual(final["status"], "completed")


if __name__ == "__main__":
    unittest.main()
