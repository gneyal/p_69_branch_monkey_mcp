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
def kompany_decision_list(status: str = None) -> str:
    """List all decisions for the current project.

    Args:
        status: Optional status filter (pending, approved, rejected, dismissed)

    Requires a project to be focused first using kompany_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "No project focused. Use `kompany_project_focus <project_id>` first.\n\nUse `kompany_project_list` to see available projects."

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
            if d.get("task_id"):
                output += f" | Task: `{d.get('task_id')}`"
            if d.get("machine_id"):
                output += f" | Machine: `{d.get('machine_id')}`"
            if d.get("priority", 0) > 0:
                output += f" | Priority: {d.get('priority')}"
            output += "\n"

            if d.get("description"):
                desc = d["description"][:120]
                output += f"   {desc}{'...' if len(d['description']) > 120 else ''}\n"

            if d.get("options"):
                labels = [o.get("label", str(o)) if isinstance(o, dict) else str(o) for o in d["options"]]
                output += f"   Options: {', '.join(labels)}\n"

            if d.get("resolved_option"):
                output += f"   Resolved: {d['resolved_option']} at {d.get('resolved_at', 'unknown')}\n"

            output += "\n"

        return output
    except Exception as e:
        return f"Error fetching decisions: {str(e)}"


@mcp.tool()
def kompany_decision_create(
    title: str,
    description: str = "",
    options: str = None,
    priority: int = 0,
    machine_id: str = None,
    agent_id: str = None,
    task_id: str = None,
    blocks: str = None,
    create_notification: bool = True
) -> str:
    """Create a new decision requiring human approval.

    Use this when your agent needs user input before proceeding.
    The user will see this in their notification bell and can approve/reject.

    Args:
        title: The decision question (e.g., "Deploy to production?")
        description: Context to help the user decide
        options: Comma-separated choices (e.g., "Deploy Now, Schedule Later, Skip") OR a JSON array of structured option objects:
            '[{"label":"Merge","action":"Merge PR into main","agent_id":"<uuid>"},{"label":"Reject"}]'
            - label (required): button text shown to user
            - action (optional): instruction for the executing agent
            - agent_id (optional): UUID of the agent that executes this option. Get IDs from kompany_agent_list(). Omit for passive options.
        priority: 0 = normal, 1 = high priority
        machine_id: The machine this decision relates to
        agent_id: The agent creating this decision
        task_id: The task this decision relates to
        blocks: JSON string of display blocks array. Each block has {type, data}.
            Supported types:
            - markdown: {content: "## Title\\nText..."}
            - social_post: {platform: "LinkedIn", title: "Post title", body: "Post text...", time: "ISO date"}
            - code_diff: {filename: "file.js", content: "diff content"}
            - data_table: {caption: "Title", headers: ["Col1"], rows: [["val"]]}
            - context_embed: {context_id: "uuid"}
            Example: '[{"type": "social_post", "data": {"platform": "LinkedIn", "body": "Check out..."}}]'
        create_notification: Whether to also create a notification (default: True)

    Requires a project to be focused first using kompany_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "No project focused. Use `kompany_project_focus <project_id>` first."

    try:
        import json as _json

        options_list = []
        if options:
            trimmed = options.strip()
            if trimmed.startswith("["):
                try:
                    options_list = _json.loads(trimmed)
                except _json.JSONDecodeError:
                    return "❌ Invalid options JSON. Must be a JSON array of option objects."
            else:
                options_list = [opt.strip() for opt in trimmed.split(",") if opt.strip()]

        blocks_list = None
        if blocks:
            try:
                blocks_list = _json.loads(blocks)
            except _json.JSONDecodeError:
                return "❌ Invalid blocks JSON. Must be a JSON array of {type, data} objects."

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
        if blocks_list:
            data["blocks"] = blocks_list

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

        opts_labels = [o.get("label", str(o)) if isinstance(o, dict) else str(o) for o in options_list] if options_list else []
        opts_str = f" | Options: {', '.join(opts_labels)}" if opts_labels else ""
        return f"✅ Decision created: **{title}** (ID: `{decision_id}`){opts_str}\n\nThe user will see this in their notification bell."
    except Exception as e:
        return f"Error creating decision: {str(e)}"


