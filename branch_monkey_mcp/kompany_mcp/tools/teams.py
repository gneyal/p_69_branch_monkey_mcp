"""
Team member management tools.
"""

from .. import state
from ..api_client import api_get, api_post
from ..mcp_app import mcp


@mcp.tool()
def monkey_team_list() -> str:
    """List all team members for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/team-members?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        members = result.get("team_members", [])

        if not members:
            return f"No team members found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Team Members (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for m in members:
            output += f"- **{m.get('name')}** ({m.get('role') or 'member'})\n"

        return output
    except Exception as e:
        return f"Error fetching team: {str(e)}"


@mcp.tool()
def monkey_team_add(name: str, email: str = "", role: str = "", color: str = "#6366f1") -> str:
    """Add a new team member to the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_post("/api/team-members", {
            "name": name,
            "email": email,
            "role": role,
            "color": color,
            "project_id": state.CURRENT_PROJECT_ID
        })
        return f"✅ Added team member: {name} to project {state.CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error adding team member: {str(e)}"
