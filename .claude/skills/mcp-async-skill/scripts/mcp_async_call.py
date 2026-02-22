#!/usr/bin/env python3
"""
MCP Async Tool Caller
Handles submit → status polling → result pattern for async MCP tools.
"""

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

# Force UTF-8 encoding for stdout/stderr/stdin (prevents UnicodeEncodeError on Windows)
# Python 3.7+ required. errors='backslashreplace' ensures no crash on unencodable chars.
try:
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding='utf-8', errors='backslashreplace')
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding='utf-8', errors='backslashreplace')
    if sys.stdin and hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding='utf-8', errors='backslashreplace')
except Exception:
    pass  # reconfigure failed, continue with default encoding

import requests

# ---------------------------------------------------------------------------
# Environment variable placeholder resolution for header values
# Supports ${VAR_NAME} syntax in --header values (e.g. "Bearer ${MCP_API_KEY}")
# ---------------------------------------------------------------------------
_PLACEHOLDER_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')
_dotenv_loaded = False
_dotenv_values: dict[str, str] = {}


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a .env file using only the standard library.

    Supports: KEY=VALUE, KEY="VALUE", KEY='VALUE', comments (#), blank lines.
    """
    result: dict[str, str] = {}
    try:
        with open(path, encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                if '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                # Strip surrounding quotes
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if key:
                    result[key] = value
    except (OSError, UnicodeDecodeError):
        pass
    return result


def _load_dotenv_simple() -> dict[str, str]:
    """Search for and load a .env file (CWD -> home directory).

    Uses python-dotenv if available, otherwise falls back to the built-in parser.
    Results are cached for the lifetime of the process.
    """
    global _dotenv_loaded, _dotenv_values
    if _dotenv_loaded:
        return _dotenv_values
    _dotenv_loaded = True

    search_dirs = [Path.cwd(), Path.home()]

    # Prefer python-dotenv if installed
    try:
        from dotenv import dotenv_values
        for search_dir in search_dirs:
            env_path = search_dir / ".env"
            if env_path.is_file():
                _dotenv_values = {
                    k: v for k, v in dotenv_values(env_path).items()
                    if v is not None
                }
                return _dotenv_values
    except ImportError:
        pass

    # Fallback: built-in parser
    for search_dir in search_dirs:
        env_path = search_dir / ".env"
        if env_path.is_file():
            _dotenv_values = _parse_dotenv(env_path)
            return _dotenv_values

    return _dotenv_values


def resolve_header_placeholders(headers: dict[str, str]) -> dict[str, str]:
    """Resolve ${VAR_NAME} placeholders in header values.

    Resolution order: os.environ -> .env file.
    Plaintext values pass through unchanged.
    Exits with error if any placeholder cannot be resolved.
    """
    resolved: dict[str, str] = {}
    missing_vars: list[str] = []

    for key, value in headers.items():
        if not _PLACEHOLDER_RE.search(value):
            resolved[key] = value
            continue

        dotenv = _load_dotenv_simple()

        def replacer(match: re.Match) -> str:
            var_name = match.group(1)
            # Environment variable takes precedence
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            # Fallback to .env file
            dotenv_val = dotenv.get(var_name)
            if dotenv_val is not None:
                return dotenv_val
            # Unresolved
            missing_vars.append(var_name)
            return match.group(0)

        resolved[key] = _PLACEHOLDER_RE.sub(replacer, value)

    if missing_vars:
        unique_missing = sorted(set(missing_vars))
        print(
            f"Error: Unresolved environment variable(s): {', '.join(unique_missing)}\n"
            f"Set them as environment variables or define in a .env file.",
            file=sys.stderr,
        )
        sys.exit(1)

    return resolved


# Content-Type to extension mapping
CONTENT_TYPE_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/mpeg": ".mpeg",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/flac": ".flac",
    "audio/aac": ".aac",
    "application/pdf": ".pdf",
    "application/json": ".json",
    "application/zip": ".zip",
    "text/plain": ".txt",
    "text/html": ".html",
    "text/csv": ".csv",
}


class MCPAsyncClient:
    """Client for async MCP tools using JSON-RPC 2.0."""

    def __init__(self, endpoint: str, headers: dict | None = None):
        self.endpoint = endpoint
        self.headers = headers or {"Content-Type": "application/json"}
        self.session_id: str | None = None
        self._initialized = False

    def initialize(self) -> dict:
        """
        Initialize MCP session.
        Must be called before any tool calls.
        Gets Mcp-Session-Id from response headers.
        """
        if self._initialized:
            return {"status": "already_initialized", "session_id": self.session_id}

        # Generate initial session ID for request
        initial_session_id = str(uuid.uuid4())
        headers = {**self.headers, "Mcp-Session-Id": initial_session_id}

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-async-client", "version": "1.0.0"},
            },
        }

        resp = requests.post(self.endpoint, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()

        # Get session ID from response headers (server may assign a new one)
        self.session_id = resp.headers.get("Mcp-Session-Id", initial_session_id)
        self.headers["Mcp-Session-Id"] = self.session_id
        self._initialized = True

        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"Initialize Error: {result['error']}")

        print(f"[INIT] MCP session initialized: {self.session_id}")
        return result.get("result", {})

    def _jsonrpc_request(self, method: str, params: dict) -> dict:
        """Send JSON-RPC 2.0 request."""
        # Auto-initialize if not done yet
        if not self._initialized:
            self.initialize()

        payload = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params,
        }
        resp = requests.post(self.endpoint, json=payload, headers=self.headers, timeout=60)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"JSON-RPC Error: {result['error']}")
        return result.get("result", {})

    def call_tool(self, tool_name: str, arguments: dict) -> dict:
        """Call MCP tool via tools/call method."""
        return self._jsonrpc_request("tools/call", {"name": tool_name, "arguments": arguments})

    def submit(self, tool_name: str, arguments: dict) -> str:
        """Submit async job, returns request_id (or session_id)."""
        result = self.call_tool(tool_name, arguments)
        # Extract request_id/session_id from various response formats
        # Support both request_id (fal.ai style) and session_id naming
        id_keys = ["request_id", "requestId", "session_id", "sessionId", "id", "job_id", "jobId"]

        if isinstance(result, dict):
            for key in id_keys:
                if key in result and result[key]:
                    return result[key]
            # Check nested content
            content = result.get("content", [])
            if content and isinstance(content, list):
                for item in content:
                    if isinstance(item, dict):
                        text = item.get("text", "")
                        try:
                            parsed = json.loads(text)
                            for key in id_keys:
                                if key in parsed and parsed[key]:
                                    return parsed[key]
                        except json.JSONDecodeError:
                            pass
        raise ValueError(f"Could not extract request_id/session_id from submit response: {result}")

    def check_status(self, status_tool: str, request_id: str) -> dict:
        """Check job status."""
        return self.call_tool(status_tool, {"request_id": request_id})

    def get_result(self, result_tool: str, request_id: str) -> dict:
        """Get job result."""
        return self.call_tool(result_tool, {"request_id": request_id})


def parse_status_response(result: dict) -> tuple[str, dict]:
    """Parse status response, returns (status, full_result)."""
    if isinstance(result, dict):
        status = result.get("status") or result.get("state", "unknown")
        # Check nested content
        content = result.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    try:
                        parsed = json.loads(text)
                        status = parsed.get("status") or parsed.get("state") or status
                        return status.lower(), parsed
                    except json.JSONDecodeError:
                        pass
        return status.lower(), result
    return "unknown", result


def extract_download_urls(result: dict) -> list[str]:
    """
    Recursively extract all download URLs from result response.

    Traverses the entire response structure to find all URL strings,
    regardless of key names. Handles nested dicts, lists, and JSON strings.
    """
    urls = []
    seen = set()  # Avoid duplicates

    def _extract(obj):
        if isinstance(obj, str):
            # Check if it's a URL
            if obj.startswith(("http://", "https://")) and obj not in seen:
                seen.add(obj)
                urls.append(obj)
            # Check if it's a JSON string that might contain URLs
            elif obj.startswith("{") or obj.startswith("["):
                try:
                    parsed = json.loads(obj)
                    _extract(parsed)
                except json.JSONDecodeError:
                    pass
        elif isinstance(obj, list):
            for item in obj:
                _extract(item)
        elif isinstance(obj, dict):
            for value in obj.values():
                _extract(value)

    _extract(result)
    return urls


def extract_download_url(result: dict) -> str | None:
    """Extract first download URL from result response (backward compat)."""
    urls = extract_download_urls(result)
    return urls[0] if urls else None


def get_extension_from_content_type(content_type: str) -> str:
    """Extract file extension from Content-Type header."""
    if not content_type:
        return ""
    # "image/png; charset=utf-8" → "image/png"
    mime = content_type.split(";")[0].strip().lower()
    return CONTENT_TYPE_MAP.get(mime, "")


def get_extension_from_url(url: str) -> str:
    """Extract file extension from URL path."""
    path = urlparse(url).path
    if path:
        _, ext = os.path.splitext(path)
        if ext and len(ext) <= 5:  # Reasonable extension length
            return ext.lower()
    return ""


def get_filename_from_url(url: str) -> str:
    """Extract filename from URL path."""
    path = urlparse(url).path
    if path:
        filename = os.path.basename(path)
        if filename:
            return filename
    return ""


def get_unique_filepath(filepath: str) -> str:
    """Get unique filepath by adding suffix if file exists."""
    if not os.path.exists(filepath):
        return filepath

    base, ext = os.path.splitext(filepath)
    counter = 1
    while os.path.exists(f"{base}_{counter}{ext}"):
        counter += 1
    return f"{base}_{counter}{ext}"


def resolve_output_path(
    output_dir: str | None,
    output_file: str | None,
    auto_filename: str,
    avoid_overwrite: bool = True,
) -> str:
    """
    Resolve final output file path.

    Args:
        output_dir: Output directory (--output)
        output_file: Output file path (--output-file)
        auto_filename: Auto-generated filename (used if output_file not specified)
        avoid_overwrite: If True and output_file not specified, add suffix to avoid overwrite

    Returns:
        Resolved file path
    """
    if output_file:
        if os.path.isabs(output_file) or os.path.dirname(output_file):
            # Absolute path or path with directory → use as-is
            filepath = output_file
        else:
            # Filename only → combine with output_dir
            base_dir = output_dir or "."
            filepath = os.path.join(base_dir, output_file)
        # User specified file → allow overwrite (no suffix)
    else:
        # Auto filename
        base_dir = output_dir or "./output"
        filepath = os.path.join(base_dir, auto_filename)
        # Auto filename → avoid overwrite
        if avoid_overwrite:
            filepath = get_unique_filepath(filepath)

    # Create parent directory if needed
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)

    return filepath


def generate_auto_filename(request_id: str | None, ext: str) -> str:
    """Generate auto filename with request_id and timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if request_id:
        # Sanitize request_id (remove special chars)
        safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in request_id[:32])
        return f"{safe_id}_{timestamp}{ext}"
    else:
        return f"output_{timestamp}{ext}"


