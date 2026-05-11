# -*- coding: utf-8 -*-
"""Smoke tests for queue_dashboard (PR5 / #61 + #53).

The dashboard ships its own ``queue_dashboard.py`` entry point under
``.claude/skills/queue-dashboard/``. The two behaviours we need to pin
across releases live there:

1. ``--port 0`` (#53): when the user passes ``--port 0`` the OS picks
   a free port and the dashboard prints a ``PORT=NNNNN`` line on
   stdout. Tooling parses that line to know where to point a browser.
2. The proxy whitelist (PR5): ``/api/groups``, ``/api/version``, and
   ``POST /api/groups/{name}/{pause|resume}`` reach the worker. The
   pre-PR5 dashboard would 404 these because they were missing from
   ALLOWED_GET / ALLOWED_POST.

Both are integration-shaped (subprocess + HTTP) but cheap because we
run them against a mock worker on a random port and tear them down
inside the test.
"""
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests


REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "..")
)
DASHBOARD_PY = os.path.join(
    REPO_ROOT,
    ".claude", "skills", "queue-dashboard", "scripts", "queue_dashboard.py",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _MockWorkerHandler(BaseHTTPRequestHandler):
    """Records every proxied request so tests can assert pass-through."""

    received_paths: list[str] = []

    def log_message(self, format, *args):
        # Suppress default access-log noise in test output.
        pass

    def _send(self, status: int, body: dict):
        import json as _json
        data = _json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        # Mimic the worker's PR2 X-Worker-Version header so the
        # dashboard's loadConfig() can read it back.
        self.send_header("X-Worker-Version", "test-worker-v9.9.9")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        type(self).received_paths.append(("GET", self.path))
        if self.path == "/api/version":
            self._send(200, {
                "version": "test-worker-v9.9.9",
                "api_compatible_versions": ["test-worker-v9.9.9"],
                "server_time_utc": "2026-05-10T00:00:00.000000Z",
            })
            return
        if self.path == "/api/groups":
            self._send(200, {
                "server_time_utc": "2026-05-10T00:00:00.000000Z",
                "groups": {
                    "premium-video": {
                        "endpoints": ["https://kamui-code.ai/t2v/fal/veo3*"],
                        "max_inflight": 1,
                        "min_interval": 30,
                        "exhaust_cooldown": 7200,
                        "paused": False,
                        "inflight": 0,
                        "consecutive_429": 0,
                        "cooldown_remaining_s": 0,
                    },
                },
            })
            return
        if self.path == "/api/health":
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"error": "not found"})

    def do_POST(self):
        type(self).received_paths.append(("POST", self.path))
        if self.path.startswith("/api/groups/"):
            # Whitelist check is what we really care about here.
            self._send(200, {"group": "premium-video", "paused": True})
            return
        self._send(404, {"error": "not found"})


class _MockWorker:
    """Background HTTP server playing the worker's role."""

    def __init__(self):
        self.port = _free_port()
        # Each test gets its own subclass so received_paths doesn't bleed
        # between tests in the same process.
        self.handler_cls = type(
            "Handler", (_MockWorkerHandler,), {"received_paths": []},
        )
        self.server = HTTPServer(("127.0.0.1", self.port), self.handler_cls)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


class _DashboardSubprocess:
    """Launches queue_dashboard.py as a subprocess and parses its
    ``PORT=NNNNN`` line out of stdout."""

    def __init__(self, worker_url: str, requested_port: int = 0):
        self.worker_url = worker_url
        self.requested_port = requested_port
        self.proc: subprocess.Popen | None = None
        self.actual_port: int | None = None

    def start(self, deadline_s: float = 10.0):
        cmd = [
            sys.executable, DASHBOARD_PY,
            "--port", str(self.requested_port),
            "--worker-url", self.worker_url,
            "--no-open",
        ]
        # Force UTF-8 for the subprocess's stdout/stderr so reading the
        # 'PORT=NNNNN' line doesn't crash on Windows when the dashboard
        # logs a non-ASCII path (typical on this repo's Windows
        # checkouts where the project root contains Japanese
        # characters). cp932 + 0x87 is what the previous run hit.
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        # Read stdout line-by-line until we see the PORT= line.
        deadline = time.monotonic() + deadline_s
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                # Already exited — surface stdout to make the failure
                # message useful.
                remaining = self.proc.stdout.read() if self.proc.stdout else ""
                raise RuntimeError(
                    f"queue_dashboard exited with code {self.proc.returncode} "
                    f"before announcing port. Output:\n{remaining}"
                )
            line = self.proc.stdout.readline()
            if not line:
                time.sleep(0.05)
                continue
            m = re.match(r"PORT=(\d+)", line.strip())
            if m:
                self.actual_port = int(m.group(1))
                return
        raise TimeoutError(
            "queue_dashboard did not print 'PORT=NNNNN' within "
            f"{deadline_s}s"
        )

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()
                self.proc.wait()

    @property
    def url(self) -> str:
        if self.actual_port is None:
            raise RuntimeError("Dashboard hasn't started yet")
        return f"http://127.0.0.1:{self.actual_port}"


