---
name: mcp-async-skill
description: Generate Skills from HTTP MCP servers with async job patterns (submit/status/result). Use when converting MCP specifications (.mcp.json) into reusable Skills using mcp_tool_catalog.yaml. Supports --lazy mode for context-saving generation. Also use for calling async MCP tools via JSON-RPC 2.0 with session-based polling.
---

# MCP Async Skill Generator

Generate reusable Skills from HTTP MCP servers that use async job patterns.

## When to Use

- Converting `.mcp.json` into a packaged Skill (tool info is fetched from catalog)
- Calling async MCP tools: submit → poll status → get result → download
- Integrating image/video generation MCPs (fal.ai, Replicate, etc.)

## File Upload (for image/audio/video inputs)

Many MCPs require URL inputs for media files. Use `fal_client` to upload local files:

```bash
# Upload file and get URL (one-liner)
python -c "import fal_client; url=fal_client.upload_file(r'/path/to/file.png'); print(f'URL: {url}')"

# Examples for different platforms:
# Windows
python -c "import fal_client; url=fal_client.upload_file(r'C:\Users\name\image.png'); print(f'URL: {url}')"

# Linux/Mac
python -c "import fal_client; url=fal_client.upload_file('/home/user/image.png'); print(f'URL: {url}')"

# Android (Termux)
python -c "import fal_client; url=fal_client.upload_file('/storage/emulated/0/Download/image.png'); print(f'URL: {url}')"
```

The returned URL (e.g., `https://v3b.fal.media/files/...`) can be used in `image_url`, `image_urls`, `audio_url`, etc. parameters.

**Supported formats:** png, jpg, jpeg, gif, webp, mp3, wav, mp4, webm, etc.

## Quick Start

### Generate Skill from MCP Config (Recommended)

Tool information is automatically fetched from `mcp_tool_catalog.yaml`:

```bash
# Generate skills for ALL servers in mcp.json
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json

# Generate skill for specific server(s) only
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  -s fal-ai/flux-lora

# Generate multiple specific servers
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  -s server1 -s server2
```

Output: `.claude/skills/<skill-name>/SKILL.md`

The server name in `.mcp.json` is used to look up tools from the catalog.

### Lazy Mode (Context-Saving)

For MCPs with many tools, use `--lazy` to minimize initial context consumption:

```bash
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --lazy
```

In lazy mode:
- SKILL.md contains only tool names and descriptions (no parameter details)
- Full tool definitions are stored in `references/tools/<skill>.yaml`
- AI reads YAML before execution to get parameters

### Generate Skill with Legacy tools.info

If you have a local `tools.info` file:

```bash
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --tools-info /path/to/tools.info \
  --name my-mcp-skill
```

### Specify Custom Output Directory

```bash
python scripts/generate_skill.py \
  --mcp-config /path/to/.mcp.json \
  --output /custom/path
```

### Direct Async Tool Call

```bash
python scripts/mcp_async_call.py \
  --endpoint "https://mcp.example.com/sse" \
  --submit-tool "generate_image" \
  --status-tool "check_status" \
  --result-tool "get_result" \
  --args '{"prompt": "a cat"}' \
  --output ./output
```

## Async Pattern Flow

```
1. SUBMIT    → POST JSON-RPC → Get session_id
2. STATUS    → Poll with session_id → Wait for "completed"
3. RESULT    → Get download URL
4. DOWNLOAD  → Save file locally
```

## JSON-RPC 2.0 Format

All MCP calls use this structure:

```json
{
  "jsonrpc": "2.0",
  "id": "unique-id",
  "method": "tools/call",
  "params": {
    "name": "tool_name",
    "arguments": { "key": "value" }
  }
}
```

## Input File Formats

### .mcp.json

**Multi-server format (recommended):**

```json
{
  "mcpServers": {
    "fal-ai/flux-lora": {
      "url": "https://mcp.example.com/flux-lora/sse",
      "headers": {
        "Authorization": "Bearer xxx"
      }
    },
    "fal-ai/video-enhance": {
      "url": "https://mcp.example.com/video-enhance/sse",
      "headers": {
        "Authorization": "Bearer xxx"
      }
    }
  }
}
```

With multi-server format:
- `python generate_skill.py -m mcp.json` → Generates skills for ALL servers
- `python generate_skill.py -m mcp.json -s fal-ai/flux-lora` → Generates only specified server
- `python generate_skill.py -m mcp.json -s server1 -s server2` → Multiple servers