def download_file(
    url: str,
    output_dir: str | None = None,
    output_file: str | None = None,
    headers: dict | None = None,
    request_id: str | None = None,
    auto_filename_mode: bool = False,
) -> str:
    """
    Download file from URL.

    Args:
        url: Download URL
        output_dir: Output directory (--output)
        output_file: Output file path (--output-file), if specified overwrite is allowed
        headers: HTTP headers for download request
        request_id: Request ID for auto filename generation
        auto_filename_mode: If True, use {request_id}_{timestamp}.{ext} format

    Returns:
        Path to saved file
    """
    resp = requests.get(url, headers=headers, stream=True, timeout=300)
    resp.raise_for_status()

    # Determine extension
    ext = ""
    if output_file:
        # User specified file → use its extension
        _, ext = os.path.splitext(output_file)

    if not ext:
        # Try Content-Type
        content_type = resp.headers.get("Content-Type", "")
        ext = get_extension_from_content_type(content_type)

    if not ext:
        # Try URL
        ext = get_extension_from_url(url)

    if not ext:
        # Warning: no extension detected
        print("[WARNING] Could not detect file extension", file=sys.stderr)

    # Determine filename
    if output_file:
        # User specified → use as-is
        auto_filename = os.path.basename(output_file)
    elif auto_filename_mode:
        # Auto filename mode
        auto_filename = generate_auto_filename(request_id, ext)
    else:
        # Try Content-Disposition
        cd = resp.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            auto_filename = cd.split("filename=")[-1].strip('"\'')
        else:
            # Try URL filename
            url_filename = get_filename_from_url(url)
            if url_filename and url_filename != "":
                auto_filename = url_filename
            else:
                # Fallback
                if request_id:
                    auto_filename = f"{request_id}{ext}"
                else:
                    auto_filename = f"output{ext}"

    # Resolve final path
    filepath = resolve_output_path(
        output_dir=output_dir,
        output_file=output_file,
        auto_filename=auto_filename,
        avoid_overwrite=(output_file is None),  # Avoid overwrite only for auto filenames
    )

    # Download
    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return filepath


