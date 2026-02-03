"""
Local Server Package for Branch Monkey Relay.

This package provides a FastAPI server that handles local Claude Code agent operations.
When combined with the relay client, it allows cloud users to run agents on local machines.

Public API:
- run_server: Start the FastAPI server
- set_default_working_dir: Set the default working directory for agent execution
- set_home_directory: Set the home directory (base directory passed to relay)
"""

from .app import app, run_server
from .config import set_default_working_dir, set_home_directory

__all__ = [
    "app",
    "run_server",
    "set_default_working_dir",
    "set_home_directory",
]
