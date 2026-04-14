"""HeyNeuron extensions for the Google Ads MCP server.

This subpackage adds write operations, safety guardrails, and audit logging
to the upstream ``googleads/google-ads-mcp`` server. Importing this package
(either directly or via :mod:`ads_mcp.server`) registers the HeyNeuron tools
with the shared FastMCP coordinator.
"""

from ads_mcp.heyneuron import tools  # noqa: F401
