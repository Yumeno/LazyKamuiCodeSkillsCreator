---
name: i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product
description: MCP skill for i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product tools that require async processing.
---

# i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product

MCP integration for **i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product**.

## Endpoint

```
https://kamui-code.ai/i2i/fal/qwen-image-edit-plus-lora-gallery/integrate-product
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

1. **Submit**: `qwen_image_edit_plus_lora_gallery_integrate_product_submit` - Start job, returns request_id
2. **Status**: `qwen_image_edit_plus_lora_gallery_integrate_product_status` - Poll job status with request_id
3. **Result**: `qwen_image_edit_plus_lora_gallery_integrate_product_result` - Get result when completed

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
> Before executing any tool, you MUST read the full specification from `references/tools/i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product.yaml`.

**Quick reference** (name and description only):

- **qwen_image_edit_plus_lora_gallery_integrate_product_result**: Get the result of a completed Qwen Image Edit Plus LoRA Gallery Integrate Product request.
- **qwen_image_edit_plus_lora_gallery_integrate_product_status**: Check the status of a Qwen Image Edit Plus LoRA Gallery Integrate Product processing request.
- **qwen_image_edit_plus_lora_gallery_integrate_product_submit**: Submit a Qwen Image Edit Plus LoRA Gallery Integrate Product request. Seamlessly blends products or elements into background images by automatically correcting perspective angles, adjusting lighting and shadows to match the scene, and creating natural integration for realistic product placement. Perfect for product photography and e-commerce, compositing objects into scenes, creating realistic mockups, and architectural visualization.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/i2i-kamui-qwen-image-edit-plus-lora-gallery-integrate-product.yaml`
- MCP Config: `references/mcp.json`
