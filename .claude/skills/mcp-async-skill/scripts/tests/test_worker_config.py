# -*- coding: utf-8 -*-
"""Tests for worker.py ``GET /api/config`` and ``PATCH /api/config`` (#59).

Covers:

* GET response includes the new per-category ``limits.{cat}.{key}`` shape
  AND the legacy mirror keys (``category.max_inflight`` etc.) for
  lazy-v2.10.x dashboard compatibility.
* PATCH accepts the new per-category form.
* PATCH accepts the legacy flat form (fans out to ALL configured
  categories with an ``_legacy_warning`` in ``applied``).
* PATCH validates types: ``category.limits`` rejects non-dict
  (including explicit ``null``), ``category.limits.{cat}`` rejects
  non-dict, individual fields enforce min values.
* Three-way upgrade compatibility: legacy queue_config + new dispatcher
  + old dashboard read path keeps working.
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


class _CategoryConfigBase(unittest.TestCase):
    """Base class that boots a worker with a known per-category config."""

    config_override: dict | None = None

    def setUp(self):
        self.port = get_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

        config_dict = self.config_override or {
            "default_rate_limit": {
                "max_concurrent_jobs": 5,
                "min_interval_seconds": 0.0,
            },
            "category_rate_limits": {
                "categories": ["t2i", "i2i", "t2v", "i2v"],
                "limits": {
                    "t2i": {"max_inflight": 3, "min_interval": 0.0,
                             "exhaust_cooldown": 60},
                    "i2i": {"max_inflight": 2, "min_interval": 0.0,
                             "exhaust_cooldown": 120},
                    "t2v": {"max_inflight": 1, "min_interval": 0.0,
                             "exhaust_cooldown": 1800},
                    "i2v": {"max_inflight": 1, "min_interval": 0.0,
                             "exhaust_cooldown": 3600},
                },
            },
        }

        self.worker_app = worker.WorkerApp(
            host="127.0.0.1",
            port=self.port,
            db_path=":memory:",
            config_dict=config_dict,
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


class TestGetConfigShape(_CategoryConfigBase):
    """GET /api/config returns the new per-category shape with mirror keys."""

    def test_returns_per_category_limits(self):
        resp = requests.get(f"{self.base_url}/api/config")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        self.assertIn("category", data)
        self.assertIn("limits", data["category"])
        limits = data["category"]["limits"]
        self.assertEqual(set(limits.keys()), {"t2i", "i2i", "t2v", "i2v"})
        self.assertEqual(limits["t2i"]["max_inflight"], 3)
        self.assertEqual(limits["t2v"]["exhaust_cooldown"], 1800)

    def test_includes_legacy_mirror_keys_for_old_dashboard(self):
        """The pre-v2.11 dashboard reads ``cfg.category.max_inflight`` etc.
        directly. We mirror the first category's values into top-level
        keys so its UI does not go blank."""
        resp = requests.get(f"{self.base_url}/api/config")
        data = resp.json()

        self.assertIn("max_inflight", data["category"])
        self.assertIn("min_interval", data["category"])
        self.assertIn("exhaust_cooldown", data["category"])

        # First category alphabetically is "i2i" (max_inflight=2)
        self.assertEqual(data["category"]["max_inflight"], 2)
        self.assertEqual(data["category"]["exhaust_cooldown"], 120)


class TestPatchNewForm(_CategoryConfigBase):
    """PATCH /api/config with the new per-category shape."""

    def test_patch_single_category_max_inflight(self):
        body = {"category": {"limits": {"t2v": {"max_inflight": 5}}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("category.limits.t2v.max_inflight", data["applied"])
        self.assertEqual(data["applied"]["category.limits.t2v.max_inflight"], 5)
        self.assertEqual(data["rejected"], {})

        # Verify the change is reflected in subsequent GET
        cfg = requests.get(f"{self.base_url}/api/config").json()
        self.assertEqual(cfg["category"]["limits"]["t2v"]["max_inflight"], 5)
        # Other categories untouched
        self.assertEqual(cfg["category"]["limits"]["t2i"]["max_inflight"], 3)

    def test_patch_multiple_categories_in_one_request(self):
        body = {"category": {"limits": {
            "t2i": {"max_inflight": 7, "exhaust_cooldown": 300},
            "t2v": {"exhaust_cooldown": 7200},
        }}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        data = resp.json()
        self.assertEqual(data["rejected"], {})

        cfg = requests.get(f"{self.base_url}/api/config").json()
        self.assertEqual(cfg["category"]["limits"]["t2i"]["max_inflight"], 7)
        self.assertEqual(cfg["category"]["limits"]["t2i"]["exhaust_cooldown"], 300)
        self.assertEqual(cfg["category"]["limits"]["t2v"]["exhaust_cooldown"], 7200)


class TestPatchLegacyForm(_CategoryConfigBase):
    """PATCH /api/config with the legacy flat shape — applied to all
    configured categories with a warning in ``applied``."""

    def test_legacy_max_inflight_applies_to_all_categories(self):
        body = {"category": {"max_inflight": 9}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # _legacy_warning should be present
        self.assertIn("_legacy_warning", data["applied"])
        self.assertIn("legacy", data["applied"]["_legacy_warning"].lower())

        # The fan-out result should list affected categories
        applied = data["applied"]["category.max_inflight"]
        self.assertEqual(applied["value"], 9)
        self.assertEqual(set(applied["affected"]),
                         {"t2i", "i2i", "t2v", "i2v"})

        # All categories now have max_inflight=9
        cfg = requests.get(f"{self.base_url}/api/config").json()
        for cat in ["t2i", "i2i", "t2v", "i2v"]:
            self.assertEqual(
                cfg["category"]["limits"][cat]["max_inflight"], 9,
                msg=f"category {cat} should have max_inflight=9",
            )

    def test_legacy_does_not_overwrite_explicit_per_category(self):
        """If both shapes are in the same PATCH, the per-category
        ``limits`` block wins for the categories it covers."""
        body = {"category": {
            "limits": {"t2v": {"max_inflight": 1}},  # explicit
            "max_inflight": 5,  # legacy fan-out for everyone else
        }}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertEqual(resp.status_code, 200)

        cfg = requests.get(f"{self.base_url}/api/config").json()
        # t2v keeps its explicit value
        self.assertEqual(cfg["category"]["limits"]["t2v"]["max_inflight"], 1)
        # Others got the legacy fan-out
        self.assertEqual(cfg["category"]["limits"]["t2i"]["max_inflight"], 5)
        self.assertEqual(cfg["category"]["limits"]["i2i"]["max_inflight"], 5)
        self.assertEqual(cfg["category"]["limits"]["i2v"]["max_inflight"], 5)


class TestPatchRejection(_CategoryConfigBase):
    """PATCH /api/config validates types and value ranges."""

    def test_limits_must_be_dict_not_list(self):
        body = {"category": {"limits": [1, 2, 3]}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        data = resp.json()
        self.assertIn("category.limits", data["rejected"])
        self.assertIn("must be object", data["rejected"]["category.limits"])

    def test_limits_must_be_dict_not_int(self):
        body = {"category": {"limits": 42}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertIn("category.limits", resp.json()["rejected"])

    def test_limits_must_be_dict_not_string(self):
        body = {"category": {"limits": "all"}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertIn("category.limits", resp.json()["rejected"])

    def test_limits_explicit_null_rejected(self):
        """v3 fix M1: explicit ``null`` is NOT the same as "key absent".
        It is rejected to avoid silently masking config typos."""
        body = {"category": {"limits": None}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertIn("category.limits", resp.json()["rejected"])

    def test_per_category_value_must_be_dict(self):
        body = {"category": {"limits": {"t2v": [1, 2]}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertIn("category.limits.t2v", resp.json()["rejected"])

    def test_unknown_category_rejected(self):
        body = {"category": {"limits": {"foobar": {"max_inflight": 1}}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        rejected = resp.json()["rejected"]
        self.assertIn("category.limits.foobar", rejected)
        self.assertIn("unknown category", rejected["category.limits.foobar"])

    def test_max_inflight_must_be_int_ge_1(self):
        body = {"category": {"limits": {"t2v": {"max_inflight": 0}}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertIn("category.limits.t2v.max_inflight",
                      resp.json()["rejected"])

    def test_max_inflight_must_be_int_not_string(self):
        body = {"category": {"limits": {"t2v": {"max_inflight": "five"}}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertIn("category.limits.t2v.max_inflight",
                      resp.json()["rejected"])

    def test_exhaust_cooldown_must_be_non_negative(self):
        body = {"category": {"limits": {"t2v": {"exhaust_cooldown": -1}}}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertIn("category.limits.t2v.exhaust_cooldown",
                      resp.json()["rejected"])

    def test_partial_failure_applies_valid_and_rejects_invalid(self):
        """A PATCH with a mix of valid and invalid fields applies the
        valid ones and reports the invalid ones in ``rejected``."""
        body = {"category": {"limits": {
            "t2v": {
                "max_inflight": 4,        # valid
                "exhaust_cooldown": -1,   # invalid
            },
        }}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        data = resp.json()
        self.assertEqual(data["rejected"]["category.limits.t2v.exhaust_cooldown"],
                         "must be float >= 0")
        self.assertEqual(data["applied"]["category.limits.t2v.max_inflight"], 4)

        # The valid update is reflected
        cfg = requests.get(f"{self.base_url}/api/config").json()
        self.assertEqual(cfg["category"]["limits"]["t2v"]["max_inflight"], 4)
        # The invalid one didn't change anything
        self.assertEqual(cfg["category"]["limits"]["t2v"]["exhaust_cooldown"],
                         1800)


class TestUpgradeCompat(unittest.TestCase):
    """Three-way compatibility for users who upgrade mcp-async-skill but
    keep their lazy-v2.10.x ``queue_config.json`` and ``queue-dashboard``.

    The legacy queue_config below is exactly the shape produced by
    ``generate_queue_config()`` in lazy-v2.10.x.
    """

    def setUp(self):
        self.port = get_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"

        legacy_queue_config = {
            "default_rate_limit": {
                "max_concurrent_jobs": 5,
                "min_interval_seconds": 0.0,
            },
            "category_rate_limits": {
                "categories": ["t2i", "i2i", "t2v", "i2v"],
                "aliases": {"r2i": "i2i", "r2v": "i2v"},
                "min_interval": 1.0,
                "max_category_inflight": 1,
                "exhaust_cooldown": 3600,
            },
        }

        self.worker_app = worker.WorkerApp(
            host="127.0.0.1",
            port=self.port,
            db_path=":memory:",
            config_dict=legacy_queue_config,
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

    def test_legacy_queue_config_works_with_new_dispatcher(self):
        """Worker boots from a lazy-v2.10.x queue_config.json and
        exposes both new + legacy shape via /api/config."""
        cfg = requests.get(f"{self.base_url}/api/config").json()

        # New shape: every category has the legacy fan-out value
        for cat in ["t2i", "i2i", "t2v", "i2v"]:
            self.assertEqual(
                cfg["category"]["limits"][cat]["max_inflight"], 1)
            self.assertEqual(
                cfg["category"]["limits"][cat]["exhaust_cooldown"], 3600)

        # Mirror keys: present and consistent
        self.assertEqual(cfg["category"]["max_inflight"], 1)
        self.assertEqual(cfg["category"]["exhaust_cooldown"], 3600)

    def test_legacy_dashboard_patch_through_legacy_form_still_works(self):
        """Old dashboard sends ``{"category": {"max_inflight": 2}}``.
        New worker fans out to all categories and reports
        ``_legacy_warning`` so the change is visible in tooling."""
        body = {"category": {"max_inflight": 2}}
        resp = requests.patch(f"{self.base_url}/api/config", json=body)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("_legacy_warning", data["applied"])

        cfg = requests.get(f"{self.base_url}/api/config").json()
        for cat in ["t2i", "i2i", "t2v", "i2v"]:
            self.assertEqual(
                cfg["category"]["limits"][cat]["max_inflight"], 2)


if __name__ == "__main__":
    unittest.main()
