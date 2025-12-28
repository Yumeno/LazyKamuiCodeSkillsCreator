#!/usr/bin/env python3
"""
MCP Async Tool Caller
Handles submit → status polling → result pattern for async MCP tools.
"""

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests


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

    def check_status(self, status_tool: str, request_id: str, id_param_name: str = "request_id") -> dict:
        """Check job status."""
        return self.call_tool(status_tool, {id_param_name: request_id})

    def get_result(self, result_tool: str, request_id: str, id_param_name: str = "request_id") -> dict:
        """Get job result."""
        return self.call_tool(result_tool, {id_param_name: request_id})


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


def extract_download_url(result: dict) -> str | None:
    """Extract download URL from result response."""
    if isinstance(result, dict):
        # Direct URL fields
        for key in ["url", "download_url", "downloadUrl", "output_url", "outputUrl", "result_url"]:
            if key in result:
                return result[key]

        # Check images array (fal.ai style)
        images = result.get("images", [])
        if images and isinstance(images, list) and len(images) > 0:
            first_image = images[0]
            if isinstance(first_image, dict) and "url" in first_image:
                return first_image["url"]

        # Check nested content
        content = result.get("content", [])
        if content and isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text", "")
                    try:
                        parsed = json.loads(text)
                        # Direct URL fields in parsed content
                        for key in ["url", "download_url", "downloadUrl", "output_url", "outputUrl", "result_url"]:
                            if key in parsed:
                                return parsed[key]
                        # Check images array in parsed content (fal.ai style)
                        images = parsed.get("images", [])
                        if images and isinstance(images, list) and len(images) > 0:
                            first_image = images[0]
                            if isinstance(first_image, dict) and "url" in first_image:
                                return first_image["url"]
                    except json.JSONDecodeError:
                        # Maybe the text itself is a URL
                        if text.startswith("http"):
                            return text
    return None


def download_file(url: str, output_path: str, headers: dict | None = None) -> str:
    """Download file from URL."""
    resp = requests.get(url, headers=headers, stream=True, timeout=300)
    resp.raise_for_status()

    # Determine filename
    if output_path and not os.path.isdir(output_path):
        filepath = output_path
    else:
        # Extract from Content-Disposition or URL
        filename = None
        cd = resp.headers.get("Content-Disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip('"\'')
        if not filename:
            filename = os.path.basename(urlparse(url).path) or "output"
        filepath = os.path.join(output_path or ".", filename)

    os.makedirs(os.path.dirname(filepath) or ".", exist_ok=True)
    with open(filepath, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)
    return filepath


def run_async_mcp_job(
    endpoint: str,
    submit_tool: str,
    submit_args: dict,
    status_tool: str,
    result_tool: str,
    output_path: str = "./output",
    poll_interval: float = 2.0,
    max_polls: int = 300,
    headers: dict | None = None,
    completed_statuses: list[str] | None = None,
    failed_statuses: list[str] | None = None,
    id_param_name: str = "request_id",
) -> dict:
    """
    Execute async MCP job: submit → poll status → get result → download.

    Args:
        endpoint: MCP server endpoint URL
        submit_tool: Name of the submit tool
        submit_args: Arguments for the submit tool
        status_tool: Name of the status checking tool
        result_tool: Name of the result retrieval tool
        output_path: Where to save downloaded files
        poll_interval: Seconds between status polls
        max_polls: Maximum number of poll attempts
        headers: Additional HTTP headers (auth, etc.)
        completed_statuses: List of status strings indicating completion
        failed_statuses: List of status strings indicating failure
        id_param_name: Parameter name for job ID (request_id or session_id)

    Returns dict with request_id, status, download_url, saved_path.
    """
    completed_statuses = completed_statuses or ["completed", "done", "success", "finished", "ready"]
    failed_statuses = failed_statuses or ["failed", "error", "cancelled", "timeout"]

    client = MCPAsyncClient(endpoint, headers)

    # Step 1: Submit
    print(f"[SUBMIT] Calling {submit_tool}...")
    request_id = client.submit(submit_tool, submit_args)
    print(f"[SUBMIT] Request ID: {request_id}")

    # Step 2: Poll status
    print(f"[STATUS] Polling {status_tool}...")
    status = "pending"
    poll_count = 0
    status_result = {}

    while poll_count < max_polls:
        poll_count += 1
        status_resp = client.check_status(status_tool, request_id, id_param_name)
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

    # Step 3: Get result
    print(f"[RESULT] Calling {result_tool}...")
    result_resp = client.get_result(result_tool, request_id, id_param_name)
    download_url = extract_download_url(result_resp)

    if not download_url:
        # Maybe result is already in status response
        download_url = extract_download_url(status_result)

    if not download_url:
        return {
            "request_id": request_id,
            "status": status,
            "result": result_resp,
            "note": "No download URL found in result",
        }

    # Step 4: Download
    print(f"[DOWNLOAD] {download_url}")
    saved_path = download_file(download_url, output_path)
    print(f"[DOWNLOAD] Saved to: {saved_path}")

    return {
        "request_id": request_id,
        "status": status,
        "download_url": download_url,
        "saved_path": saved_path,
    }


def load_mcp_config(config_path: str) -> dict:
    """Load .mcp.json configuration."""
    with open(config_path) as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description="Run async MCP job")
    parser.add_argument("--config", "-c", help="Path to .mcp.json config file")
    parser.add_argument("--endpoint", "-e", help="MCP server endpoint URL")
    parser.add_argument("--submit-tool", required=True, help="Submit tool name")
    parser.add_argument("--status-tool", required=True, help="Status tool name")
    parser.add_argument("--result-tool", required=True, help="Result tool name")
    parser.add_argument("--args", "-a", help="Submit arguments as JSON string")
    parser.add_argument("--args-file", help="Submit arguments from JSON file")
    parser.add_argument("--output", "-o", default="./output", help="Output directory/path")
    parser.add_argument("--poll-interval", type=float, default=2.0, help="Poll interval in seconds")
    parser.add_argument("--max-polls", type=int, default=300, help="Maximum poll attempts")
    parser.add_argument("--header", action="append", help="Add header (format: Key:Value)")
    parser.add_argument("--id-param", default="request_id", help="Job ID parameter name (request_id or session_id)")

    args = parser.parse_args()

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

    # Parse submit arguments
    submit_args = {}
    if args.args:
        submit_args = json.loads(args.args)
    elif args.args_file:
        with open(args.args_file) as f:
            submit_args = json.load(f)

    # Run
    result = run_async_mcp_job(
        endpoint=endpoint,
        submit_tool=args.submit_tool,
        submit_args=submit_args,
        status_tool=args.status_tool,
        result_tool=args.result_tool,
        output_path=args.output,
        poll_interval=args.poll_interval,
        max_polls=args.max_polls,
        headers=headers,
        id_param_name=args.id_param,
    )

    print("\n=== Result ===")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