def save_log_file(log_data: dict, filepath: str) -> str:
    """Save log data to JSON file."""
    parent = os.path.dirname(filepath)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(log_data, f, indent=2, ensure_ascii=False)
    return filepath


def save_logs(
    logs: dict,
    output_dir: str | None,
    saved_filepath: str | None,
    save_to_logs_dir: bool = False,
    save_inline: bool = False,
) -> list[str]:
    """
    Save request/response logs.

    Args:
        logs: Dict with keys like 'submit_request', 'submit_response', etc.
        output_dir: Output directory (--output)
        saved_filepath: Path to the downloaded file (for inline naming)
        save_to_logs_dir: Save to {output_dir}/logs/
        save_inline: Save alongside the downloaded file as {filename}_*.json

    Returns:
        List of saved log file paths
    """
    saved_paths = []

    if save_to_logs_dir:
        logs_dir = os.path.join(output_dir or "./output", "logs")
        for log_name, log_data in logs.items():
            log_path = os.path.join(logs_dir, f"{log_name}.json")
            saved_paths.append(save_log_file(log_data, log_path))

    if save_inline and saved_filepath:
        base, _ = os.path.splitext(saved_filepath)
        for log_name, log_data in logs.items():
            log_path = f"{base}_{log_name}.json"
            saved_paths.append(save_log_file(log_data, log_path))

    return saved_paths


