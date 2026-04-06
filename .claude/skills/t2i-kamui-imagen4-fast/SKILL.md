---
name: t2i-kamui-imagen4-fast
description: MCP skill for t2i-kamui-imagen4-fast. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling t2i-kamui-imagen4-fast tools that require async processing.
---

# t2i-kamui-imagen4-fast

MCP integration for **t2i-kamui-imagen4-fast**.

## Endpoint

```
https://kamui-code.ai/t2i/fal/imagen4/fast
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

1. **Submit**: `imagen4_fast_submit` - Start job, returns request_id
2. **Status**: `imagen4_fast_status` - Poll job status with request_id
3. **Result**: `imagen4_fast_result` - Get result when completed

Use `scripts/mcp_async_call.py` for automated workflow.

## Image Upload

Before editing images, you need to upload local files to get URLs. Use fal_client:

```bash
# Upload image
python -c "import fal_client; url=fal_client.upload_file(r'/path/to/file.png'); print(f'URL: {url}')"

# Windows path example
python -c "import fal_client; url=fal_client.upload_file(r'C:\Users\name\Pictures\photo.jpg'); print(f'URL: {url}')"

# Unix/Linux path example
python -c "import fal_client; url=fal_client.upload_file('/home/user/images/photo.png'); print(f'URL: {url}')"

# Android (Termux)
python -c "import fal_client; url=fal_client.upload_file('/storage/emulated/0/Download/image.png'); print(f'URL: {url}')"
```

The returned URL can be used in the `image_url or image_urls` parameter.

## Available Tools

> **Note:** Detailed tool definitions are NOT included in this document to save context window.
> Before executing any tool, you MUST read the full specification from `references/tools/t2i-kamui-imagen4-fast.yaml`.

**Quick reference** (name and description only):

- **imagen4_fast_result**: Get result and download completed Imagen4 Fast images to local directory.
- **imagen4_fast_status**: Check status of Imagen4 Fast processing using request ID.
- **imagen4_fast_submit**: Submit Fal.ai Imagen4 Fast text-to-image generation request and receive request ID for polling.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/t2i-kamui-imagen4-fast.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/t2i-kamui-imagen4-fast.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/t2i-kamui-imagen4-fast.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/t2i-kamui-imagen4-fast/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/t2i-kamui-imagen4-fast.yaml`
- MCP Config: `references/mcp.json`
