---
name: i2i-kamui-easel-ai-fashion-size-estimator
description: MCP skill for i2i-kamui-easel-ai-fashion-size-estimator. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling i2i-kamui-easel-ai-fashion-size-estimator tools that require async processing.
---

# i2i-kamui-easel-ai-fashion-size-estimator

MCP integration for **i2i-kamui-easel-ai-fashion-size-estimator**.

## Endpoint

```
https://kamui-code.ai/i2i/fal/easel-ai-fashion-size-estimator
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

1. **Submit**: `easel_ai_fashion_size_estimator_i2i_submit` - Start job, returns request_id
2. **Status**: `easel_ai_fashion_size_estimator_i2i_status` - Poll job status with request_id
3. **Result**: `easel_ai_fashion_size_estimator_i2i_result` - Get result when completed

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
> Before executing any tool, you MUST read the full specification from `references/tools/i2i-kamui-easel-ai-fashion-size-estimator.yaml`.

**Quick reference** (name and description only):

- **easel_ai_fashion_size_estimator_i2i_result**: Get result of completed Easel AI Fashion Size Estimator including size (S/M/L/XL), height, bust, waist, and hip measurements.
- **easel_ai_fashion_size_estimator_i2i_status**: Check status of Easel AI Fashion Size Estimator processing using request ID.
- **easel_ai_fashion_size_estimator_i2i_submit**: Submit Easel AI Fashion Size Estimator request to estimate clothing size from person image and receive request ID for polling.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/i2i-kamui-easel-ai-fashion-size-estimator.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/i2i-kamui-easel-ai-fashion-size-estimator.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/i2i-kamui-easel-ai-fashion-size-estimator.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/i2i-kamui-easel-ai-fashion-size-estimator/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/i2i-kamui-easel-ai-fashion-size-estimator.yaml`
- MCP Config: `references/mcp.json`