def run_async_mcp_job(
    endpoint: str,
    submit_tool: str,
    submit_args: dict,
    status_tool: str,
    result_tool: str,
    output_dir: str | None = "./output",
    output_file: str | None = None,
    auto_filename: bool = False,
    poll_interval: float = 2.0,
    max_polls: int = 300,
    headers: dict | None = None,
    completed_statuses: list[str] | None = None,
    failed_statuses: list[str] | None = None,
    save_logs_to_dir: bool = False,
    save_logs_inline: bool = False,
) -> dict:
    """
    Execute async MCP job: submit → poll status → get result → download.

    Args:
        endpoint: MCP server endpoint URL
        submit_tool: Name of the submit tool
        submit_args: Arguments for the submit tool
        status_tool: Name of the status checking tool
        result_tool: Name of the result retrieval tool
        output_dir: Output directory (--output)
        output_file: Output file path (--output-file)
        auto_filename: If True, use {request_id}_{timestamp}.{ext} format
        poll_interval: Seconds between status polls
        max_polls: Maximum number of poll attempts
        headers: Additional HTTP headers (auth, etc.)
        completed_statuses: List of status strings indicating completion
        failed_statuses: List of status strings indicating failure
        save_logs_to_dir: Save logs to {output_dir}/logs/
        save_logs_inline: Save logs alongside downloaded file

    Returns dict with:
        - request_id: Job ID
        - status: Final job status
        - download_urls: List of all download URLs found
        - saved_paths: List of all saved file paths
        - download_url: First download URL (backward compat)
        - saved_path: First saved file path (backward compat)
        - log_paths: List of log file paths (if logging enabled)
    """
    completed_statuses = completed_statuses or ["completed", "done", "success", "finished", "ready"]
    failed_statuses = failed_statuses or ["failed", "error", "cancelled", "timeout"]

    client = MCPAsyncClient(endpoint, headers)

    # Logs collection
    logs = {}
    timestamp_now = lambda: datetime.now().isoformat()

    # Step 1: Submit
    print(f"[SUBMIT] Calling {submit_tool}...")
    logs["submit_request"] = {
        "timestamp": timestamp_now(),
        "tool": submit_tool,
        "arguments": submit_args,
    }
    request_id = client.submit(submit_tool, submit_args)
    logs["submit_response"] = {
        "timestamp": timestamp_now(),
        "tool": submit_tool,
        "request_id": request_id,
    }
    print(f"[SUBMIT] Request ID: {request_id}")

    # Step 2: Poll status
    print(f"[STATUS] Polling {status_tool}...")
    status = "pending"
    poll_count = 0
    status_result = {}

    while poll_count < max_polls:
        poll_count += 1
        status_resp = client.check_status(status_tool, request_id)
        status, status_result = parse_status_response(status_resp)
        print(f"[STATUS] Poll {poll_count}: {status}")

        if status in completed_statuses:
            print("[STATUS] Job completed!")
            break
        if status in failed_statuses:
            raise RuntimeError(f"Job failed with status: {status}, details: {status_result}")

        time.sleep(poll_interval)
    else:
        raise TimeoutError(f"Job did not complete within {max_polls} polls")

    logs["status_final"] = {
        "timestamp": timestamp_now(),
        "tool": status_tool,
        "poll_count": poll_count,
        "status": status,
        "response": status_result,
    }

    # Step 3: Get result
    print(f"[RESULT] Calling {result_tool}...")
    result_resp = client.get_result(result_tool, request_id)
    logs["result_response"] = {
        "timestamp": timestamp_now(),
        "tool": result_tool,
        "response": result_resp,
    }
    # Extract all download URLs
    download_urls = extract_download_urls(result_resp)

    if not download_urls:
        # Maybe URLs are in status response
        download_urls = extract_download_urls(status_result)

    if not download_urls:
        # Save logs even if no download URL
        log_paths = []
        if save_logs_to_dir or save_logs_inline:
            log_paths = save_logs(
                logs=logs,
                output_dir=output_dir,
                saved_filepath=None,
                save_to_logs_dir=save_logs_to_dir,
                save_inline=False,  # Can't do inline without a file
            )
        return {
            "request_id": request_id,
            "status": status,
            "result": result_resp,
            "note": "No download URL found in result",
            "log_paths": log_paths,
        }

    # Step 4: Download all files
    saved_paths = []
    for i, url in enumerate(download_urls):
        print(f"[DOWNLOAD] ({i + 1}/{len(download_urls)}) {url}")

        # For multiple files, add index suffix to output_file if specified
        current_output_file = output_file
        if output_file and len(download_urls) > 1:
            base, ext = os.path.splitext(output_file)
            current_output_file = f"{base}_{i + 1}{ext}"

        saved_path = download_file(
            url=url,
            output_dir=output_dir,
            output_file=current_output_file,
            headers=None,  # Don't use MCP headers for download
            request_id=request_id,
            auto_filename_mode=auto_filename,
        )
        saved_paths.append(saved_path)
        print(f"[DOWNLOAD] Saved to: {saved_path}")

    # Step 5: Save logs if requested
    log_paths = []
    if save_logs_to_dir or save_logs_inline:
        log_paths = save_logs(
            logs=logs,
            output_dir=output_dir,
            saved_filepath=saved_paths[0] if saved_paths else None,
            save_to_logs_dir=save_logs_to_dir,
            save_inline=save_logs_inline,
        )
        if log_paths:
            print(f"[LOGS] Saved {len(log_paths)} log file(s)")

    return {
        "request_id": request_id,
        "status": status,
        "download_urls": download_urls,
        "saved_paths": saved_paths,
        # Backward compatibility (first file)
        "download_url": download_urls[0] if download_urls else None,
        "saved_path": saved_paths[0] if saved_paths else None,
        "log_paths": log_paths,
    }