@unittest.skipUnless(
    os.path.isfile(DASHBOARD_PY),
    f"queue_dashboard.py not found at {DASHBOARD_PY}",
)
class TestPortZeroAllocation(unittest.TestCase):
    """PR5 (#53): ``--port 0`` makes the OS pick a free port. The
    dashboard prints ``PORT=NNNNN`` on stdout so a calling script can
    parse it without guessing or racing on the default 54322."""

    def test_dashboard_with_port_zero_announces_actual_port(self):
        worker = _MockWorker()
        worker.start()
        try:
            dashboard = _DashboardSubprocess(worker.url, requested_port=0)
            try:
                dashboard.start()
                # ``actual_port`` must be a valid TCP port and not 0.
                self.assertIsNotNone(dashboard.actual_port)
                self.assertGreater(dashboard.actual_port, 0)
                self.assertLessEqual(dashboard.actual_port, 65535)

                # Sanity: the dashboard actually serves on that port.
                resp = requests.get(f"{dashboard.url}/api/health", timeout=3)
                self.assertEqual(resp.status_code, 200)
            finally:
                dashboard.stop()
        finally:
            worker.stop()


@unittest.skipUnless(
    os.path.isfile(DASHBOARD_PY),
    f"queue_dashboard.py not found at {DASHBOARD_PY}",
)
class TestProxyWhitelist(unittest.TestCase):
    """PR5 (#61): ``/api/groups``, ``/api/version``, and group
    pause/resume endpoints are whitelisted in queue_dashboard.py and
    therefore reach the (mock) worker. A regression that drops them
    from ALLOWED_GET / ALLOWED_POST would surface as a 404 from the
    dashboard before the worker is consulted at all."""

    def setUp(self):
        self.worker = _MockWorker()
        self.worker.start()
        self.dashboard = _DashboardSubprocess(self.worker.url, requested_port=0)
        self.dashboard.start()

    def tearDown(self):
        self.dashboard.stop()
        self.worker.stop()

    def _get(self, path: str) -> requests.Response:
        return requests.get(self.dashboard.url + path, timeout=3)

    def _post(self, path: str) -> requests.Response:
        return requests.post(self.dashboard.url + path, timeout=3)

    def test_get_api_version_proxies_to_worker(self):
        resp = self._get("/api/version")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["version"], "test-worker-v9.9.9")
        # Confirm the worker actually saw the request (i.e. dashboard
        # didn't 404 before forwarding).
        self.assertIn(("GET", "/api/version"),
                      self.worker.handler_cls.received_paths)

    def test_get_api_groups_proxies_to_worker(self):
        resp = self._get("/api/groups")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("groups", body)
        self.assertIn(("GET", "/api/groups"),
                      self.worker.handler_cls.received_paths)

    def test_post_api_groups_pause_proxies_to_worker(self):
        resp = self._post("/api/groups/premium-video/pause")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(("POST", "/api/groups/premium-video/pause"),
                      self.worker.handler_cls.received_paths)

    def test_post_api_groups_resume_proxies_to_worker(self):
        resp = self._post("/api/groups/premium-video/resume")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(("POST", "/api/groups/premium-video/resume"),
                      self.worker.handler_cls.received_paths)

    def test_post_api_groups_with_dotted_name_proxies(self):
        """The ALLOWED_POST regex permits ``[A-Za-z0-9_\\-.]+`` so a
        group named ``foo.bar`` survives the whitelist. Don't break
        this without intent — the worker's group name validation is
        more lenient than this regex on purpose."""
        resp = self._post("/api/groups/foo.bar/pause")
        # Dashboard whitelist OK → request reaches our mock worker
        self.assertIn(("POST", "/api/groups/foo.bar/pause"),
                      self.worker.handler_cls.received_paths)

    def test_post_to_unknown_path_is_blocked_locally(self):
        """Sanity: requests outside the whitelist do NOT reach the
        worker. The dashboard short-circuits with 403 / 404 / 405
        (any non-2xx is fine — the assertion that matters is that
        the worker never saw it)."""
        before = list(self.worker.handler_cls.received_paths)
        resp = self._post("/api/totally-bogus")
        # Anything in the 4xx range proves the dashboard refused to
        # forward; the exact code is an implementation detail.
        self.assertGreaterEqual(resp.status_code, 400)
        self.assertLess(resp.status_code, 500)
        # Worker never saw it
        self.assertEqual(self.worker.handler_cls.received_paths, before)


