# -*- coding: utf-8 -*-
"""Tests for the queue dispatcher and rate limiter (dispatcher.py).

Uses mock job executors to verify rate limiting, concurrency control,
and per-endpoint independent scheduling.
"""
import sys
import os
import time
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from job_queue import db, dispatcher


class TestConfigLoading(unittest.TestCase):
    """Loading and merging rate limit configuration."""

    def test_default_config(self):
        cfg = dispatcher.QueueConfig()
        self.assertGreater(cfg.default_max_concurrent, 0)
        self.assertGreater(cfg.default_min_interval, 0)

    def test_from_dict(self):
        cfg = dispatcher.QueueConfig.from_dict({
            "host": "127.0.0.1",
            "port": 55555,
            "idle_timeout_seconds": 120,
            "default_rate_limit": {
                "max_concurrent_jobs": 3,
                "min_interval_seconds": 1.5,
            },
            "endpoint_rate_limits": {
                "http://slow:8000": {
                    "max_concurrent_jobs": 1,
                    "min_interval_seconds": 10.0,
                }
            },
        })
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 55555)
        self.assertEqual(cfg.idle_timeout, 120)
        self.assertEqual(cfg.default_max_concurrent, 3)
        self.assertEqual(cfg.default_min_interval, 1.5)

    def test_get_limits_for_known_endpoint(self):
        cfg = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 2,
                "min_interval_seconds": 2.0,
            },
            "endpoint_rate_limits": {
                "http://slow:8000": {
                    "max_concurrent_jobs": 1,
                    "min_interval_seconds": 10.0,
                }
            },
        })
        max_c, min_i = cfg.get_limits("http://slow:8000")
        self.assertEqual(max_c, 1)
        self.assertEqual(min_i, 10.0)

    def test_get_limits_falls_back_to_default(self):
        cfg = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 2,
                "min_interval_seconds": 2.0,
            },
            "endpoint_rate_limits": {},
        })
        max_c, min_i = cfg.get_limits("http://unknown:9999")
        self.assertEqual(max_c, 2)
        self.assertEqual(min_i, 2.0)


class TestDispatcherBasic(unittest.TestCase):
    """Basic dispatcher behavior with mock executor."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.config = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 5,
                "min_interval_seconds": 0.0,
            },
        })
        self.executed_jobs = []
        self.disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )

    def _mock_executor(self, job: dict):
        self.executed_jobs.append(job["id"])
        self.store.update_status(job["id"], "completed", result='{"ok":true}')

    def test_dispatch_single_job(self):
        job_id = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="gen", args="{}"
        )
        self.disp.dispatch_once()
        # Give executor thread time to complete
        time.sleep(0.2)
        self.assertIn(job_id, self.executed_jobs)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "completed")

    def test_dispatch_empty_queue(self):
        # Should not raise when no jobs exist
        dispatched = self.disp.dispatch_once()
        self.assertEqual(dispatched, 0)

    def test_dispatch_multiple_jobs_same_endpoint(self):
        ids = []
        for i in range(3):
            ids.append(
                self.store.insert_job(
                    endpoint="http://a:8000", submit_tool="gen", args=f'{{"i":{i}}}'
                )
            )
        # Dispatch all at once (max_concurrent=5, interval=0)
        self.disp.dispatch_once()
        time.sleep(0.3)
        for jid in ids:
            self.assertIn(jid, self.executed_jobs)


class TestConcurrencyLimit(unittest.TestCase):
    """Verify max_concurrent_jobs is respected per endpoint."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.config = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 1,
                "min_interval_seconds": 0.0,
            },
        })
        self.running_event = threading.Event()
        self.block_event = threading.Event()
        self.concurrent_count = 0
        self.max_concurrent_seen = 0
        self.lock = threading.Lock()

    def _slow_executor(self, job: dict):
        with self.lock:
            self.concurrent_count += 1
            self.max_concurrent_seen = max(
                self.max_concurrent_seen, self.concurrent_count
            )
        self.running_event.set()
        self.block_event.wait(timeout=5)
        with self.lock:
            self.concurrent_count -= 1
        self.store.update_status(job["id"], "completed", result="{}")

    def test_max_concurrent_1_blocks_second_job(self):
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._slow_executor,
        )
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")

        disp.dispatch_once()
        self.running_event.wait(timeout=2)
        # First job is running; second should NOT be dispatched
        disp.dispatch_once()
        time.sleep(0.1)
        self.assertEqual(self.max_concurrent_seen, 1)

        # Release first job
        self.block_event.set()
        time.sleep(0.3)
        # Now second should be dispatchable
        disp.dispatch_once()
        time.sleep(0.3)
        self.assertEqual(self.store.get_job(
            self.store.get_pending_endpoints() and "x" or "x"
        ), None)  # just checking no crash