def load_mcp_config(config_path: str) -> dict:
    """Load .mcp.json configuration."""
    with open(config_path) as f:
        return json.load(f)


def resolve_worker_url(
    worker_url: str | None = None,
    queue_config_path: str | None = None,
) -> str | None:
    """Resolve the worker URL from explicit arg or queue config file.

    Returns None if neither is provided (direct execution mode).
    """
    if worker_url:
        return worker_url
    if queue_config_path and os.path.exists(queue_config_path):
        with open(queue_config_path, encoding="utf-8") as f:
            cfg = json.load(f)
        host = cfg.get("host", "127.0.0.1")
        port = cfg.get("port", 54321)
        return f"http://{host}:{port}"
    if queue_config_path:
        # Config path specified but file doesn't exist yet; use defaults
        return "http://127.0.0.1:54321"
    return None


def _queue_submit_only(
    worker_url: str,
    endpoint: str,
    submit_tool: str,
    submit_args: dict,
    status_tool: str | None = None,
    result_tool: str | None = None,
    headers: dict | None = None,
) -> dict:
    """Submit a job to the queue and return immediately."""
    from job_queue.client import submit_job
    return submit_job(
        worker_url=worker_url,
        endpoint=endpoint,
        submit_tool=submit_tool,
        args=submit_args,
        status_tool=status_tool,
        result_tool=result_tool,
        headers=headers,
    )


