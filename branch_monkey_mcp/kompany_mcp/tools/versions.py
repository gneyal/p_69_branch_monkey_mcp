"""
Version management tools.
"""

from .. import state
from ..api_client import api_get, api_post
from ..mcp_app import mcp


@mcp.tool()
def monkey_version_list() -> str:
    """List all versions for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/versions?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        versions = result.get("versions", [])

        if not versions:
            return f"No versions found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Versions (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for v in versions:
            locked = " 🔒" if v.get("locked") else ""
            output += f"- **{v.get('key')}**: {v.get('label')}{locked}\n"

        return output
    except Exception as e:
        return f"Error fetching versions: {str(e)}"


@mcp.tool()
def monkey_version_create(key: str, label: str, description: str = "", sort_order: int = 0) -> str:
    """Create a new version in the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_post("/api/versions", {
            "key": key,
            "label": label,
            "description": description,
            "sort_order": sort_order,
            "project_id": state.CURRENT_PROJECT_ID
        })
        return f"✅ Created version: {label} in project {state.CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating version: {str(e)}"
