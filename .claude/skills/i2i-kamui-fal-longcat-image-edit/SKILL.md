---
name: i2i-kamui-fal-longcat-image-edit
description: MCP skill for i2i-kamui-fal-longcat-image-edit. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling i2i-kamui-fal-longcat-image-edit tools that require async processing.
---

# i2i-kamui-fal-longcat-image-edit

MCP integration for **i2i-kamui-fal-longcat-image-edit**.

## Endpoint

```
https://kamui-code.ai/i2i/fal/longcat-image/edit
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

1. **Submit**: `longcat_image_edit_submit` - Start job, returns request_id
2. **Status**: `longcat_image_edit_status` - Poll job status with request_id
3. **Result**: `longcat_image_edit_result` - Get result when completed

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
> Before executing any tool, you MUST read the full specification from `references/tools/i2i-kamui-fal-longcat-image-edit.yaml`.

**Quick reference** (name and description only):

- **longcat_image_edit_result**: Get the result JSON for a completed LongCat Image Edit request.
- **longcat_image_edit_status**: Check status of a LongCat Image Edit request using the request ID.
- **longcat_image_edit_submit**: Submit LongCat Image Edit request. Edit images with multilingual text rendering and photorealistic results.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/i2i-kamui-fal-longcat-image-edit.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/i2i-kamui-fal-longcat-image-edit.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/i2i-kamui-fal-longcat-image-edit.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/i2i-kamui-fal-longcat-image-edit/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/i2i-kamui-fal-longcat-image-edit.yaml`
- MCP Config: `references/mcp.json`
