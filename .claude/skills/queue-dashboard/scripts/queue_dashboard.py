#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Queue Dashboard — a local web UI for the MCP job queue.

Serves static HTML/CSS/JS and proxies a whitelisted set of ``/api/*`` requests
to the worker daemon (default http://127.0.0.1:54321), avoiding CORS issues
by keeping everything on the same origin.

Also provides dashboard-native endpoints:
  GET /api/skills — scan and return registered skill metadata
  POST /api/worker/start — start the worker daemon
  POST /api/worker/restart — restart the worker daemon

Usage:
    python queue_dashboard.py [--port 54322] [--worker-url http://127.0.0.1:54321]
"""
import argparse
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

# Force UTF-8 stdout/stderr on Windows
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")
except Exception:
    pass

DEFAULT_DASHBOARD_PORT = 54322
DEFAULT_WORKER_URL = "http://127.0.0.1:54321"
PROXY_TIMEOUT = 10.0
MAX_REQUEST_BODY = 1 * 1024 * 1024  # 1 MiB

STATIC_DIR = Path(__file__).parent / "static"

# ---- Whitelist of proxied API paths (to worker) ----------------------------
ALLOWED_GET = [
    re.compile(r"^/api/health$"),
    re.compile(r"^/api/stats$"),
    re.compile(r"^/api/categories$"),
    re.compile(r"^/api/config$"),
    re.compile(r"^/api/worker/status$"),
    re.compile(r"^/api/jobs(\?.*)?$"),
    re.compile(r"^/api/jobs/[A-Za-z0-9_\-]+(\?.*)?$"),
]
ALLOWED_POST = [
    re.compile(r"^/api/categories/(t2i|i2i|t2v|i2v|r2i|r2v)/(pause|resume)$"),
    re.compile(r"^/api/endpoints/resume$"),
    re.compile(r"^/api/worker/shutdown$"),
]
ALLOWED_PATCH = [
    re.compile(r"^/api/config$"),
]

# ---- Skill scanning --------------------------------------------------------

_skills_cache: dict | None = None
_skills_scanned_at: str | None = None


def _find_project_root() -> Path:
    """Walk upward from this script to find a directory containing .claude/ or .agents/."""
    current = Path(__file__).resolve().parent
    while True:
        for marker in (".claude", ".agents"):
            if (current / marker).is_dir():
                return current
        parent = current.parent
        if parent == current:
            return Path(__file__).resolve().parent
        current = parent


def _scan_skills(project_root: Path) -> dict:
    """Scan skill directories and return metadata."""
    items = []
    for marker in (".claude", ".agents"):
        skills_dir = project_root / marker / "skills"
        if not skills_dir.is_dir():
            continue
        try:
            entries = list(skills_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.is_file():
                continue
            # Parse frontmatter (first few KB)
            name = entry.name
            description = ""
            endpoint_url = ""
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")[:4096]
                if text.startswith("---"):
                    end = text.find("---", 3)
                    if end > 0:
                        fm = text[3:end]
                        for line in fm.splitlines():
                            if line.startswith("name:"):
                                name = line[5:].strip().strip("'\"")
                            elif line.startswith("description:"):
                                description = line[12:].strip().strip("'\"")
            except OSError:
                pass
            # Try to read endpoint from mcp.json
            mcp_json = entry / "references" / "mcp.json"
            if mcp_json.is_file():
                try:
                    mcp_data = json.loads(mcp_json.read_text(encoding="utf-8")[:8192])
                    endpoint_url = mcp_data.get("url", "")
                except (OSError, json.JSONDecodeError):
                    pass
            # Determine category from directory name prefix
            category = "other"
            for prefix in ("t2i", "i2i", "t2v", "i2v", "r2i", "r2v", "file-upload"):
                if entry.name.startswith(prefix + "-"):
                    category = prefix
                    break
            items.append({
                "id": entry.name,
                "source": marker,
                "category": category,
                "name": name,
                "description": description[:200],
                "endpoint_url": endpoint_url,
            })
    return {
        "scanned_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total": len(items),
        "items": sorted(items, key=lambda x: (x["category"], x["id"])),
    }


# ---- Worker management helpers ---------------------------------------------

def _find_worker_script(project_root: Path) -> str | None:
    """Locate mcp_worker_daemon.py from project root."""
    for marker in (".claude", ".agents"):
        candidate = project_root / marker / "skills" / "mcp-async-skill" / "scripts" / "mcp_worker_daemon.py"
        if candidate.is_file():
            return str(candidate)
    return None


def _is_worker_running(worker_url: str) -> bool:
    """Check worker health."""
    try:
        req = urllib.request.Request(f"{worker_url}/api/health", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_worker(project_root: Path, worker_url: str) -> dict:
    """Start the worker daemon as a background process."""
    script = _find_worker_script(project_root)
    if not script:
        return {"error": "Worker script (mcp_worker_daemon.py) not found"}
    if _is_worker_running(worker_url):
        return {"status": "already_running"}

    cmd = [sys.executable, script]
    kwargs = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)

    # Wait for worker to become reachable
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if _is_worker_running(worker_url):
            return {"status": "started"}
        time.sleep(0.3)
    return {"status": "start_timeout", "error": "Worker did not become reachable within 10s"}


def _restart_worker(project_root: Path, worker_url: str) -> dict:
    """Stop and restart the worker daemon."""
    # Send shutdown
    if _is_worker_running(worker_url):
        try:
            body = json.dumps({"timeout": 30}).encode()
            req = urllib.request.Request(
                f"{worker_url}/api/worker/shutdown",
                data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass
        # Wait for shutdown
        deadline = time.monotonic() + 35.0
        while time.monotonic() < deadline:
            if not _is_worker_running(worker_url):
                break
            time.sleep(0.5)

    return _start_worker(project_root, worker_url)


# ---- HTTP Handler ----------------------------------------------------------

class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Static file server with a whitelisted /api/* proxy."""

    worker_url = DEFAULT_WORKER_URL  # overwritten in main()
    project_root: Path = Path(".")  # overwritten in main()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format, *args):  # noqa: A002
        pass

    # --- HTTP methods ------------------------------------------------------

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/"):
            # Dashboard-native endpoints (not proxied)
            if self.path == "/api/skills" or self.path.startswith("/api/skills?"):
                return self._handle_skills()
            if not any(p.match(self.path) for p in ALLOWED_GET):
                self._send_json(403, {"error": "Path not allowed"})
                return
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/"):
            # Dashboard-native endpoints
            if self.path == "/api/worker/start":
                result = _start_worker(self.project_root, self.worker_url)
                code = 200 if result.get("status") in ("started", "already_running") else 500
                self._send_json(code, result)
                return
            if self.path == "/api/worker/restart":
                result = _restart_worker(self.project_root, self.worker_url)
                code = 200 if result.get("status") in ("started", "already_running") else 500
                self._send_json(code, result)
                return
            if not any(p.match(self.path) for p in ALLOWED_POST):
                self._send_json(403, {"error": "Path not allowed"})
                return
            return self._proxy("POST")
        self._send_json(404, {"error": "Not found"})

    def do_PATCH(self):  # noqa: N802
        if self.path.startswith("/api/"):
            if not any(p.match(self.path) for p in ALLOWED_PATCH):
                self._send_json(403, {"error": "Path not allowed"})
                return
            return self._proxy("PATCH")
        self._send_json(404, {"error": "Not found"})

    # --- Skills endpoint (dashboard-native) --------------------------------

    def _handle_skills(self):
        global _skills_cache, _skills_scanned_at
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        force_refresh = params.get("refresh", [None])[0] in ("true", "1")

        if _skills_cache is None or force_refresh:
            _skills_cache = _scan_skills(self.project_root)
            _skills_scanned_at = _skills_cache["scanned_at"]

        self._send_json(200, _skills_cache)

    # --- Proxy -------------------------------------------------------------

    def _proxy(self, method: str):
        target = self.worker_url + self.path
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_REQUEST_BODY:
            self._send_json(413, {"error": "Request body too large"})
            return
        body = self.rfile.read(length) if length else None

        fwd_headers = {}
        for h in ("Content-Type", "Accept"):
            v = self.headers.get(h)
            if v:
                fwd_headers[h] = v

        req = urllib.request.Request(
            target, data=body, method=method, headers=fwd_headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as resp:
                data = resp.read()
                self.send_response(resp.status)
                for h in ("Content-Type", "Content-Length"):
                    v = resp.headers.get(h)
                    if v:
                        self.send_header(h, v)
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            try:
                upstream_body = e.read() if e.fp else b""
            except Exception:
                upstream_body = b""
            self.send_response(e.code)
            content_type = "application/json"
            if e.headers is not None:
                content_type = e.headers.get("Content-Type", content_type)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(upstream_body)))
            self.end_headers()
            self.wfile.write(upstream_body)
        except urllib.error.URLError as e:
            self._send_json(502, {
                "error": "Worker unreachable",
                "detail": str(e.reason),
            })
        except Exception as e:
            self._send_json(500, {
                "error": "Proxy error",
                "detail": str(e),
            })

    def _send_json(self, code: int, data: dict):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ThreadingDashboardServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """HTTP server with per-request threading and fast restart support."""

    daemon_threads = True
    allow_reuse_address = True


def main():
    parser = argparse.ArgumentParser(description="Queue dashboard web UI")
    parser.add_argument("--port", type=int, default=DEFAULT_DASHBOARD_PORT,
                        help=f"Dashboard HTTP port (default: {DEFAULT_DASHBOARD_PORT})")
    parser.add_argument("--worker-url", default=DEFAULT_WORKER_URL,
                        help=f"Worker API base URL (default: {DEFAULT_WORKER_URL})")
    parser.add_argument("--no-open", action="store_true",
                        help="Do not auto-open the browser")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default: 127.0.0.1)")
    args = parser.parse_args()

    if not STATIC_DIR.is_dir():
        print(f"[Queue Dashboard] static dir not found: {STATIC_DIR}", file=sys.stderr)
        sys.exit(1)

    project_root = _find_project_root()
    DashboardHandler.worker_url = args.worker_url.rstrip("/")
    DashboardHandler.project_root = project_root

    url = f"http://{args.host}:{args.port}/"
    try:
        with ThreadingDashboardServer((args.host, args.port), DashboardHandler) as httpd:
            print(f"[Queue Dashboard] {url}")
            print(f"[Queue Dashboard] Worker API: {DashboardHandler.worker_url}")
            print(f"[Queue Dashboard] Project root: {project_root}")
            print("  Ctrl+C to stop")
            if not args.no_open:
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\n[Queue Dashboard] Shutting down")
    except OSError as e:
        print(f"[Queue Dashboard] Failed to bind {args.host}:{args.port}: {e}",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
