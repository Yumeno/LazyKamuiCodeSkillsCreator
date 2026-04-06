#!/usr/bin/env python3
"""
i2i-kamui-chrono-edit - Wrapper with preset defaults for mcp_async_call.py

This wrapper pre-configures endpoint, tool names, and authentication.
Authentication headers are loaded from references/mcp.json via --config.
All mcp_async_call.py options are supported and can override defaults.

Usage:
    python i2i_kamui_chrono_edit.py --args '{"prompt": "..."}'
    python i2i_kamui_chrono_edit.py --args '{"prompt": "..."}' --auto-filename --save-logs
    python i2i_kamui_chrono_edit.py --help  # Show all available options
"""

import sys
import os

# Force UTF-8 encoding for stdout/stderr (prevents UnicodeEncodeError on Windows)
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
except Exception:
    pass

# Preset defaults (injected if not specified by user)
DEFAULTS = [
    ("--endpoint", "https://kamui-code.ai/i2i/fal/chrono-edit"),
    ("--submit-tool", "chrono_edit_submit"),
    ("--status-tool", "chrono_edit_status"),
    ("--result-tool", "chrono_edit_result"),
    ("--queue-config", os.path.join(os.path.dirname(os.path.dirname(__file__)), "queue_config.json")),
    ("--config", os.path.join(os.path.dirname(os.path.dirname(__file__)), "references", "mcp.json")),
]

def main():
    # Inject defaults into sys.argv (only if not already specified)
    args = sys.argv[1:]
    for key, value in DEFAULTS:
        if key not in args:
            args = [key, value] + args

    sys.argv = [sys.argv[0]] + args

    # Import and run mcp_async_call.py main
    sys.path.insert(0, os.path.dirname(__file__))
    from mcp_async_call import main as mcp_main
    mcp_main()


if __name__ == "__main__":
    main()
