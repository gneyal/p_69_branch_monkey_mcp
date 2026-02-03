"""
Company notes management tools.
"""

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp


@mcp.tool()
def monkey_note_list() -> str:
    """List all company notes for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        endpoint = f"/api/company-notes?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        notes = result.get("notes", [])

        if not notes:
            return f"No notes found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Notes (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for n in notes:
            content_preview = (n.get('content') or '')[:50]
            if len(n.get('content', '')) > 50:
                content_preview += "..."
            output += f"- **Note** (ID: `{n.get('id')}`): {content_preview}\n"

        return output
    except Exception as e:
        return f"Error fetching notes: {str(e)}"


@mcp.tool()
def monkey_note_create(
    content: str = "",
    color: str = "#fef08a",
    position_x: float = 100,
    position_y: float = 100,
    width: float = 200,
    height: float = 150
) -> str:
    """Create a new company note in the current project.

    Args:
        content: Note text content
        color: Background color (hex, default yellow)
        position_x: X position on canvas
        position_y: Y position on canvas
        width: Note width in pixels
        height: Note height in pixels

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_post("/api/company-notes", {
            "content": content,
            "color": color,
            "position_x": position_x,
            "position_y": position_y,
            "width": width,
            "height": height,
            "project_id": state.CURRENT_PROJECT_ID
        })
        note = result.get("note", result)
        return f"✅ Created note (ID: {note.get('id')}) in project {state.CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating note: {str(e)}"


@mcp.tool()
def monkey_note_get(note_id: str) -> str:
    """Get a specific note by ID.

    Args:
        note_id: The UUID of the note to retrieve
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/company-notes/{note_id}")
        note = result.get("note", result)

        output = f"# Note\n\n"
        output += f"**ID:** `{note.get('id')}`\n"
        output += f"**Color:** {note.get('color', '#fef08a')}\n"
        output += f"**Size:** {note.get('width', 200)}x{note.get('height', 150)}\n"
        output += f"**Position:** ({note.get('position_x', 0)}, {note.get('position_y', 0)})\n"
        output += f"\n**Content:**\n{note.get('content', '')}\n"

        return output
    except Exception as e:
        return f"Error fetching note: {str(e)}"


@mcp.tool()
def monkey_note_update(
    note_id: str,
    content: str = None,
    color: str = None,
    position_x: float = None,
    position_y: float = None,
    width: float = None,
    height: float = None
) -> str:
    """Update an existing note.

    Args:
        note_id: The UUID of the note to update
        content: New content (optional)
        color: New background color (optional)
        position_x: New X position (optional)
        position_y: New Y position (optional)
        width: New width (optional)
        height: New height (optional)
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        updates = {}
        if content is not None:
            updates["content"] = content
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

        api_put(f"/api/company-notes/{note_id}", updates)
        return f"✅ Updated note (ID: {note_id})"
    except Exception as e:
        return f"Error updating note: {str(e)}"


@mcp.tool()
def monkey_note_delete(note_id: str) -> str:
    """Delete a note by ID.

    Args:
        note_id: The UUID of the note to delete
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_delete(f"/api/company-notes/{note_id}")
        return f"✅ Deleted note (ID: {note_id})"
    except Exception as e:
        return f"Error deleting note: {str(e)}"
