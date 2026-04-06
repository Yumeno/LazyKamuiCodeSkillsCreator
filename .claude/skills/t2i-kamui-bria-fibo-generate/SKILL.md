---
name: t2i-kamui-bria-fibo-generate
description: MCP skill for t2i-kamui-bria-fibo-generate. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling t2i-kamui-bria-fibo-generate tools that require async processing.
---

# t2i-kamui-bria-fibo-generate

MCP integration for **t2i-kamui-bria-fibo-generate**.

## Endpoint

```
https://kamui-code.ai/t2i/fal/bria/fibo/generate
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
> Before executing any tool, you MUST read the full specification from `references/tools/t2i-kamui-bria-fibo-generate.yaml`.

**Quick reference** (name and description only):

- **bria_fibo_generate_t2i_result**: Get result of completed Bria Fibo Generate image generation. Returns raw JSON response from Fal.ai API.
- **bria_fibo_generate_t2i_status**: Check status of Bria Fibo Generate image generation processing using request ID.
- **bria_fibo_generate_t2i_submit**: Submit Fal.ai Bria Fibo Generate text-to-image generation request and receive request ID for polling. Generate high-quality images from text prompts or structured prompts.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/t2i-kamui-bria-fibo-generate.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/t2i-kamui-bria-fibo-generate.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/t2i-kamui-bria-fibo-generate.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/t2i-kamui-bria-fibo-generate/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/t2i-kamui-bria-fibo-generate.yaml`
- MCP Config: `references/mcp.json`
