"""
Business domain management tools.
"""

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
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


@mcp.tool()
def monkey_domain_create(
    name: str,
    description: str = "",
    color: str = "#6366f1",
    position_x: float = 0,
    position_y: float = 0,
    width: float = 400,
    height: float = 300
) -> str:
    """Create a new business domain on the canvas.

    Domains are visual grouping areas that contain machines.

    Args:
        name: Domain name (e.g. "Marketing", "Sales", "Operations")
        description: Short description of the domain
        color: Background color hex (default: #6366f1)
        position_x: X position on canvas
        position_y: Y position on canvas
        width: Domain width in pixels (default: 400)
        height: Domain height in pixels (default: 300)

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_post("/api/domains", {
            "name": name,
            "description": description,
            "color": color,
            "position_x": position_x,
            "position_y": position_y,
            "width": width,
            "height": height,
            "project_id": state.CURRENT_PROJECT_ID
        })
        domain = result.get("domain", result)
        return f"✅ Created domain: {name} (ID: {domain.get('id')}) in project {state.CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating domain: {str(e)}"


@mcp.tool()
def monkey_domain_update(
    domain_id: str,
    name: str = None,
    description: str = None,
    color: str = None,
    position_x: float = None,
    position_y: float = None,
    width: float = None,
    height: float = None
) -> str:
    """Update an existing business domain.

    Args:
        domain_id: The UUID of the domain to update
        name: New name (optional)
        description: New description (optional)
        color: New background color hex (optional)
        position_x: New X position (optional)
        position_y: New Y position (optional)
        width: New width (optional)
        height: New height (optional)
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        updates = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if color is not None:
            updates["color"] = color
        if position_x is not None:
            updates["position_x"] = position_x
        if position_y is not None:
            updates["position_y"] = position_y
        if width is not None:
            updates["width"] = width
        if height is not None:
            updates["height"] = height

        if not updates:
            return "⚠️ No updates provided. Specify at least one field to update."

        api_put(f"/api/domains/{domain_id}", updates)
        return f"✅ Updated domain (ID: {domain_id})"
    except Exception as e:
        return f"Error updating domain: {str(e)}"


@mcp.tool()
def monkey_domain_delete(domain_id: str) -> str:
    """Delete a business domain by ID.

    Args:
        domain_id: The UUID of the domain to delete
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_delete(f"/api/domains/{domain_id}")
        return f"✅ Deleted domain (ID: {domain_id})"
    except Exception as e:
        return f"Error deleting domain: {str(e)}"