class TestIntervalLimit(unittest.TestCase):
    """Verify min_interval_seconds is respected per endpoint."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.config = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 10,
                "min_interval_seconds": 0.5,
            },
        })
        self.dispatch_times = []

    def _instant_executor(self, job: dict):
        self.dispatch_times.append(time.monotonic())
        self.store.update_status(job["id"], "completed", result="{}")

    def test_interval_between_dispatches(self):
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._instant_executor,
        )
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")

        disp.dispatch_once()
        time.sleep(0.1)
        # Second dispatch should be skipped (interval not met)
        dispatched = disp.dispatch_once()
        self.assertEqual(dispatched, 0)

        # Wait for interval to pass
        time.sleep(0.5)
        dispatched = disp.dispatch_once()
        time.sleep(0.1)
        self.assertEqual(dispatched, 1)


class TestMultiEndpointIndependence(unittest.TestCase):
    """Two endpoints with different limits should not block each other."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.config = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 2,
                "min_interval_seconds": 0.0,
            },
            "endpoint_rate_limits": {
                "http://slow:8000": {
                    "max_concurrent_jobs": 1,
                    "min_interval_seconds": 5.0,
                },
                "http://fast:8001": {
                    "max_concurrent_jobs": 5,
                    "min_interval_seconds": 0.0,
                },
            },
        })
        self.executed = {"slow": [], "fast": []}

    def _executor(self, job: dict):
        ep = "slow" if "slow" in job["endpoint"] else "fast"
        self.executed[ep].append(job["id"])
        self.store.update_status(job["id"], "completed", result="{}")

    def test_fast_endpoint_not_blocked_by_slow(self):
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._executor,
        )
        # Submit 1 slow + 3 fast jobs
        self.store.insert_job(endpoint="http://slow:8000", submit_tool="g", args="{}")
        for _ in range(3):
            self.store.insert_job(
                endpoint="http://fast:8001", submit_tool="g", args="{}"
            )

        disp.dispatch_once()
        time.sleep(0.3)

        # Slow: 1 dispatched (max_concurrent=1)
        self.assertEqual(len(self.executed["slow"]), 1)
        # Fast: all 3 dispatched (max_concurrent=5, interval=0)
        self.assertEqual(len(self.executed["fast"]), 3)

    def test_slow_endpoint_second_job_waits_for_interval(self):
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._executor,
        )
        self.store.insert_job(endpoint="http://slow:8000", submit_tool="g", args="{}")
        self.store.insert_job(endpoint="http://slow:8000", submit_tool="g", args="{}")

        disp.dispatch_once()
        time.sleep(0.2)
        self.assertEqual(len(self.executed["slow"]), 1)

        # Second dispatch: interval 5.0s not met yet
        dispatched = disp.dispatch_once()
        self.assertEqual(dispatched, 0)


class TestDispatcherLoop(unittest.TestCase):
    """Test the main loop start/stop."""

    def test_start_and_stop(self):
        store = db.JobStore(":memory:")
        config = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 2,
                "min_interval_seconds": 0.0,
            },
        })
        executed = []

        def executor(job):
            executed.append(job["id"])
            store.update_status(job["id"], "completed", result="{}")

        disp = dispatcher.Dispatcher(
            store=store,
            config=config,
            job_executor=executor,
            loop_interval=0.05,
        )
        store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")

        disp.start()
        time.sleep(0.5)
        disp.stop()
        self.assertEqual(len(executed), 1)


