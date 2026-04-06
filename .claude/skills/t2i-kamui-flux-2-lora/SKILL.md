---
name: t2i-kamui-flux-2-lora
description: MCP skill for t2i-kamui-flux-2-lora. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling t2i-kamui-flux-2-lora tools that require async processing.
---

# t2i-kamui-flux-2-lora

MCP integration for **t2i-kamui-flux-2-lora**.

## Endpoint

```
https://kamui-code.ai/t2i/fal/flux-2-lora
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

1. **Submit**: `flux_2_lora_submit` - Start job, returns request_id
2. **Status**: `flux_2_lora_status` - Poll job status with request_id
3. **Result**: `flux_2_lora_result` - Get result when completed

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
> Before executing any tool, you MUST read the full specification from `references/tools/t2i-kamui-flux-2-lora.yaml`.

**Quick reference** (name and description only):

- **flux_2_lora_result**: Get result of Flux-2 LoRA text-to-image generation. Returns URLs to the generated images.
- **flux_2_lora_status**: Check status of Flux-2 LoRA text-to-image processing using request ID.
- **flux_2_lora_submit**: Submit Fal.ai Flux-2 LoRA text-to-image generation request with custom LoRA weights and receive request ID for polling. Supports custom style adaptation and fine-tuned model variations.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/t2i-kamui-flux-2-lora.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/t2i-kamui-flux-2-lora.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/t2i-kamui-flux-2-lora.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/t2i-kamui-flux-2-lora/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/t2i-kamui-flux-2-lora.yaml`
- MCP Config: `references/mcp.json`
