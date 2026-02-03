"""
MCP Application instance for Branch Monkey.

This module creates the FastMCP application instance that all tools register with.
"""

import sys

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: mcp package not installed.", file=sys.stderr)
    sys.exit(1)

# Create the MCP app instance
mcp = FastMCP("Branch Monkey")
