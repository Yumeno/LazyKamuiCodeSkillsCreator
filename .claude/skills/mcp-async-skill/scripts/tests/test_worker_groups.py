# -*- coding: utf-8 -*-
"""Tests for the worker HTTP API surface introduced for custom groups
(PR4 / #60).

Covers:

* ``GET /api/groups`` returns runtime status of every configured
  custom group.
* ``GET /api/config`` includes a ``custom_groups`` block with the
  per-group config.
* ``GET /api/stats`` includes a ``custom_groups`` section alongside
  ``category_limits``.
* ``POST /api/groups/{name}/{pause|resume}`` toggles the group's
  paused state, returns the new status, and rejects unknown group
  names with HTTP 404.
* ``PATCH /api/config`` accepts ``{"groups": {"name": {"max_inflight":
  N}}}`` for per-group runtime tuning, validates types, and rejects
  unknown groups.
* When a custom group is paused, ``POST /api/jobs`` returns a warning
  pointing at the group (not the category).
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

from job_queue import worker


def get_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _GroupsWorkerBase(unittest.TestCase):
    """Boots a worker with a known custom_groups + categories config."""

    def setUp(self):
        self.port = get_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

        self.worker_app = worker.WorkerApp(
            host="127.0.0.1",
            port=self.port,
            db_path=":memory:",
            config_dict={
                "default_rate_limit": {
                    "max_concurrent_jobs": 5,
                    "min_interval_seconds": 0.0,
                },
                "category_rate_limits": {
                    "categories": ["t2i", "i2i", "t2v", "i2v"],
                    "limits": {
                        "t2v": {"max_inflight": 1, "min_interval": 0.0,
                                 "exhaust_cooldown": 60},
                    },
                },
                "custom_groups": {
                    "premium-video": {
                        "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
                        "max_inflight": 1,
                        "min_interval": 30,
                        "exhaust_cooldown": 7200,
                    },
                    "expensive-edit": {
                        "endpoints": ["https://kamui-code.ai/i2i/fal/gpt-image*"],
                        "max_inflight": 2,
                        "min_interval": 5,
                        "exhaust_cooldown": 1800,
                    },
                },
            },
            job_executor=lambda job: None,
            idle_timeout=0,
        )
        self.worker_app.start()

        for _ in range(50):
            try:
                requests.get(f"{self.base_url}/api/health", timeout=0.5)
                break
            except requests.ConnectionError:
                time.sleep(0.05)

    def tearDown(self):
        self.worker_app.stop()


# ---------------------------------------------------------------------
# GET endpoints
# ---------------------------------------------------------------------


class TestGetGroupsEndpoint(_GroupsWorkerBase):
    def test_returns_all_configured_groups(self):
        resp = requests.get(f"{self.base_url}/api/groups")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("groups", body)
        self.assertEqual(set(body["groups"].keys()),
                         {"premium-video", "expensive-edit"})

    def test_each_group_has_status_fields(self):
        body = requests.get(f"{self.base_url}/api/groups").json()
        g = body["groups"]["premium-video"]
        self.assertEqual(g["max_inflight"], 1)
        self.assertEqual(g["exhaust_cooldown"], 7200)
        self.assertEqual(g["paused"], False)
        self.assertEqual(g["inflight"], 0)
        self.assertIn("endpoints", g)
        self.assertEqual(g["endpoints"],
                         ["https://kamui-code.ai/t2v/fal/veo3*"])

    def test_includes_server_time(self):
        body = requests.get(f"{self.base_url}/api/groups").json()
        self.assertIn("server_time_utc", body)
        self.assertTrue(body["server_time_utc"].endswith("Z"))


class TestGetConfigIncludesCustomGroups(_GroupsWorkerBase):
    def test_config_has_custom_groups_block(self):
        body = requests.get(f"{self.base_url}/api/config").json()
        self.assertIn("custom_groups", body)
        self.assertIn("premium-video", body["custom_groups"])
        cfg = body["custom_groups"]["premium-video"]
        self.assertEqual(cfg["max_inflight"], 1)
        self.assertEqual(cfg["min_interval"], 30)
        self.assertEqual(cfg["exhaust_cooldown"], 7200)

    def test_config_still_has_category_block(self):
        """custom_groups addition must not break the existing
        category section consumed by the dashboard."""
        body = requests.get(f"{self.base_url}/api/config").json()
        self.assertIn("category", body)
        self.assertIn("limits", body["category"])


class TestGetStatsIncludesCustomGroups(_GroupsWorkerBase):
    def test_stats_has_custom_groups_section(self):
        body = requests.get(f"{self.base_url}/api/stats").json()
        self.assertIn("custom_groups", body)
        self.assertIn("premium-video", body["custom_groups"])

    def test_stats_still_has_category_limits(self):
        body = requests.get(f"{self.base_url}/api/stats").json()
        self.assertIn("category_limits", body)
        self.assertIn("t2v", body["category_limits"])


# ---------------------------------------------------------------------
# POST /api/groups/{name}/{action}
# ---------------------------------------------------------------------


class TestGroupPauseResume(_GroupsWorkerBase):
    def test_pause_marks_group_paused(self):
        resp = requests.post(
            f"{self.base_url}/api/groups/premium-video/pause",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["group"], "premium-video")
        self.assertTrue(body["paused"])
        self.assertEqual(body["status"]["paused"], True)

        # Confirm via GET
        groups = requests.get(f"{self.base_url}/api/groups").json()["groups"]
        self.assertTrue(groups["premium-video"]["paused"])

    def test_resume_clears_paused(self):
        requests.post(f"{self.base_url}/api/groups/premium-video/pause")
        resp = requests.post(
            f"{self.base_url}/api/groups/premium-video/resume",
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertFalse(body["paused"])

        groups = requests.get(f"{self.base_url}/api/groups").json()["groups"]
        self.assertFalse(groups["premium-video"]["paused"])

    def test_unknown_group_returns_404(self):
        resp = requests.post(
            f"{self.base_url}/api/groups/nonexistent/pause",
        )
        self.assertEqual(resp.status_code, 404)
        body = resp.json()
        self.assertIn("error", body)
        self.assertIn("Unknown group", body["error"])
        # Lists available groups so the user can fix their request
        self.assertIn("available_groups", body)
        self.assertIn("premium-video", body["available_groups"])

    def test_invalid_action_returns_400(self):
        resp = requests.post(
            f"{self.base_url}/api/groups/premium-video/invalid-action",
        )
        self.assertEqual(resp.status_code, 400)


# ---------------------------------------------------------------------
# PATCH /api/config — groups block
# ---------------------------------------------------------------------


class TestPatchGroupsConfig(_GroupsWorkerBase):
    def test_patch_single_group_max_inflight(self):
        body = {"groups": {"premium-video": {"max_inflight": 3}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("groups.premium-video.max_inflight", data["applied"])
        self.assertEqual(data["applied"]["groups.premium-video.max_inflight"], 3)
        self.assertEqual(data["rejected"], {})

        # Confirm via GET
        groups = requests.get(f"{self.base_url}/api/groups").json()["groups"]
        self.assertEqual(groups["premium-video"]["max_inflight"], 3)
        # Other group untouched
        self.assertEqual(groups["expensive-edit"]["max_inflight"], 2)

    def test_patch_multiple_groups(self):
        body = {"groups": {
            "premium-video": {"max_inflight": 5},
            "expensive-edit": {"exhaust_cooldown": 9000},
        }}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertEqual(resp.status_code, 200)
        groups = requests.get(f"{self.base_url}/api/groups").json()["groups"]
        self.assertEqual(groups["premium-video"]["max_inflight"], 5)
        self.assertEqual(groups["expensive-edit"]["exhaust_cooldown"], 9000)

    def test_unknown_group_rejected(self):
        body = {"groups": {"nope": {"max_inflight": 1}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        rejected = resp.json()["rejected"]
        self.assertIn("groups.nope", rejected)
        self.assertIn("unknown group", rejected["groups.nope"])

    def test_groups_must_be_dict(self):
        body = {"groups": [1, 2, 3]}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        rejected = resp.json()["rejected"]
        self.assertIn("groups", rejected)
        self.assertIn("must be object", rejected["groups"])

    def test_per_group_value_must_be_dict(self):
        body = {"groups": {"premium-video": "not-a-dict"}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        rejected = resp.json()["rejected"]
        self.assertIn("groups.premium-video", rejected)

    def test_max_inflight_must_be_int_ge_1(self):
        body = {"groups": {"premium-video": {"max_inflight": 0}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertIn("groups.premium-video.max_inflight",
                      resp.json()["rejected"])

    def test_partial_failure_applies_valid_only(self):
        body = {"groups": {"premium-video": {
            "max_inflight": 4,        # valid
            "exhaust_cooldown": -1,   # invalid
        }}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        data = resp.json()
        self.assertEqual(data["applied"]["groups.premium-video.max_inflight"], 4)
        self.assertIn("groups.premium-video.exhaust_cooldown", data["rejected"])

        groups = requests.get(f"{self.base_url}/api/groups").json()["groups"]
        self.assertEqual(groups["premium-video"]["max_inflight"], 4)
        # The invalid one didn't change anything
        self.assertEqual(groups["premium-video"]["exhaust_cooldown"], 7200)


# ---------------------------------------------------------------------
# POST /api/jobs — group-aware pause warning
# ---------------------------------------------------------------------


class TestSubmitWarningWhenGroupPaused(_GroupsWorkerBase):
    def test_warning_mentions_group_when_group_is_paused(self):
        # Pause the group
        requests.post(f"{self.base_url}/api/groups/premium-video/pause")

        # Submit a job to a group-matching endpoint
        resp = requests.post(f"{self.base_url}/api/jobs", json={
            "endpoint": "https://kamui-code.ai/t2v/fal/veo3-pro",
            "submit_tool": "submit",
            "args": {"prompt": "test"},
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("warning", body)
        self.assertIn("Group premium-video", body["warning"])

    def test_warning_mentions_category_when_category_is_paused(self):
        # Confirm category-only path still warns about the category
        # (not a group). t2v/fal/some-other-model doesn't match the
        # premium-video pattern, so it routes to the t2v category.
        cl = self.worker_app.dispatcher.category_limiter
        cl.pause_category("t2v")

        resp = requests.post(f"{self.base_url}/api/jobs", json={
            "endpoint": "https://kamui-code.ai/t2v/fal/some-other-model",
            "submit_tool": "submit",
            "args": {"prompt": "test"},
        })
        body = resp.json()
        self.assertIn("warning", body)
        self.assertIn("Category t2v", body["warning"])


if __name__ == "__main__":
    unittest.main()