@mcp.tool()
def kompany_decision_update(
    decision_id: str,
    title: str = None,
    description: str = None,
    options: str = None,
    priority: int = None,
    status: str = None,
    agent_id: str = None,
    task_id: str = None,
    blocks: str = None,
) -> str:
    """Update an existing decision.

    Use this to modify a decision's content, blocks, status, or metadata.
    Only provided fields will be updated — omitted fields stay unchanged.

    Args:
        decision_id: The UUID of the decision to update
        title: New title
        description: New description
        options: New comma-separated choices OR JSON array of structured option objects (same format as kompany_decision_create)
        priority: New priority (0 = normal, 1 = high)
        status: New status (pending, approved, rejected, dismissed)
        agent_id: Agent to trigger on approval (set to "none" to clear)
        task_id: Linked task ID (set to "none" to clear)
        blocks: JSON string of display blocks array (replaces all blocks).
            Each block has {type, data}. Supported types:
            - social_post: {platform, title, body, status, ...}
            - markdown: {content}
            - code_diff: {filename, content}
            - data_table: {caption, headers, rows}
    """
    import json as _json

    try:
        data = {}
        if title is not None:
            data["title"] = title
        if description is not None:
            data["description"] = description
        if priority is not None:
            data["priority"] = priority
        if status is not None:
            data["status"] = status
        if options is not None:
            trimmed = options.strip()
            if trimmed.startswith("["):
                try:
                    data["options"] = _json.loads(trimmed)
                except _json.JSONDecodeError:
                    return "❌ Invalid options JSON. Must be a JSON array of option objects."
            else:
                data["options"] = [opt.strip() for opt in trimmed.split(",") if opt.strip()]
        if agent_id is not None:
            data["agent_id"] = None if agent_id == "none" else agent_id
        if task_id is not None:
            data["task_id"] = None if task_id == "none" else task_id
        if blocks is not None:
            try:
                data["blocks"] = _json.loads(blocks)
            except _json.JSONDecodeError:
                return "❌ Invalid blocks JSON. Must be a JSON array of {type, data} objects."

        if not data:
            return "❌ No fields to update. Provide at least one field."

        result = api_put(f"/api/decisions/{decision_id}", data)
        d = result.get("decision", result)
        return f"✅ Decision updated: **{d.get('title')}** (ID: `{decision_id}`)"
    except Exception as e:
        return f"Error updating decision: {str(e)}"


@mcp.tool()
def kompany_decision_check(decision_id: str) -> str:
    """Check the full details of a decision including blocks.

    Returns status, metadata, and block content (for reading before updating).

    Args:
        decision_id: The UUID of the decision to check
    """
    try:
        import json as _json

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
        output += f"   ID: `{d.get('id')}`"
        if d.get("agent_id"):
            output += f" | Agent: `{d.get('agent_id')}`"
        if d.get("task_id"):
            output += f" | Task: `{d.get('task_id')}`"
        if d.get("machine_id"):
            output += f" | Machine: `{d.get('machine_id')}`"
        output += "\n"

        if status == "pending":
            output += "\nThe user has not responded yet."
        elif d.get("resolved_option"):
            output += f"\nResolved with: **{d['resolved_option']}** at {d.get('resolved_at', 'unknown')}"
        else:
            output += f"\nResolved at {d.get('resolved_at', 'unknown')}"

        if d.get("description"):
            desc = d["description"][:200]
            output += f"\n\n**Description:** {desc}{'...' if len(d['description']) > 200 else ''}"

        if d.get("options"):
            labels = [o.get("label", str(o)) if isinstance(o, dict) else str(o) for o in d["options"]]
            output += f"\n**Options:** {', '.join(labels)}"

        blocks = d.get("blocks") or []
        if blocks:
            output += f"\n\n**Blocks ({len(blocks)}):**\n"
            output += f"```json\n{_json.dumps(blocks, indent=2)}\n```"

        return output
    except Exception as e:
        return f"Error checking decision: {str(e)}"
