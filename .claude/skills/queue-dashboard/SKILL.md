---
name: queue-dashboard
description: Web dashboard for monitoring the MCP job queue. Use when the user wants to visually inspect queue state, running jobs, category pause/resume status, or debug failed jobs. Starts a local HTTP server (default port 54322) and optionally opens a browser.
---

# Queue Dashboard

Browser-based dashboard for the MCP job queue, built with Python stdlib + Vanilla JS.

## When to Use

- Monitoring queue status in real time
- Inspecting pending / running / completed / failed jobs at a glance
- Debugging failed jobs (view full error details with args)
- Pausing / resuming categories (t2i / i2i / t2v / i2v)
- Confirming that rate limiting is behaving as expected

## How to Start

```bash
python .claude/skills/queue-dashboard/scripts/queue_dashboard.py
# → http://127.0.0.1:54322/ opens in the default browser
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | 54322 | Dashboard HTTP port |
| `--worker-url` | `http://127.0.0.1:54321` | Worker API base URL |
| `--no-open` | false | Do not auto-open the browser |
| `--host` | 127.0.0.1 | Bind address (external binding requires explicit change) |

## Features

- **Summary cards**: pending / running / completed / failed counts
- **Category cards**: inflight, cooldown remaining, consecutive 429s, pause reason, pause/resume buttons
- **Endpoint statistics table**: per-endpoint job counts
- **Recent jobs list**: status badges, filter by status, click-to-detail modal
- **Job detail modal**: full JSON including original args and error body
- **Auto refresh**: every 2 seconds (backs off on errors, throttles when tab hidden)
- **Error banner**: clear indication when the worker is unreachable

## Architecture

The dashboard is a small Python HTTP server that:

1. Serves static files from `scripts/static/` (HTML/CSS/JS)
2. Proxies a whitelisted set of `/api/*` requests to the worker (default port 54321)

Keeping the browser, static assets, and API on a single origin avoids CORS issues without modifying the worker.

### Security & Safety

- **Path whitelist**: only known endpoints are proxied
  - GET: `/api/health`, `/api/stats`, `/api/categories`, `/api/jobs`, `/api/jobs/{id}`
  - POST: `/api/categories/{t2i|i2i|t2v|i2v|r2i|r2v}/{pause|resume}`
- **Request body cap**: 1 MiB max
- **Upstream timeout**: 10 seconds per proxied request
- **Default bind**: `127.0.0.1` only (external binding requires explicit `--host`)
- **Threading HTTP server**: single-threaded stalls are avoided when the worker is slow

## Requirements

The MCP worker daemon (`mcp_worker_daemon.py`) must be running. If it's not,
the dashboard shows a red indicator and a top banner describing the failure.

To start the worker, either:
- Run any `mcp-async-skill`-generated skill with a job (auto-starts the worker), or
- Launch the worker manually via `python .claude/skills/mcp-async-skill/scripts/mcp_worker_daemon.py`

## Dependencies

**None beyond Python stdlib**: `http.server`, `socketserver`, `urllib.request`, `argparse`, `webbrowser`.
No JavaScript frameworks. No CSS frameworks.

## Files

```
queue-dashboard/
├── SKILL.md                # this file
└── scripts/
    ├── queue_dashboard.py  # HTTP server + /api/* proxy
    └── static/
        ├── index.html      # dashboard markup
        ├── dashboard.css   # dark theme styles
        └── dashboard.js    # polling client (vanilla JS)
```

## Platform Support

Works with both Claude Code and Codex CLI:

- **Claude Code**: Place in `.claude/skills/queue-dashboard/`
- **Codex CLI**: Place in `.agents/skills/queue-dashboard/`

The Python entry point discovers the worker on the standard port either way.
