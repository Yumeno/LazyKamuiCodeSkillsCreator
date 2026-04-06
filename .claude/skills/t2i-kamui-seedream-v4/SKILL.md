---
name: t2i-kamui-seedream-v4
description: MCP skill for t2i-kamui-seedream-v4. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling t2i-kamui-seedream-v4 tools that require async processing.
---

# t2i-kamui-seedream-v4

MCP integration for **t2i-kamui-seedream-v4**.

## Endpoint

```
https://kamui-code.ai/t2i/fal/bytedance/seedream/v4
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

1. **Submit**: `bytedance_seedream_v4_text_to_image_submit` - Start job, returns request_id
2. **Status**: `bytedance_seedream_v4_text_to_image_status` - Poll job status with request_id
3. **Result**: `bytedance_seedream_v4_text_to_image_result` - Get result when completed

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
> Before executing any tool, you MUST read the full specification from `references/tools/t2i-kamui-seedream-v4.yaml`.

**Quick reference** (name and description only):

- **bytedance_seedream_v4_text_to_image_result**: Get result of completed ByteDance Seedream V4 Text-to-Image generation.
- **bytedance_seedream_v4_text_to_image_status**: Check status of ByteDance Seedream V4 Text-to-Image processing using request ID.
- **bytedance_seedream_v4_text_to_image_submit**: Submit ByteDance Seedream V4 Text-to-Image generation request using the latest Seedream 4.0 model with unified architecture for image generation.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/t2i-kamui-seedream-v4.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/t2i-kamui-seedream-v4.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/t2i-kamui-seedream-v4.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/t2i-kamui-seedream-v4/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/t2i-kamui-seedream-v4.yaml`
- MCP Config: `references/mcp.json`
