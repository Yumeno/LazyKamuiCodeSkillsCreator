# -*- coding: utf-8 -*-
"""Tests for queue integration in generate_skill.py.

Covers queue_config generation, file copying, wrapper script updates,
and SKILL.md documentation.
"""
import sys
import os
import json
import tempfile
import shutil
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import generate_skill


# ── 6-1. queue_config Generation ────────────────────────────────────────

class TestGenerateQueueConfig(unittest.TestCase):
    """generate_queue_config() produces a valid config dict."""

    def test_default_structure(self):
        cfg = generate_skill.generate_queue_config("http://mcp:8000/sse")
        self.assertIn("host", cfg)
        self.assertIn("port", cfg)
        self.assertIn("idle_timeout_seconds", cfg)
        self.assertIn("default_rate_limit", cfg)

    def test_endpoint_in_rate_limits(self):
        ep = "http://mcp-server:8000/sse"
        cfg = generate_skill.generate_queue_config(ep)
        self.assertIn("endpoint_rate_limits", cfg)
        self.assertIn(ep, cfg["endpoint_rate_limits"])

    def test_required_keys(self):
        cfg = generate_skill.generate_queue_config("http://x:8000")
        rl = cfg["default_rate_limit"]
        self.assertIn("max_concurrent_jobs", rl)
        self.assertIn("min_interval_seconds", rl)

    def test_default_values_sensible(self):
        cfg = generate_skill.generate_queue_config("http://x:8000")
        self.assertEqual(cfg["host"], "127.0.0.1")
        self.assertIsInstance(cfg["port"], int)
        self.assertGreater(cfg["idle_timeout_seconds"], 0)


# ── 6-2. File Copy Tests ────────────────────────────────────────────────