class TestJobDetailUrlSurfacing(unittest.TestCase):
    """lazy-v2.12.0: Job detail modal exposes Inputs / Outputs URLs
    extracted by walking ``args`` and ``result`` JSON. The actual URL
    walker is in dashboard.js so we cannot unit-test it from Python,
    but we can pin a few static invariants so a regression in the
    static assets is caught here:

    1. ``index.html`` uses a ``<div id="job-detail-content">`` rather
       than a ``<pre>``. The earlier pre-formatted version is incompat
       with the new flex layout for the Inputs / Outputs sections.
    2. ``dashboard.js`` exports an ``extractUrls`` symbol — the helper
       any future test (or future Python smoke harness driving a
       headless browser) would want to call.
    3. ``dashboard.css`` carries the ``.url-section`` ruleset so the
       Inputs / Outputs visual styling actually ships with releases.
    """

    STATIC_DIR = os.path.join(
        REPO_ROOT, ".claude", "skills", "queue-dashboard", "scripts", "static",
    )

    def _read(self, name: str) -> str:
        path = os.path.join(self.STATIC_DIR, name)
        with open(path, "r", encoding="utf-8") as fp:
            return fp.read()

    def test_index_html_uses_div_for_job_detail_content(self):
        html = self._read("index.html")
        self.assertIn('<div id="job-detail-content">', html)
        # No leftover <pre> form: that would override the new flex layout
        self.assertNotIn('<pre id="job-detail-content"', html)

    def test_dashboard_js_defines_extract_urls(self):
        js = self._read("dashboard.js")
        # The walker is called extractUrls(...) by both showJobDetail
        # and (eventually) any future automated harness.
        self.assertRegex(js, r"function\s+extractUrls\s*\(")
        # Inputs and Outputs section titles ship in the JS — they are
        # what the user sees first in the modal.
        self.assertIn("Inputs (URLs in submit args)", js)
        self.assertIn("Outputs (URLs in result)", js)

    def test_dashboard_js_handles_kamui_double_encoded_result(self):
        """kamui-code MCP wraps the actual result payload as a JSON
        string inside `remote_result.content[].text`. The walker has to
        try-parse string nodes that begin with `{` or `[`, otherwise
        the output URL (`images[].url` / `video.url`) is invisible to
        the user — which is exactly the failure mode the lazy-v2.12.0
        feature is designed to prevent. Sample shape from real jobs:

            "remote_result": {
              "content": [
                {"type": "text",
                 "text": "{\\"video\\":{\\"url\\":\\"https://...\\"}}"}
              ]
            }
        """
        js = self._read("dashboard.js")
        # The walker labels embedded JSON paths with "(parsed text)".
        # Future-proofing: if the label changes, this test catches it.
        self.assertIn("(parsed text)", js)

    def test_dashboard_js_detects_local_paths(self):
        """kamui-code results carry `local_files` with Windows or
        POSIX absolute paths. The walker exposes those as a separate
        'local' entry type so the user can copy-paste them into
        Explorer / Finder. We pin the helper name to catch accidental
        deletion."""
        js = self._read("dashboard.js")
        self.assertRegex(js, r"function\s+looksLikeLocalPath\s*\(")
        # The 'local' kind needs to be wired through to the renderer
        # — without this distinction the local path would silently get
        # the 'url' kind and produce a dead <a href>.
        self.assertRegex(js, r"type:\s*\"local\"")

    def test_dashboard_css_has_url_section_styling(self):
        css = self._read("dashboard.css")
        self.assertIn(".url-section", css)
        self.assertIn(".url-thumb", css)
        # The 4 URL kind variants need their accent borders or the
        # classification UX silently falls back to all-grey.
        for kind in ("image", "video", "audio", "other"):
            self.assertIn(f".url-kind-{kind}", css)


if __name__ == "__main__":
    unittest.main()
