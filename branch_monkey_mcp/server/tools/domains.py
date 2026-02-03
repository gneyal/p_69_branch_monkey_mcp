"""
Business domain management tools.
"""

from .. import state
from ..api_client import api_get
from ..mcp_app import mcp


@mcp.tool()
def monkey_domain_list() -> str:
    """List all business domains for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/domains?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        domains = result.get("domains", [])

        if not domains:
            return f"No business domains found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Business Domains (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for d in domains:
            output += f"- **{d.get('name')}** (ID: `{d.get('id')}`)\n"
            if d.get("description"):
                output += f"   {d.get('description')[:80]}...\n"

        return output
    except Exception as e:
        return f"Error fetching domains: {str(e)}"
