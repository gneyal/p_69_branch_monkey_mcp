"""
MCP Tools for Branch Monkey.

This package contains all MCP tool definitions organized by domain.
"""

# Import all tool modules to register them with the MCP app
from . import status
from . import projects
from . import tasks
from . import versions
from . import teams
from . import machines
from . import notes
from . import domains
from . import contexts
from . import agents

__all__ = [
    "status",
    "projects",
    "tasks",
    "versions",
    "teams",
    "machines",
    "notes",
    "domains",
    "contexts",
    "agents",
]
