"""
Decision management tools.

Decisions are action items that require human approval/choice.
Agents create decisions when they need user input, and can check
the resolution on subsequent runs.
"""

from .. import state
from ..api_client import api_get, api_post, api_put
from ..mcp_app import mcp


@mcp.tool()
def monkey_decision_list(status: str = None) -> str:
    """List all decisions for the current project.

    Args:
        status: Optional status filter (pending, approved, rejected, dismissed)

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        params = {"project_id": state.CURRENT_PROJECT_ID}
        if status:
            params["status"] = status

        result = api_get("/api/decisions", params=params)
        decisions = result.get("decisions", [])

        if not decisions:
            status_msg = f" with status '{status}'" if status else ""
            return f"No decisions{status_msg} found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Decisions (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for d in decisions:
            icon = {
                "pending": "⏳",
                "approved": "✅",
                "rejected": "❌",
                "dismissed": "⊘"
            }.get(d.get("status"), "⬜")

            output += f"{icon} **{d.get('title')}**\n"
            output += f"   ID: `{d.get('id')}` | Status: {d.get('status')}"
            if d.get("priority", 0) > 0:
                output += f" | Priority: {d.get('priority')}"
            output += "\n"

            if d.get("description"):
                desc = d["description"][:120]
                output += f"   {desc}{'...' if len(d['description']) > 120 else ''}\n"

            if d.get("options"):
                output += f"   Options: {', '.join(d['options'])}\n"

            if d.get("resolved_option"):
                output += f"   Resolved: {d['resolved_option']} at {d.get('resolved_at', 'unknown')}\n"

            output += "\n"

        return output
    except Exception as e:
        return f"Error fetching decisions: {str(e)}"


@mcp.tool()
def monkey_decision_create(
    title: str,
    description: str = "",
    options: str = None,
    priority: int = 0,
    machine_id: str = None,
    agent_id: str = None,
    task_id: str = None,
    create_notification: bool = True
) -> str:
    """Create a new decision requiring human approval.

    Use this when your agent needs user input before proceeding.
    The user will see this in their notification bell and can approve/reject.

    Args:
        title: The decision question (e.g., "Deploy to production?")
        description: Context to help the user decide
        options: Comma-separated choices (e.g., "Deploy Now, Schedule Later, Skip")
        priority: 0 = normal, 1 = high priority
        machine_id: The machine this decision relates to
        agent_id: The agent creating this decision
        task_id: The task this decision relates to
        create_notification: Whether to also create a notification (default: True)

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        options_list = []
        if options:
            options_list = [opt.strip() for opt in options.split(",") if opt.strip()]

        data = {
            "title": title,
            "description": description,
            "priority": priority,
            "options": options_list,
            "project_id": state.CURRENT_PROJECT_ID
        }
        if machine_id:
            data["machine_id"] = machine_id
        if agent_id:
            data["agent_id"] = agent_id
        if task_id:
            data["task_id"] = task_id

        result = api_post("/api/decisions", data)
        decision = result.get("decision", result)
        decision_id = decision.get("id")

        # Auto-create a notification so the bell lights up
        if create_notification and decision_id:
            try:
                api_post("/api/notifications", {
                    "project_id": state.CURRENT_PROJECT_ID,
                    "type": "decision",
                    "title": f"Decision needed: {title}",
                    "message": description[:200] if description else "",
                    "decision_id": decision_id
                })
            except Exception:
                pass  # Non-critical

        opts_str = f" | Options: {', '.join(options_list)}" if options_list else ""
        return f"✅ Decision created: **{title}** (ID: `{decision_id}`){opts_str}\n\nThe user will see this in their notification bell."
    except Exception as e:
        return f"Error creating decision: {str(e)}"


@mcp.tool()
def monkey_decision_check(decision_id: str) -> str:
    """Check the resolution status of a decision.

    Use this to see if the user has approved/rejected a decision you created.

    Args:
        decision_id: The UUID of the decision to check
    """
    try:
        result = api_get(f"/api/decisions/{decision_id}")
        d = result.get("decision", result)

        status = d.get("status", "unknown")
        icon = {
            "pending": "⏳",
            "approved": "✅",
            "rejected": "❌",
            "dismissed": "⊘"
        }.get(status, "❓")

        output = f"{icon} **{d.get('title')}** — {status}\n"

        if status == "pending":
            output += "\nThe user has not responded yet."
        elif d.get("resolved_option"):
            output += f"\nResolved with: **{d['resolved_option']}** at {d.get('resolved_at', 'unknown')}"
        else:
            output += f"\nResolved at {d.get('resolved_at', 'unknown')}"

        return output
    except Exception as e:
        return f"Error checking decision: {str(e)}"