**With environment variable placeholders:**

Headers can use `${VAR_NAME}` placeholders instead of plaintext credentials. Placeholders are preserved during skill generation and resolved at runtime:

```json
{
  "mcpServers": {
    "my-server": {
      "url": "https://mcp.example.com/sse",
      "headers": {
        "Authorization": "Bearer ${MCP_API_KEY}"
      }
    }
  }
}
```

Resolution order:
1. Environment variables (`os.environ`)
2. `.env` file (searched in CWD, then home directory)

`.env` file format:
```bash
# .env (place in project root, add to .gitignore)
MCP_API_KEY=sk-your-actual-key
```

**Single-server format:**

```json
{
  "name": "t2i-kamui-fal-flux-lora",
  "url": "https://kamui-code.ai/t2i/fal/flux-lora",
  "auth_header": "KAMUI-CODE-PASS",
  "auth_value": "your-pass"
}
```

### mcp_tool_catalog.yaml (Auto-fetched)

Tool information is fetched from:
`https://raw.githubusercontent.com/Yumeno/kamuicode-config-manager/main/mcp_tool_catalog.yaml`

The catalog contains 266+ servers with tool definitions:

```yaml
servers:
  - id: t2i-kamui-fal-flux-lora
    status: online
    tools:
      - name: flux_lora_submit
        description: Submit Flux LoRA image generation request
        inputSchema:
          properties:
            prompt:
              description: Image prompt
              type: string
          required:
            - prompt
          type: object
```

### tools.info (Legacy)

Optional, for backward compatibility:

```json
[
  {
    "name": "generate",
    "description": "Generate content",
    "inputSchema": {
      "type": "object",
      "properties": {
        "prompt": { "type": "string", "description": "Input prompt" }
      },
      "required": ["prompt"]
    }
  }
]
```

## Script Reference

### `scripts/mcp_async_call.py`

Main async MCP caller with full flow automation.

**Options:**
- `--endpoint, -e`: MCP server URL
- `--submit-tool`: Tool name for job submission
- `--status-tool`: Tool name for status checking
- `--result-tool`: Tool name for result retrieval
- `--args, -a`: Submit arguments as JSON string
- `--args-file`: Load arguments from JSON file
- `--output, -o`: Output directory (default: ./output)
- `--output-file, -O`: Output file path (overrides auto filename, allows overwrite)
- `--auto-filename`: Use `{request_id}_{timestamp}.{ext}` format
- `--poll-interval`: Seconds between polls (default: 2.0)
- `--max-polls`: Maximum poll attempts (default: 300)
- `--header`: Add custom header (format: `Key:Value`)
- `--config, -c`: Load endpoint from .mcp.json
- `--save-logs`: Save request/response logs to `{output}/logs/`
- `--save-logs-inline`: Save logs alongside output file as `{filename}_*.json`
- `--queue-config`: Path to queue_config.json (enables queue mode with rate limiting)
- `--worker-url`: Worker URL (default: from queue config)
- `--submit-only`: Submit job to queue and return `job_id` immediately
- `--wait JOB_ID`: Check job status once and return immediately
- `--list`: List all jobs in the queue (JSON output)
- `--stats`: Show per-endpoint statistics (JSON output)
- `--filter-status`: Filter jobs by status (used with `--list`)
- `--blocking`: Submit → poll → download in one step (default, backward compatible)

**File Extension Detection:**

Extension is determined in this order:
1. User-specified via `--output-file`
2. `Content-Type` header from download response
3. URL path extension
4. Warning if none detected

**Duplicate File Avoidance:**

When `--output-file` is not specified, existing files are not overwritten. A suffix is added:
- `output.png` → `output_1.png` → `output_2.png`

### `scripts/generate_skill.py`

Generate complete Skill from MCP specifications.

