"""
Machine connection management tools.
"""

from .. import state
from ..api_client import api_get, api_post, api_delete
from ..mcp_app import mcp


@mcp.tool()
def monkey_connection_create(
    source_machine_id: str,
    target_machine_id: str,
    label: str = ""
) -> str:
    """Create a connection (edge) between two machines on the canvas.

    Args:
        source_machine_id: UUID of the source machine
        target_machine_id: UUID of the target machine
        label: Optional label for the connection edge

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_post("/api/machine-connections", {
            "source_machine_id": source_machine_id,
            "target_machine_id": target_machine_id,
            "label": label
        })
        connection = result.get("connection", result)
        output = f"✅ Created connection (ID: {connection.get('id')}): {source_machine_id[:8]}... → {target_machine_id[:8]}..."
        if label:
            output += f" [{label}]"
        return output
    except Exception as e:
        return f"Error creating connection: {str(e)}"


@mcp.tool()
def monkey_connection_list() -> str:
    """List all machine connections for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get("/api/machine-connections")
        connections = result.get("connections", [])

        if not connections:
            return f"No connections found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Connections (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for c in connections:
            label = f" [{c.get('label')}]" if c.get("label") else ""
            output += f"- (ID: `{c.get('id')}`) {c.get('source_machine_id')[:8]}... → {c.get('target_machine_id')[:8]}...{label}\n"

        return output
    except Exception as e:
        return f"Error fetching connections: {str(e)}"


@mcp.tool()
def monkey_connection_delete(connection_id: str) -> str:
    """Delete a machine connection by ID.

    Args:
        connection_id: The ID of the connection to delete
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_delete(f"/api/machine-connections/{connection_id}")
        return f"✅ Deleted connection (ID: {connection_id})"
    except Exception as e:
        return f"Error deleting connection: {str(e)}"
