"""
Cron schedule management tools.
"""

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp


@mcp.tool()
def monkey_cron_list() -> str:
    """List all crons for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        result = api_get("/api/crons", params={"project_id": state.CURRENT_PROJECT_ID})
        crons = result.get("crons", [])

        if not crons:
            return f"No crons found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Crons (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for c in crons:
            enabled = "🟢" if c.get("enabled") else "⏸️"
            agent = c.get("agents") or {}
            agent_name = agent.get("name", "none")
            output += f"{enabled} **{c.get('name')}** — `{c.get('schedule')}`\n"
            output += f"   Agent: {agent_name} | Type: {c.get('cron_type', 'agent')}\n"
            if c.get("task_prompt"):
                output += f"   Prompt: {c.get('task_prompt')[:100]}...\n"
            output += f"   Last run: {c.get('last_run_at', 'never')[:19]} ({c.get('last_run_status', 'unknown')})\n"
            output += f"   ID: `{c.get('id')}`\n\n"

        return output
    except Exception as e:
        return f"Error fetching crons: {str(e)}"


@mcp.tool()
def monkey_cron_create(
    schedule: str,
    name: str = "Scheduled run",
    agent_id: str = None,
    task_prompt: str = None,
    enabled: bool = True
) -> str:
    """Create a new cron schedule in the current project.

    Args:
        schedule: Cron schedule expression, e.g. "0 6 * * *" for daily at 6am
        name: Display name for the cron (default: "Scheduled run")
        agent_id: UUID of the agent to run on this schedule (optional)
        task_prompt: Instructions the agent receives each run (optional)
        enabled: Whether the cron is active (default: True)

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        data = {
            "project_id": state.CURRENT_PROJECT_ID,
            "schedule": schedule,
            "name": name,
            "cron_type": "agent",
            "enabled": enabled,
        }
        if agent_id:
            data["agent_id"] = agent_id
        if task_prompt:
            data["task_prompt"] = task_prompt

        result = api_post("/api/crons", data)
        cron = result.get("cron", result)
        return f"✅ Created cron **{cron.get('name', name)}** — `{schedule}` (ID: `{cron.get('id')}`)"
    except Exception as e:
        return f"Error creating cron: {str(e)}"


@mcp.tool()
def monkey_cron_delete(cron_id: str) -> str:
    """Delete a cron schedule by ID.

    Args:
        cron_id: The UUID of the cron to delete
    """
    try:
        api_delete(f"/api/crons/{cron_id}")
        return f"✅ Deleted cron `{cron_id}`"
    except Exception as e:
        return f"Error deleting cron: {str(e)}"


@mcp.tool()
def monkey_cron_update(
    cron_id: str,
    name: str = None,
    schedule: str = None,
    enabled: bool = None,
    agent_id: str = None,
    task_prompt: str = None
) -> str:
    """Update an existing cron schedule.

    Args:
        cron_id: The UUID of the cron to update
        name: New display name (optional)
        schedule: New cron schedule expression, e.g. "*/5 * * * *" (optional)
        enabled: Enable or disable the cron (optional)
        agent_id: UUID of the agent to assign (optional)
        task_prompt: Instructions the agent receives each run (optional)
    """
    try:
        updates = {}
        if name is not None:
            updates["name"] = name
        if schedule is not None:
            updates["schedule"] = schedule
        if enabled is not None:
            updates["enabled"] = enabled
        if agent_id is not None:
            updates["agent_id"] = agent_id
        if task_prompt is not None:
            updates["task_prompt"] = task_prompt

        if not updates:
            return "No updates provided."

        result = api_put(f"/api/crons/{cron_id}", updates)
        cron = result.get("cron", result)
        return f"✅ Updated cron `{cron.get('name', cron_id)}`"
    except Exception as e:
        return f"Error updating cron: {str(e)}"