def _queue_wait(worker_url: str, job_id: str) -> dict:
    """Query a job's status from the queue."""
    from job_queue.client import wait_job
    return wait_job(worker_url=worker_url, job_id=job_id)


def _queue_list(worker_url: str, status_filter: str | None = None) -> dict:
    """List all jobs from the queue worker."""
    url = f"{worker_url}/api/jobs"
    if status_filter:
        url += f"?status={status_filter}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _queue_stats(worker_url: str) -> dict:
    """Get per-endpoint statistics from the queue worker."""
    resp = requests.get(f"{worker_url}/api/stats", timeout=10)
    resp.raise_for_status()
    return resp.json()


def _queue_blocking(
    worker_url: str,
    endpoint: str,
    submit_tool: str,
    submit_args: dict,
    status_tool: str | None = None,
    result_tool: str | None = None,
    headers: dict | None = None,
    poll_interval: float = 2.0,
    max_polls: int = 300,
) -> dict:
    """Submit a job and poll until completion."""
    from job_queue.client import blocking_job
    return blocking_job(
        worker_url=worker_url,
        endpoint=endpoint,
        submit_tool=submit_tool,
        args=submit_args,
        status_tool=status_tool,
        result_tool=result_tool,
        headers=headers,
        poll_interval=poll_interval,
        max_polls=max_polls,
    )


