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
- Pausing / resuming categories (t2i / i2i / t2v / i2v) AND custom groups
- Confirming that per-category and per-group rate limiting is behaving as expected
- Tuning per-category and per-group limits (`max_inflight`, `min_interval`, `exhaust_cooldown`) at runtime via the Settings panel

## How to Start

```bash
python .claude/skills/queue-dashboard/scripts/queue_dashboard.py
# → http://127.0.0.1:54322/ opens in the default browser
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--port` | 54322 | Dashboard HTTP port. **Pass `0` to let the OS pick a free port** when the default conflicts with another service. The actual port is printed on stdout as `PORT=NNNNN` (machine-parseable) followed by the human-readable URL. |
| `--worker-url` | `http://127.0.0.1:54321` | Worker API base URL |
| `--no-open` | false | Do not auto-open the browser |
| `--host` | 127.0.0.1 | Bind address (external binding requires explicit change) |

#### Example: dynamic port allocation

```bash
$ python .claude/skills/queue-dashboard/scripts/queue_dashboard.py --port 0 --no-open
PORT=49152
[Queue Dashboard] http://127.0.0.1:49152/
[Queue Dashboard] Worker API: http://127.0.0.1:54321
[Queue Dashboard] Project root: ...
  Ctrl+C to stop
```

A subprocess driver can grep for `^PORT=` in stdout to discover where the dashboard bound.

## Features

- **Summary cards**: pending / running / completed / failed counts
- **Category cards**: inflight, cooldown remaining, consecutive 429s, pause reason, pause/resume buttons
- **Custom Groups cards** *(lazy-v2.11.0+)*: same shape as Category cards, side-by-side in a two-column layout. Shows the matched endpoint patterns inline so you can confirm WHICH group is throttling a given URL. Pause/resume routes through `POST /api/groups/{name}/{action}`.
- **Settings panel** *(lazy-v2.11.0+)*:
  - **Per-category limits grid** — one row per t2i/i2i/t2v/i2v with `max_inflight` / `min_interval` / `exhaust_cooldown` inputs. Replaces the pre-PR1 single trio of inputs.
  - **Custom Groups limits grid** — one row per configured group. Group names are shown with the matched endpoint patterns as a tooltip.
  - Endpoint defaults + Worker idle timeout (unchanged from earlier versions).
- **Worker version label**: header shows whatever `/api/version` returned (PR2). Falls back to `(pre-v2.11.0)` when the worker pre-dates the version handshake endpoint.
- **Compatibility banner** *(lazy-v2.11.0+)*: when the worker is too old (no `cfg.category.limits`), the Settings panel shows a graceful-degrade banner with the exact `curl` to recover. The jobs list and stats continue to function.
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
  - GET: `/api/health`, `/api/stats`, `/api/categories`, `/api/groups`, `/api/config`, `/api/version`, `/api/worker/status`, `/api/jobs`, `/api/jobs/{id}`
  - POST: `/api/categories/{t2i|i2i|t2v|i2v|r2i|r2v}/{pause|resume}`, `/api/groups/{name}/{pause|resume}` (group `name` accepts `[A-Za-z0-9_\-.]+`), `/api/endpoints/resume`, `/api/worker/shutdown`
  - PATCH: `/api/config`
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
