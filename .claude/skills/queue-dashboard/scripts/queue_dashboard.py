#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Queue Dashboard — a local web UI for the MCP job queue.

Serves static HTML/CSS/JS and proxies a whitelisted set of ``/api/*`` requests
to the worker daemon (default http://127.0.0.1:54321), avoiding CORS issues
by keeping everything on the same origin.

Usage:
    python queue_dashboard.py [--port 54322] [--worker-url http://127.0.0.1:54321]
"""
import argparse
import http.server
import json
import re
import socketserver
import sys
import urllib.error
import urllib.request
import webbrowser
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

# ---- Whitelist of proxied API paths ---------------------------------------
ALLOWED_GET = [
    re.compile(r"^/api/health$"),
    re.compile(r"^/api/stats$"),
    re.compile(r"^/api/categories$"),
    re.compile(r"^/api/jobs(\?.*)?$"),
    re.compile(r"^/api/jobs/[A-Za-z0-9_\-]+(\?.*)?$"),
]
ALLOWED_POST = [
    re.compile(r"^/api/categories/(t2i|i2i|t2v|i2v|r2i|r2v)/(pause|resume)$"),
]


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    """Static file server with a whitelisted /api/* proxy."""

    worker_url = DEFAULT_WORKER_URL  # overwritten in main()

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def log_message(self, format, *args):  # noqa: A002 - signature fixed by base class
        # Quiet the per-request stderr logging
        pass

    # --- HTTP methods ------------------------------------------------------

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/"):
            if not any(p.match(self.path) for p in ALLOWED_GET):
                self._send_json(403, {"error": "Path not allowed"})
                return
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self):  # noqa: N802
        if self.path.startswith("/api/"):
            if not any(p.match(self.path) for p in ALLOWED_POST):
                self._send_json(403, {"error": "Path not allowed"})
                return
            return self._proxy("POST")
        self._send_json(404, {"error": "Not found"})

    # --- Proxy -------------------------------------------------------------

    def _proxy(self, method: str):
        target = self.worker_url + self.path
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length > MAX_REQUEST_BODY:
            self._send_json(413, {"error": "Request body too large"})
            return
        body = self.rfile.read(length) if length else None

        # Forward a conservative set of request headers
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
            # Relay upstream error as-is
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

    DashboardHandler.worker_url = args.worker_url.rstrip("/")

    url = f"http://{args.host}:{args.port}/"
    try:
        with ThreadingDashboardServer((args.host, args.port), DashboardHandler) as httpd:
            print(f"[Queue Dashboard] {url}")
            print(f"[Queue Dashboard] Worker API: {DashboardHandler.worker_url}")
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
