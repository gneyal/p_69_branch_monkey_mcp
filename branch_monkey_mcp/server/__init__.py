"""
Branch Monkey MCP Server Package.

MCP server for Claude Code that connects to Branch Monkey Cloud.
No API key needed - authenticates via browser approval.

Usage:
    Add to .mcp.json in your project:
        {
            "mcpServers": {
                "branch-monkey-cloud": {
                    "command": "uvx",
                    "args": ["--from", "git+https://github.com/gneyal/p_69_branch_monkey_mcp.git", "branch-monkey-mcp"],
                    "env": {
                        "BRANCH_MONKEY_API_URL": "https://p-63-branch-monkey.pages.dev"
                    }
                }
            }
        }

On first run, a browser opens for you to log in and approve. Token is saved for future use.
"""

import sys
import os

# Import state and auth modules first
from . import state
from .auth import load_stored_token, device_code_flow, save_token

# Initialize authentication
if not os.environ.get("BRANCH_MONKEY_API_KEY"):
    stored = load_stored_token(state.API_URL)
    if stored:
        state.API_KEY = stored.get("access_token")
        state.ORG_ID = stored.get("org_id")
    else:
        auth_result = device_code_flow(state.API_URL)
        if auth_result:
            state.API_KEY = auth_result.get("access_token")
            state.ORG_ID = auth_result.get("org_id")
            save_token(state.API_KEY, state.API_URL, state.ORG_ID)
        else:
            print("\n" + "=" * 60, file=sys.stderr)
            print("  AUTHENTICATION FAILED", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            print("\nCould not authenticate with Branch Monkey Cloud.", file=sys.stderr)
            print("\nPossible reasons:", file=sys.stderr)
            print("  - Browser approval was denied or timed out", file=sys.stderr)
            print("  - Network connectivity issues", file=sys.stderr)
            print(f"  - Unable to reach {state.API_URL}", file=sys.stderr)
            print("\nTo try again:", file=sys.stderr)
            print("  1. Restart Claude Code", file=sys.stderr)
            print("  2. Or use the `monkey_login` tool after startup", file=sys.stderr)
            print("=" * 60 + "\n", file=sys.stderr)
            sys.exit(1)
else:
    state.API_KEY = os.environ.get("BRANCH_MONKEY_API_KEY")

# Import MCP app instance
from .mcp_app import mcp

# Import all tools to register them with the MCP app
from . import tools


def main():
    """Run the MCP server."""
    print(f"Branch Monkey MCP starting...", file=sys.stderr)
    print(f"Connecting to: {state.API_URL}", file=sys.stderr)
    mcp.run()


__all__ = ["mcp", "main"]