**Options:**
- `--mcp-config, -m`: Path to .mcp.json (required)
- `--servers, -s`: Server name(s) to generate (can specify multiple, default: all)
- `--tools-info, -t`: Path to tools.info (legacy mode, single server only)
- `--output, -o`: Output directory
- `--name, -n`: Skill name (auto-detected if omitted, single server only)
- `--catalog-url`: Custom catalog URL (default: GitHub raw URL)
- `--lazy, -l`: Generate minimal SKILL.md (tool definitions in references/tools/*.yaml)

**Requirements:**
- `pip install pyyaml requests` (for catalog fetching)

## Generated Skill Structure

Skills are generated to `.claude/skills/<skill-name>/`:

**Normal mode:**
```
.claude/skills/<skill-name>/
├── SKILL.md              # Usage documentation (full tool details)
├── queue_config.json     # Queue rate limit configuration
├── scripts/
│   ├── mcp_async_call.py # Core async caller
│   ├── mcp_worker_daemon.py # Queue worker daemon
│   ├── skill_name.py     # Convenience wrapper
│   └── job_queue/        # Queue system package
│       ├── db.py         # SQLite job store
│       ├── dispatcher.py # Per-endpoint rate limiting
│       ├── worker.py     # HTTP REST API server
│       └── client.py     # Queue client (submit/wait/blocking)
└── references/
    ├── mcp.json          # Original MCP config
    └── tools.json        # Original tool specs
```

**Lazy mode (`--lazy`):**
```
.claude/skills/<skill-name>/
├── SKILL.md              # Usage documentation (minimal)
├── queue_config.json     # Queue rate limit configuration
├── scripts/
│   ├── mcp_async_call.py # Core async caller
│   ├── mcp_worker_daemon.py # Queue worker daemon
│   ├── skill_name.py     # Convenience wrapper
│   └── job_queue/        # Queue system package
└── references/
    ├── mcp.json          # Original MCP config
    └── tools/
        └── <skill-name>.yaml  # Tool definitions + usage examples (YAML)
```

## Queue System (Non-Blocking Mode)

For parallel job submission with rate limiting per endpoint:

```bash
# Submit job and get job_id immediately
python scripts/mcp_async_call.py --submit-only \
  --endpoint "https://mcp.example.com/sse" \
  --submit-tool "generate_image" \
  --args '{"prompt": "a cat"}'
# Output: {"job_id": "abc-123", "status": "pending"}

# Check job status
python scripts/mcp_async_call.py --wait abc-123
# Output: {"job_id": "abc-123", "status": "completed", "result": {...}}
```

The local worker daemon starts automatically when needed and shuts down after idle timeout. Each endpoint has independent concurrency and interval limits configured via `queue_config.json`.

## Common Status Values

| Status | Meaning |
|--------|---------|
| `pending`, `queued` | Job waiting |
| `processing`, `running` | In progress |
| `completed`, `done`, `success` | Finished |
| `failed`, `error` | Failed |

## Programmatic Usage

```python
from scripts.mcp_async_call import run_async_mcp_job

result = run_async_mcp_job(
    endpoint="https://mcp.example.com/sse",
    submit_tool="generate",
    submit_args={"prompt": "sunset over mountains"},
    status_tool="status",
    result_tool="result",
    output_dir="./output",
    poll_interval=2.0,
    max_polls=300,
)

print(result["saved_path"])  # Path to downloaded file
```

## Queue System (Rate Limiting)

Generated skills include a local queue system that prevents overloading MCP servers when AI agents call tools in parallel.

**How it works:**
- A local worker daemon accepts job submissions via HTTP and dispatches them with per-endpoint rate limiting
- The worker starts automatically on first use and stops after idle timeout (default: 60s)
- Rate limits are configured per-endpoint in `queue_config.json`

**Queue modes:**

```bash
# Submit and wait for result (default, backward compatible)
python skill_name.py --args '{"prompt": "..."}'

# Submit only - returns job_id immediately
python skill_name.py --submit-only --args '{"prompt": "..."}'

# Check job status
python mcp_async_call.py --queue-config ../queue_config.json --wait JOB_ID

# List all jobs in the queue
python mcp_async_call.py --queue-config ../queue_config.json --list

# List only pending jobs
python mcp_async_call.py --queue-config ../queue_config.json --list --filter-status pending

# Show per-endpoint statistics
python mcp_async_call.py --queue-config ../queue_config.json --stats
```

**queue_config.json example:**

```json
{
  "port": 54321,
  "idle_timeout_seconds": 60,
  "default_rate_limit": { "max_concurrent_jobs": 2, "min_interval_seconds": 2.0 },
  "endpoint_rate_limits": { "http://slow-server:8000": { "max_concurrent_jobs": 1, "min_interval_seconds": 10.0 } }
}
```

## Error Handling

The script handles:
- JSON-RPC errors in response
- Job failures (status: failed/error)
- Timeout after max polls
- Download failures

All errors raise exceptions with descriptive messages.