class TestSkillFileCopying(unittest.TestCase):
    """Skill generation copies queue system files."""

    @classmethod
    def setUpClass(cls):
        """Generate a skill into a temp directory."""
        cls.tmpdir = tempfile.mkdtemp(prefix="test_skill_")

        # Minimal MCP config
        cls.mcp_config = {
            "name": "test-skill",
            "url": "http://mcp-server:8000/sse",
            "auth_header": "X-Auth",
            "auth_value": "test-token",
        }

        # Minimal tool definitions
        cls.tools = [
            {
                "name": "submit_job",
                "description": "Submit a job",
                "inputSchema": {
                    "type": "object",
                    "properties": {"prompt": {"type": "string"}},
                    "required": ["prompt"],
                },
            },
            {
                "name": "check_status",
                "description": "Check job status",
                "inputSchema": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                    "required": ["request_id"],
                },
            },
            {
                "name": "get_result",
                "description": "Get job result",
                "inputSchema": {
                    "type": "object",
                    "properties": {"request_id": {"type": "string"}},
                    "required": ["request_id"],
                },
            },
        ]

        cls.skill_dir = generate_skill.generate_skill_internal(
            mcp_config=cls.mcp_config,
            tools=cls.tools,
            output_dir=cls.tmpdir,
            skill_name="test-skill",
            lazy=False,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_skill_copies_job_queue_package(self):
        jq_dir = os.path.join(self.skill_dir, "scripts", "job_queue")
        self.assertTrue(os.path.isdir(jq_dir))
        for mod in ["__init__.py", "db.py", "dispatcher.py", "worker.py", "client.py"]:
            self.assertTrue(
                os.path.exists(os.path.join(jq_dir, mod)),
                f"Missing: job_queue/{mod}",
            )

    def test_skill_copies_worker_daemon(self):
        daemon = os.path.join(self.skill_dir, "scripts", "mcp_worker_daemon.py")
        self.assertTrue(os.path.exists(daemon))

    def test_skill_creates_queue_config(self):
        config_path = os.path.join(self.skill_dir, "queue_config.json")
        self.assertTrue(os.path.exists(config_path))
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("host", cfg)
        self.assertIn("port", cfg)

    def test_existing_mcp_async_call_still_copied(self):
        """Backward compat: mcp_async_call.py is still present."""
        self.assertTrue(
            os.path.exists(os.path.join(self.skill_dir, "scripts", "mcp_async_call.py"))
        )


# ── 6-3. Wrapper Script Tests ──────────────────────────────────────────

class TestWrapperQueueConfig(unittest.TestCase):
    """Wrapper script includes --queue-config in DEFAULTS."""

    def setUp(self):
        self.mcp_config = {
            "url": "http://mcp-server:8000/sse",
        }
        self.tools = [
            {"name": "submit_job", "description": "Submit", "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
            {"name": "check_status", "description": "Status", "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
            {"name": "get_result", "description": "Result", "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
        ]

    def test_wrapper_includes_queue_config_default(self):
        wrapper = generate_skill.generate_wrapper_script(
            self.mcp_config, self.tools, "test-skill"
        )
        self.assertIn("--queue-config", wrapper)

    def test_wrapper_queue_config_path_points_to_skill_root(self):
        wrapper = generate_skill.generate_wrapper_script(
            self.mcp_config, self.tools, "test-skill"
        )
        self.assertIn("queue_config.json", wrapper)

    def test_wrapper_uses_config_not_header(self):
        """Wrapper should use --config for auth, not --header."""
        wrapper = generate_skill.generate_wrapper_script(
            self.mcp_config, self.tools, "test-skill"
        )
        self.assertIn('"--config"', wrapper)
        self.assertIn("mcp.json", wrapper)
        self.assertNotIn('"--header"', wrapper)

    def test_wrapper_with_auth_uses_config_not_header(self):
        """Even with auth headers, wrapper should use --config, not --header."""
        mcp_config = {
            "url": "http://mcp-server:8000/sse",
            "all_headers": {"Authorization": "Bearer ${API_KEY}"},
            "auth_header": "Authorization",
            "auth_value": "Bearer ${API_KEY}",
        }
        wrapper = generate_skill.generate_wrapper_script(
            mcp_config, self.tools, "test-skill"
        )
        self.assertIn('"--config"', wrapper)
        self.assertNotIn('"--header"', wrapper)


# ── 6-4. SKILL.md Tests ────────────────────────────────────────────────

class TestSkillMdQueue(unittest.TestCase):
    """Generated SKILL.md includes queue system documentation."""

    def test_skill_md_contains_queue_section(self):
        mcp_config = {"url": "http://mcp:8000/sse", "name": "test"}
        tools = [
            {"name": "submit", "description": "Submit", "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "status", "description": "Status", "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "result", "description": "Result", "inputSchema": {"type": "object", "properties": {}, "required": []}},
        ]
        md = generate_skill.generate_skill_md(mcp_config, tools, "test-skill")
        # Should contain queue-related content
        self.assertTrue(
            "queue" in md.lower() or "Queue" in md,
            "SKILL.md should mention the queue system",
        )

    def test_skill_md_uses_config_not_header(self):
        """Generated SKILL.md should use --config, not --header."""
        mcp_config = {
            "url": "http://mcp:8000/sse",
            "name": "test",
            "all_headers": {"Authorization": "Bearer ${API_KEY}"},
            "auth_header": "Authorization",
            "auth_value": "Bearer ${API_KEY}",
        }
        tools = [
            {"name": "submit", "description": "Submit", "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "status", "description": "Status", "inputSchema": {"type": "object", "properties": {}, "required": []}},
            {"name": "result", "description": "Result", "inputSchema": {"type": "object", "properties": {}, "required": []}},
        ]
        md = generate_skill.generate_skill_md(mcp_config, tools, "test-skill")
        self.assertIn("--config", md)
        self.assertNotIn("--header", md)


# ── 6-5. Lazy Mode with Queue ──────────────────────────────────────────

class TestLazyModeWithQueue(unittest.TestCase):
    """Lazy mode should also include queue system files."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="test_lazy_queue_")
        cls.mcp_config = {
            "name": "lazy-test",
            "url": "http://mcp:8000/sse",
        }
        cls.tools = [
            {"name": "submit_job", "description": "Submit", "inputSchema": {"type": "object", "properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}},
            {"name": "check_status", "description": "Status", "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
            {"name": "get_result", "description": "Result", "inputSchema": {"type": "object", "properties": {"request_id": {"type": "string"}}, "required": ["request_id"]}},
        ]
        cls.skill_dir = generate_skill.generate_skill_internal(
            mcp_config=cls.mcp_config,
            tools=cls.tools,
            output_dir=cls.tmpdir,
            skill_name="lazy-test",
            lazy=True,
        )

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_lazy_mode_has_job_queue(self):
        jq_dir = os.path.join(self.skill_dir, "scripts", "job_queue")
        self.assertTrue(os.path.isdir(jq_dir))

    def test_lazy_mode_has_queue_config(self):
        self.assertTrue(
            os.path.exists(os.path.join(self.skill_dir, "queue_config.json"))
        )

    def test_lazy_mode_has_worker_daemon(self):
        self.assertTrue(
            os.path.exists(os.path.join(self.skill_dir, "scripts", "mcp_worker_daemon.py"))
        )


class TestConfigMergeProtection(unittest.TestCase):
    """Verify queue_config.json merge protection on re-generation."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.skill_dir = os.path.join(self.tmpdir, "test_skill")
        os.makedirs(os.path.join(self.skill_dir, "scripts"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_existing_config_preserves_user_settings(self):
        """Re-generation should preserve user-customized rate limit settings."""
        from generate_skill import _copy_queue_files

        # Create an existing config with user customizations
        existing_config = {
            "host": "127.0.0.1",
            "port": 54321,
            "idle_timeout_seconds": 120,  # User changed from default 60
            "default_rate_limit": {
                "max_concurrent_jobs": 1,  # User changed from default 2
                "min_interval_seconds": 5.0,  # User changed from default 10.0
            },
            "endpoint_rate_limits": {
                "http://old-endpoint:8000": {
                    "max_concurrent_jobs": 1,
                    "min_interval_seconds": 10.0,
                }
            },
        }
        config_path = os.path.join(self.skill_dir, "queue_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(existing_config, f)

        # Re-generate with a new endpoint
        scripts_dir = Path(os.path.join(self.skill_dir, "scripts"))
        _copy_queue_files(scripts_dir, Path(self.skill_dir), "http://new-endpoint:9000")

        # Read the merged config
        with open(config_path, encoding="utf-8") as f:
            merged = json.load(f)

        # User settings should be preserved
        self.assertEqual(merged["idle_timeout_seconds"], 120)
        self.assertEqual(merged["default_rate_limit"]["max_concurrent_jobs"], 1)
        self.assertEqual(merged["default_rate_limit"]["min_interval_seconds"], 5.0)

        # Old endpoint should be preserved
        self.assertIn("http://old-endpoint:8000", merged["endpoint_rate_limits"])

        # New endpoint should be added
        self.assertIn("http://new-endpoint:9000", merged["endpoint_rate_limits"])

    def test_new_config_created_from_scratch(self):
        """When no config exists, it should be created fresh."""
        from generate_skill import _copy_queue_files

        config_path = os.path.join(self.skill_dir, "queue_config.json")
        self.assertFalse(os.path.exists(config_path))

        scripts_dir = Path(os.path.join(self.skill_dir, "scripts"))
        _copy_queue_files(scripts_dir, Path(self.skill_dir), "http://test:8000")

        self.assertTrue(os.path.exists(config_path))
        with open(config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertIn("http://test:8000", cfg["endpoint_rate_limits"])

    def test_existing_endpoint_not_overwritten(self):
        """If the endpoint already exists in config, its limits should be preserved."""
        from generate_skill import _copy_queue_files

        existing_config = {
            "host": "127.0.0.1",
            "port": 54321,
            "idle_timeout_seconds": 60,
            "default_rate_limit": {
                "max_concurrent_jobs": 2,
                "min_interval_seconds": 2.0,
            },
            "endpoint_rate_limits": {
                "http://same:8000": {
                    "max_concurrent_jobs": 1,
                    "min_interval_seconds": 15.0,
                }
            },
        }
        config_path = os.path.join(self.skill_dir, "queue_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(existing_config, f)

        scripts_dir = Path(os.path.join(self.skill_dir, "scripts"))
        _copy_queue_files(scripts_dir, Path(self.skill_dir), "http://same:8000")

        with open(config_path, encoding="utf-8") as f:
            merged = json.load(f)

        # User's custom limit should be preserved, not overwritten by defaults
        self.assertEqual(
            merged["endpoint_rate_limits"]["http://same:8000"]["min_interval_seconds"],
            15.0,
        )


if __name__ == "__main__":
    unittest.main()
