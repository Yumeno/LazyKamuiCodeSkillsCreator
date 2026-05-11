# Worker / client version. Used by:
# - worker.py        → advertised in every response via the X-Worker-Version
#                      header and via GET /api/version
# - mcp_async_call.py / job_queue/client.py
#                    → compared against the server's X-Worker-Version to
#                      detect "the worker daemon is stale, you upgraded
#                      mcp-async-skill but the previous worker is still
#                      running" situations
#
# This default value is rewritten in place at release time by the
# `Stamp version into __init__.py` step in `.github/workflows/release.yml`.
# The release workflow validates that the rewrite succeeded before tagging,
# so a missing or malformed `__version__` here will fail CI.
__version__ = "2.12.0"

# Default maximum number of poll attempts before timeout.
# With a default poll_interval of 2.0s, 3000 polls ≈ 100 minutes.
# Override via --max-polls CLI flag or by passing max_polls= to API functions.
DEFAULT_MAX_POLLS = 3000
