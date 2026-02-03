"""
Project management tools.
"""

from .. import state
from ..api_client import api_get
from ..mcp_app import mcp


@mcp.tool()
def monkey_project_list() -> str:
    """List all projects available to you."""
    try:
        result = api_get("/api/projects")
        projects = result.get("projects", [])

        if not projects:
            return "No projects found."

        output = "# Projects\n\n"
        for p in projects:
            focus_marker = " 👈 **FOCUSED**" if str(p.get("id")) == str(state.CURRENT_PROJECT_ID) else ""
            output += f"- **{p.get('name')}** (ID: `{p.get('id')}`){focus_marker}\n"
            if p.get("description"):
                output += f"   {p.get('description')[:80]}\n"

        if state.CURRENT_PROJECT_ID:
            output += f"\n---\n**Current focus:** {state.CURRENT_PROJECT_NAME}\n"
        else:
            output += f"\n---\n⚠️ No project focused. Use `monkey_project_focus <id>` to set one.\n"

        return output
    except Exception as e:
        return f"Error fetching projects: {str(e)}"


@mcp.tool()
def monkey_project_focus(project_id: str) -> str:
    """Set the project in focus. All operations will be scoped to this project.

    Args:
        project_id: The UUID of the project to focus on
    """
    try:
        # Fetch the project to validate and get its name
        result = api_get(f"/api/projects/{project_id}")
        project = result.get("project", {})

        if not project:
            return f"❌ Project not found: {project_id}"

        state.CURRENT_PROJECT_ID = str(project_id)
        state.CURRENT_PROJECT_NAME = project.get("name", "Unknown")

        return f"""# 🎯 Project Focused

**Project:** {state.CURRENT_PROJECT_NAME}
**ID:** {state.CURRENT_PROJECT_ID}

All operations are now scoped to this project:
- Tasks you create will be in this project
- Task lists will show only this project's tasks
- Same for machines, versions, team members, and domains

Use `monkey_project_clear` to remove focus."""
    except Exception as e:
        return f"Error focusing project: {str(e)}"


@mcp.tool()
def monkey_project_clear() -> str:
    """Clear the current project focus."""
    state.CURRENT_PROJECT_ID = None
    state.CURRENT_PROJECT_NAME = None
    return "✅ Project focus cleared. Use `monkey_project_focus <id>` to set a new project."


@mcp.tool()
def monkey_org_list() -> str:
    """List all organizations."""
    try:
        result = api_get("/api/organizations")
        orgs = result.get("organizations", [])

        if not orgs:
            return "No organizations found."

        output = "# Organizations\n\n"
        for o in orgs:
            output += f"- **{o.get('name')}** (ID: `{o.get('id')}`)\n"
            if o.get("description"):
                output += f"   {o.get('description')[:80]}\n"

        return output
    except Exception as e:
        return f"Error fetching organizations: {str(e)}"