def route_execution(
    worker_url: str | None,
    queue_config_path: str | None,
    submit_only: bool,
    wait_job_id: str | None,
    list_jobs: bool = False,
    show_stats: bool = False,
    filter_status: str | None = None,
    endpoint: str | None = None,
    submit_tool: str | None = None,
    submit_args: dict | None = None,
    status_tool: str | None = None,
    result_tool: str | None = None,
    headers: dict | None = None,
    output_dir: str | None = None,
    output_file: str | None = None,
    auto_filename: bool = False,
    poll_interval: float = 2.0,
    max_polls: int = 300,
    id_param_name: str = "request_id",
    save_logs_to_dir: bool = False,
    save_logs_inline: bool = False,
) -> dict:
    """Route execution to queue system or direct MCP call."""
    submit_args = submit_args or {}
    headers = headers or {}

    # --list mode
    if list_jobs:
        url = worker_url or resolve_worker_url(None, queue_config_path) or "http://127.0.0.1:54321"
        return _queue_list(url, status_filter=filter_status)

    # --stats mode
    if show_stats:
        url = worker_url or resolve_worker_url(None, queue_config_path) or "http://127.0.0.1:54321"
        return _queue_stats(url)

    # --wait mode
    if wait_job_id:
        url = worker_url or resolve_worker_url(None, queue_config_path) or "http://127.0.0.1:54321"
        return _queue_wait(url, wait_job_id)

    # Queue mode (--queue-config or --worker-url provided)
    if worker_url or queue_config_path:
        url = worker_url or resolve_worker_url(None, queue_config_path)
        if submit_only:
            return _queue_submit_only(
                worker_url=url,
                endpoint=endpoint,
                submit_tool=submit_tool,
                submit_args=submit_args,
                status_tool=status_tool,
                result_tool=result_tool,
                headers=headers if headers != {"Content-Type": "application/json"} else None,
            )
        else:
            return _queue_blocking(
                worker_url=url,
                endpoint=endpoint,
                submit_tool=submit_tool,
                submit_args=submit_args,
                status_tool=status_tool,
                result_tool=result_tool,
                headers=headers if headers != {"Content-Type": "application/json"} else None,
                poll_interval=poll_interval,
                max_polls=max_polls,
            )

    # Direct execution mode (backward compatible)
    return run_async_mcp_job(
        endpoint=endpoint,
        submit_tool=submit_tool,
        submit_args=submit_args,
        status_tool=status_tool,
        result_tool=result_tool,
        output_dir=output_dir,
        output_file=output_file,
        auto_filename=auto_filename,
        poll_interval=poll_interval,
        max_polls=max_polls,
        headers=headers,
        id_param_name=id_param_name,
        save_logs_to_dir=save_logs_to_dir,
        save_logs_inline=save_logs_inline,
    )


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser (separated for testability)."""
    parser = argparse.ArgumentParser(
        description="Run async MCP job",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage (directory output, auto filename)
  python mcp_async_call.py --endpoint "..." --submit-tool "..." --output ./results/

  # Queue mode: submit only
  python mcp_async_call.py --queue-config queue_config.json --submit-only --submit-tool "..." --args '{...}'

  # Queue mode: check job status
  python mcp_async_call.py --queue-config queue_config.json --wait JOB_ID

  # Queue mode: blocking (submit + poll until done)
  python mcp_async_call.py --queue-config queue_config.json --submit-tool "..." --args '{...}'
"""
    )
    parser.add_argument("--config", "-c", help="Path to .mcp.json config file")
    parser.add_argument("--endpoint", "-e", help="MCP server endpoint URL")
    parser.add_argument("--submit-tool", help="Submit tool name")
    parser.add_argument("--status-tool", help="Status tool name")
    parser.add_argument("--result-tool", help="Result tool name")
    parser.add_argument("--args", "-a", help="Submit arguments as JSON string")
    parser.add_argument("--args-file", help="Submit arguments from JSON file")
    parser.add_argument("--output", "-o", default="./output", help="Output directory (default: ./output)")
    parser.add_argument("--output-file", "-O", help="Output file path (overrides auto filename)")
    parser.add_argument("--auto-filename", action="store_true", help="Use {request_id}_{timestamp}.{ext} format")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Poll interval in seconds")
    parser.add_argument("--max-polls", type=int, default=300, help="Maximum poll attempts")
    parser.add_argument("--header", action="append", help="Add header (format: Key:Value)")
    parser.add_argument("--save-logs", action="store_true", help="Save logs to {output}/logs/")
    parser.add_argument("--save-logs-inline", action="store_true", help="Save logs alongside output file")

    # Queue system options
    queue_group = parser.add_argument_group("Queue system")
    queue_group.add_argument("--queue-config", help="Path to queue_config.json (enables queue mode)")
    queue_group.add_argument("--worker-url", help="Worker URL (default: from queue config or http://127.0.0.1:54321)")
    queue_group.add_argument("--submit-only", action="store_true", help="Submit job and return job_id immediately")
    queue_group.add_argument("--wait", metavar="JOB_ID", help="Query job status by ID")
    queue_group.add_argument("--list", action="store_true", help="List all jobs in the queue")
    queue_group.add_argument("--stats", action="store_true", help="Show per-endpoint statistics")
    queue_group.add_argument("--filter-status", help="Filter jobs by status (used with --list)")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    # --list mode needs minimal args
    if args.list:
        worker_url = resolve_worker_url(args.worker_url, args.queue_config)
        result = _queue_list(worker_url or "http://127.0.0.1:54321", status_filter=args.filter_status)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # --stats mode needs minimal args
    if args.stats:
        worker_url = resolve_worker_url(args.worker_url, args.queue_config)
        result = _queue_stats(worker_url or "http://127.0.0.1:54321")
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # --wait mode needs minimal args
    if args.wait:
        worker_url = resolve_worker_url(args.worker_url, args.queue_config)
        result = _queue_wait(worker_url or "http://127.0.0.1:54321", args.wait)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    # Validate: submit-tool is required for non-wait modes
    if not args.submit_tool:
        parser.error("--submit-tool is required (unless using --wait)")

    # Direct mode requires status-tool and result-tool
    is_queue_mode = bool(args.queue_config or args.worker_url)
    if not is_queue_mode:
        if not args.status_tool:
            parser.error("--status-tool is required for direct mode (use --queue-config for queue mode)")
        if not args.result_tool:
            parser.error("--result-tool is required for direct mode (use --queue-config for queue mode)")

    # Load config
    endpoint = args.endpoint
    if args.config:
        config = load_mcp_config(args.config)
        endpoint = endpoint or config.get("url") or config.get("endpoint")

    if not endpoint:
        print("Error: Endpoint URL required (--endpoint or in --config)", file=sys.stderr)
        sys.exit(1)

    # Parse headers
    headers = {"Content-Type": "application/json"}
    if args.header:
        for h in args.header:
            key, val = h.split(":", 1)
            headers[key.strip()] = val.strip()

    # Resolve ${VAR_NAME} placeholders in header values
    headers = resolve_header_placeholders(headers)

    # Parse submit arguments
    submit_args = {}
    if args.args:
        submit_args = json.loads(args.args)
    elif args.args_file:
        with open(args.args_file, encoding='utf-8') as f:
            submit_args = json.load(f)

    # Resolve worker URL
    worker_url = resolve_worker_url(args.worker_url, args.queue_config)

    # Run
    result = route_execution(
        worker_url=worker_url,
        queue_config_path=args.queue_config,
        submit_only=args.submit_only,
        wait_job_id=None,
        endpoint=endpoint,
        submit_tool=args.submit_tool,
        submit_args=submit_args,
        status_tool=args.status_tool,
        result_tool=args.result_tool,
        headers=headers,
        output_dir=args.output,
        output_file=args.output_file,
        auto_filename=args.auto_filename,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
        headers=headers,
        save_logs_to_dir=args.save_logs,
        save_logs_inline=args.save_logs_inline,
    )

    print("\n=== Result ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
