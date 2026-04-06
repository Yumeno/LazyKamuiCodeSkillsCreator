---
name: i2i-kamui-qwen-lighting-restore
description: MCP skill for i2i-kamui-qwen-lighting-restore. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling i2i-kamui-qwen-lighting-restore tools that require async processing.
---

# i2i-kamui-qwen-lighting-restore

MCP integration for **i2i-kamui-qwen-lighting-restore**.

## Endpoint

```
https://kamui-code.ai/i2i/fal/qwen-image-edit-plus-lora-gallery/lighting-restoration
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

1. **Submit**: `qwen_image_edit_plus_lora_gallery_lighting_restoration_submit` - Start job, returns request_id
2. **Status**: `qwen_image_edit_plus_lora_gallery_lighting_restoration_status` - Poll job status with request_id
3. **Result**: `qwen_image_edit_plus_lora_gallery_lighting_restoration_result` - Get result when completed

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
> Before executing any tool, you MUST read the full specification from `references/tools/i2i-kamui-qwen-lighting-restore.yaml`.

**Quick reference** (name and description only):

- **qwen_image_edit_plus_lora_gallery_lighting_restoration_result**: Get the result of a completed Qwen Image Edit Plus LoRA Gallery Lighting Restoration request.
- **qwen_image_edit_plus_lora_gallery_lighting_restoration_status**: Check the status of a Qwen Image Edit Plus LoRA Gallery Lighting Restoration processing request.
- **qwen_image_edit_plus_lora_gallery_lighting_restoration_submit**: Submit a Qwen Image Edit Plus LoRA Gallery Lighting Restoration request. Restores natural lighting by removing harsh shadows and light spots. Removes harsh shadows, highlights, and directional lighting. Removes visible light spots and shadows. Applies neutral, soft illumination across the image. Relights the image with soft, even light. Perfect for restoring photos with harsh or uneven lighting, normalizing lighting across photo collections, removing unwanted lighting effects, and preparing images for consistent relighting. Note: This uses a fixed prompt optimized for lighting restoration - no prompt input needed. Works best with: Portrait and product photography.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/i2i-kamui-qwen-lighting-restore.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/i2i-kamui-qwen-lighting-restore.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/i2i-kamui-qwen-lighting-restore.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/i2i-kamui-qwen-lighting-restore/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/i2i-kamui-qwen-lighting-restore.yaml`
- MCP Config: `references/mcp.json`
