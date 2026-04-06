---
name: t2i-kamui-emu-3-5-image
description: MCP skill for t2i-kamui-emu-3-5-image. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling t2i-kamui-emu-3-5-image tools that require async processing.
---

# t2i-kamui-emu-3-5-image

MCP integration for **t2i-kamui-emu-3-5-image**.

## Endpoint

```
https://kamui-code.ai/t2i/fal/emu-3.5-image
```

## Authentication

This MCP requires the following headers:

```
KAMUI-CODE-PASS: ${KAMUI_CODE_PASS}
```

> Headers are automatically loaded from `references/mcp.json` via `--config`.

> **Warning:** Some header values were plaintext keys and have been masked.
> Set these environment variables or define them in a `.env` file:
> `KAMUI_CODE_PASS`

## Async Pattern

This MCP uses async job pattern:

1. **Submit**: `emu_3_5_image_t2i_submit` - Start job, returns request_id
2. **Status**: `emu_3_5_image_t2i_status` - Poll job status with request_id
3. **Result**: `emu_3_5_image_t2i_result` - Get result when completed

Use `scripts/mcp_async_call.py` for automated workflow.

## Available Tools

> **Note:** Detailed tool definitions are NOT included in this document to save context window.
> Before executing any tool, you MUST read the full specification from `references/tools/t2i-kamui-emu-3-5-image.yaml`.

**Quick reference** (name and description only):

- **emu_3_5_image_t2i_result**: Get result of completed Emu 3.5 Image Text-to-Image generation.
- **emu_3_5_image_t2i_status**: Check status of Emu 3.5 Image Text-to-Image processing using request ID.
- **emu_3_5_image_t2i_submit**: Submit Emu 3.5 Image Text-to-Image generation request and receive request ID for polling.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/t2i-kamui-emu-3-5-image.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/t2i-kamui-emu-3-5-image.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/t2i-kamui-emu-3-5-image.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/t2i-kamui-emu-3-5-image/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/t2i-kamui-emu-3-5-image.yaml`
- MCP Config: `references/mcp.json`
