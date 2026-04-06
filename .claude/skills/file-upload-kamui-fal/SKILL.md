---
name: file-upload-kamui-fal
description: MCP skill for file-upload-kamui-fal. Provides async job execution with submit/status/result pattern via JSON-RPC 2.0. Use when calling file-upload-kamui-fal tools that require async processing.
---

# file-upload-kamui-fal

MCP integration for **file-upload-kamui-fal**.

## Endpoint

```
https://kamui-code.ai/uploader/fal
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

## Available Tools

> **Note:** Detailed tool definitions are NOT included in this document to save context window.
> Before executing any tool, you MUST read the full specification from `references/tools/file-upload-kamui-fal.yaml`.

**Quick reference** (name and description only):

- **kamui_file_upload_fal**: Upload a local file to FAL storage using local uploader scripts. Automatically handles setup if scripts are missing.

### How to Use Tools

1. **Read tool specification**: Use Read tool on `references/tools/file-upload-kamui-fal.yaml`
2. **Find the tool** you need and check its `required` parameters
3. **Execute** using `scripts/mcp_async_call.py` with appropriate arguments

## How to Execute

> Before executing any tool, **read the full specification** from `references/tools/file-upload-kamui-fal.yaml`.
> The YAML file contains `_usage` section with execution examples and CLI options.

```bash
# Read tool specification first
cat references/tools/file-upload-kamui-fal.yaml

# Then execute (example from _usage.bash in YAML)
python .claude/skills/file-upload-kamui-fal/scripts/mcp_async_call.py --help
```

## References

- Tool Specs & Usage: `references/tools/file-upload-kamui-fal.yaml`
- MCP Config: `references/mcp.json`