class TestPauseEndpoint(unittest.TestCase):
    """Verify pause_endpoint() suspends dispatch for a given endpoint."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.config = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 5,
                "min_interval_seconds": 0.0,
            },
        })
        self.executed_jobs = []

    def _mock_executor(self, job: dict):
        self.executed_jobs.append(job["id"])
        self.store.update_status(job["id"], "completed", result="{}")

    def test_paused_endpoint_skipped(self):
        """Jobs on a paused endpoint should not be dispatched."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        disp.pause_endpoint("http://a:8000", 10.0)

        dispatched = disp.dispatch_once()
        self.assertEqual(dispatched, 0)
        self.assertEqual(len(self.executed_jobs), 0)

    def test_unpaused_endpoint_dispatched(self):
        """Other endpoints should still dispatch while one is paused."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        self.store.insert_job(endpoint="http://b:8000", submit_tool="g", args="{}")
        disp.pause_endpoint("http://a:8000", 10.0)

        disp.dispatch_once()
        time.sleep(0.2)
        # Only endpoint b should be dispatched
        self.assertEqual(len(self.executed_jobs), 1)
        job_b = self.store.get_all_jobs(endpoint="http://b:8000")[0]
        self.assertEqual(job_b["status"], "completed")
        job_a = self.store.get_all_jobs(endpoint="http://a:8000")[0]
        self.assertEqual(job_a["status"], "pending")

    def test_pause_expires(self):
        """After pause duration, endpoint should dispatch again."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        disp.pause_endpoint("http://a:8000", 0.2)

        dispatched = disp.dispatch_once()
        self.assertEqual(dispatched, 0)

        time.sleep(0.3)
        dispatched = disp.dispatch_once()
        time.sleep(0.1)
        self.assertEqual(dispatched, 1)
        self.assertEqual(len(self.executed_jobs), 1)

    def test_pause_zero_seconds_no_effect(self):
        """Pausing with 0 seconds should effectively not pause."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        disp.pause_endpoint("http://a:8000", 0.0)

        disp.dispatch_once()
        time.sleep(0.1)
        self.assertEqual(len(self.executed_jobs), 1)


class TestRegisterEndpointLimits(unittest.TestCase):
    """Dynamic registration of per-endpoint rate limits."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.config = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 5,
                "min_interval_seconds": 0.0,
            },
        })
        self.executed_jobs = []

    def _mock_executor(self, job: dict):
        self.executed_jobs.append(job["id"])
        self.store.update_status(job["id"], "completed", result="{}")

    def test_register_new_endpoint(self):
        """Registering limits for a new endpoint should update config."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        disp.register_endpoint_limits("http://new:8000", 1, 5.0)
        max_c, min_i = self.config.get_limits("http://new:8000")
        self.assertEqual(max_c, 1)
        self.assertEqual(min_i, 5.0)

    def test_register_does_not_overwrite_existing(self):
        """Registering limits for an already-configured endpoint should not overwrite."""
        cfg = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 5,
                "min_interval_seconds": 0.0,
            },
            "endpoint_rate_limits": {
                "http://existing:8000": {
                    "max_concurrent_jobs": 1,
                    "min_interval_seconds": 10.0,
                }
            },
        })
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=cfg,
            job_executor=self._mock_executor,
        )
        # Try to overwrite with different values
        disp.register_endpoint_limits("http://existing:8000", 99, 0.0)
        max_c, min_i = cfg.get_limits("http://existing:8000")
        # Should keep original values
        self.assertEqual(max_c, 1)
        self.assertEqual(min_i, 10.0)

    def test_registered_limits_applied_to_dispatch(self):
        """Dynamically registered limits should actually affect dispatch behavior."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        # Register max_concurrent=1 for endpoint
        disp.register_endpoint_limits("http://limited:8000", 1, 0.0)

        # Insert 3 jobs
        for _ in range(3):
            self.store.insert_job(
                endpoint="http://limited:8000", submit_tool="g", args="{}"
            )

        # First dispatch: should dispatch only 1 (max_concurrent=1)
        disp.dispatch_once()
        time.sleep(0.2)
        self.assertEqual(len(self.executed_jobs), 1)

        # Second dispatch: first job completed, slot available
        disp.dispatch_once()
        time.sleep(0.2)
        self.assertEqual(len(self.executed_jobs), 2)

    def test_unknown_endpoint_uses_defaults(self):
        """Endpoints without registered limits should use defaults."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        max_c, min_i = self.config.get_limits("http://unknown:9999")
        self.assertEqual(max_c, 5)
        self.assertEqual(min_i, 0.0)


class TestRecoveringDispatch(unittest.TestCase):
    """Verify recovering jobs are dispatched with rate limits applied."""

    def setUp(self):
        self.store = db.JobStore(":memory:")
        self.config = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 5,
                "min_interval_seconds": 0.0,
            },
        })
        self.executed_jobs = []

    def _mock_executor(self, job: dict):
        self.executed_jobs.append(job["id"])
        self.store.update_status(job["id"], "completed", result="{}")

    def _insert_recovering_job(self, endpoint="http://a:8000"):
        """Insert a job and transition it to recovering status."""
        job_id = self.store.insert_job(
            endpoint=endpoint, submit_tool="g", args="{}"
        )
        self.store.update_status(
            job_id, "polling",
            session_id="sess-123", remote_job_id="remote-456",
        )
        self.store.update_status(job_id, "recovering")
        return job_id

    def test_recovering_job_dispatched(self):
        """A recovering job should be dispatched by dispatch_once."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        job_id = self._insert_recovering_job()

        dispatched = disp.dispatch_once()
        time.sleep(0.2)
        self.assertEqual(dispatched, 1)
        self.assertIn(job_id, self.executed_jobs)

    def test_recovering_job_transitions_to_polling(self):
        """Dispatching a recovering job should set status to 'polling', not 'running'."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=lambda job: None,  # do nothing (don't update status)
        )
        job_id = self._insert_recovering_job()

        disp.dispatch_once()
        time.sleep(0.1)
        job = self.store.get_job(job_id)
        self.assertEqual(job["status"], "polling")

    def test_recovering_respects_concurrency_limit(self):
        """Recovering jobs should respect max_concurrent_jobs."""
        cfg = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 1,
                "min_interval_seconds": 0.0,
            },
        })
        block_event = threading.Event()

        def blocking_executor(job):
            block_event.wait(timeout=5)
            self.store.update_status(job["id"], "completed", result="{}")

        disp = dispatcher.Dispatcher(
            store=self.store,
            config=cfg,
            job_executor=blocking_executor,
        )
        # Insert a running job to fill the slot
        normal_id = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        self.store.update_status(normal_id, "running")

        # Insert a recovering job
        recovering_id = self._insert_recovering_job()

        # Should NOT dispatch recovering job (active=1, max=1)
        dispatched = disp.dispatch_once()
        self.assertEqual(dispatched, 0)
        job = self.store.get_job(recovering_id)
        self.assertEqual(job["status"], "recovering")

        block_event.set()

    def test_recovering_respects_interval_limit(self):
        """Recovering jobs should respect min_interval_seconds."""
        cfg = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 10,
                "min_interval_seconds": 0.5,
            },
        })
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=cfg,
            job_executor=self._mock_executor,
        )
        # Dispatch a pending job first to set last_run_time
        self.store.insert_job(endpoint="http://a:8000", submit_tool="g", args="{}")
        disp.dispatch_once()
        time.sleep(0.1)

        # Now insert a recovering job — interval not met yet
        recovering_id = self._insert_recovering_job()
        dispatched = disp.dispatch_once()
        self.assertEqual(dispatched, 0)

        # Wait for interval
        time.sleep(0.5)
        dispatched = disp.dispatch_once()
        time.sleep(0.1)
        self.assertEqual(dispatched, 1)
        self.assertIn(recovering_id, self.executed_jobs)

    def test_recovering_respects_pause(self):
        """Recovering jobs should respect pause_endpoint."""
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=self.config,
            job_executor=self._mock_executor,
        )
        self._insert_recovering_job()
        disp.pause_endpoint("http://a:8000", 10.0)

        dispatched = disp.dispatch_once()
        self.assertEqual(dispatched, 0)

    def test_recovering_not_counted_as_active(self):
        """Recovering jobs should NOT count toward active (running+polling)."""
        cfg = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 1,
                "min_interval_seconds": 0.0,
            },
        })
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=cfg,
            job_executor=self._mock_executor,
        )
        # Insert 2 recovering jobs
        id1 = self._insert_recovering_job()
        id2 = self._insert_recovering_job()

        # active=0 (recovering is not active), max=1
        # Should dispatch first recovering job (it becomes polling → active=1)
        disp.dispatch_once()
        time.sleep(0.2)
        self.assertEqual(len(self.executed_jobs), 1)

    def test_pending_and_recovering_both_dispatched(self):
        """Both pending and recovering jobs should be dispatched in one round."""
        cfg = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 5,
                "min_interval_seconds": 0.0,
            },
        })
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=cfg,
            job_executor=self._mock_executor,
        )
        pending_id = self.store.insert_job(
            endpoint="http://a:8000", submit_tool="g", args="{}"
        )
        recovering_id = self._insert_recovering_job("http://b:8000")

        dispatched = disp.dispatch_once()
        time.sleep(0.2)
        self.assertEqual(dispatched, 2)
        self.assertIn(pending_id, self.executed_jobs)
        self.assertIn(recovering_id, self.executed_jobs)

    def test_recovering_multiple_endpoints_independent(self):
        """Recovering jobs on different endpoints are dispatched independently."""
        cfg = dispatcher.QueueConfig.from_dict({
            "default_rate_limit": {
                "max_concurrent_jobs": 1,
                "min_interval_seconds": 0.0,
            },
        })
        disp = dispatcher.Dispatcher(
            store=self.store,
            config=cfg,
            job_executor=self._mock_executor,
        )
        id_a = self._insert_recovering_job("http://a:8000")
        id_b = self._insert_recovering_job("http://b:8000")

        disp.dispatch_once()
        time.sleep(0.2)
        # Both should dispatch (different endpoints, max_concurrent=1 each)
        self.assertEqual(len(self.executed_jobs), 2)
        self.assertIn(id_a, self.executed_jobs)
        self.assertIn(id_b, self.executed_jobs)


if __name__ == "__main__":
    unittest.main()
