"""
Agent definition management tools.
"""

import json

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp


@mcp.tool()
def monkey_agent_list() -> str:
    """List all agent definitions for the current project.

    Agents are custom AI personas with system prompts that can be assigned to tasks.
    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/agent-definitions?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        agents = result.get("agents", [])

        if not agents:
            return f"No agents found for project **{state.CURRENT_PROJECT_NAME}**.\n\nCreate one with `monkey_agent_create(name, system_prompt)`."

        output = f"# Agent Definitions (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for a in agents:
            is_default = " [built-in]" if a.get('is_default') else ""
            tools_info = ""
            if a.get('allowed_tools') is not None:
                tool_count = len(a.get('allowed_tools', []))
                tools_info = f" | {tool_count} tools"

            output += f"### {a.get('name')} (`{a.get('slug')}`){is_default}{tools_info}\n"
            if a.get('description'):
                output += f"{a.get('description')}\n"

            # Show system prompt preview (first 100 chars)
            prompt = a.get('system_prompt', '')
            if prompt:
                preview = prompt[:100].replace('\n', ' ')
                if len(prompt) > 100:
                    preview += "..."
                output += f"Prompt: _{preview}_\n"

            output += f"ID: `{a.get('id')}`\n\n"

        output += "---\n"
        output += "Use `monkey_apply_agent(agent_slug, instructions)` to run an agent.\n"
        output += "Use `monkey_agent_get(agent_id)` to see full system prompt.\n"

        return output
    except Exception as e:
        return f"Error fetching agents: {str(e)}"


@mcp.tool()
def monkey_agent_create(
    name: str,
    system_prompt: str,
    description: str = "",
    color: str = "#6366f1",
    icon: str = "bot",
    allowed_tools: str = None
) -> str:
    """Create a new agent definition in the current project.

    Args:
        name: Display name for the agent
        system_prompt: The system prompt that defines agent behavior
        description: Short description of what this agent does
        color: Hex color for the agent (default: #6366f1)
        icon: Icon name (default: bot)
        allowed_tools: Comma-separated list of tool keys, or None for all tools

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        payload = {
            "name": name,
            "system_prompt": system_prompt,
            "description": description,
            "color": color,
            "icon": icon,
            "project_id": state.CURRENT_PROJECT_ID
        }

        if allowed_tools is not None:
            payload["allowed_tools"] = [t.strip() for t in allowed_tools.split(",") if t.strip()]

        result = api_post("/api/agent-definitions", payload)
        agent = result.get("agent", result)
        return f"✅ Created agent: **{name}** (slug: `{agent.get('slug')}`, ID: `{agent.get('id')}`) in project {state.CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating agent: {str(e)}"


@mcp.tool()
def monkey_agent_get(agent_id: str) -> str:
    """Get a specific agent definition by ID or slug.

    Args:
        agent_id: The UUID or slug of the agent to retrieve (e.g., "planner" or "abc-def-123")
    """
    try:
        # Try by ID first
        try:
            result = api_get(f"/api/agent-definitions/{agent_id}")
            agent = result.get("agent", {})
        except Exception:
            agent = None

        # If not found by ID, try by slug
        if not agent:
            endpoint = f"/api/agent-definitions?project_id={state.CURRENT_PROJECT_ID}" if state.CURRENT_PROJECT_ID else "/api/agent-definitions"
            result = api_get(endpoint)
            agents = result.get("agents", [])
            for a in agents:
                if a.get("slug") == agent_id:
                    agent = a
                    break

        if not agent:
            return f"❌ Agent not found: `{agent_id}`"

        output = f"# Agent: {agent.get('name')}\n\n"
        output += f"**Slug:** `{agent.get('slug')}`\n"
        output += f"**ID:** `{agent.get('id')}`\n"
        output += f"**Description:** {agent.get('description', 'N/A')}\n"
        output += f"**Color:** {agent.get('color', '#6366f1')}\n"
        output += f"**Icon:** {agent.get('icon', 'bot')}\n"
        output += f"**Default:** {'Yes' if agent.get('is_default') else 'No'}\n\n"

        # Tool access info
        allowed_tools = agent.get('allowed_tools')
        if allowed_tools is None:
            output += "**Tool Access:** All tools enabled\n\n"
        elif len(allowed_tools) == 0:
            output += "**Tool Access:** No tools enabled\n\n"
        else:
            output += f"**Tool Access:** {len(allowed_tools)} tools enabled\n"
            output += f"   {', '.join(allowed_tools[:10])}"
            if len(allowed_tools) > 10:
                output += f" ... and {len(allowed_tools) - 10} more"
            output += "\n\n"

        output += f"## System Prompt\n\n```\n{agent.get('system_prompt', '')}\n```\n"

        return output
    except Exception as e:
        return f"Error fetching agent: {str(e)}"


@mcp.tool()
def monkey_agent_update(
    agent_id: str,
    name: str = None,
    description: str = None,
    system_prompt: str = None,
    color: str = None,
    icon: str = None,
    allowed_tools: str = None
) -> str:
    """Update an existing agent definition.

    Args:
        agent_id: The UUID of the agent to update
        name: New display name (optional)
        description: New description (optional)
        system_prompt: New system prompt (optional)
        color: New hex color (optional)
        icon: New icon name (optional)
        allowed_tools: Comma-separated list of tool keys, or "all" for all tools, or "none" for no tools (optional)
    """
    try:
        updates = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if system_prompt is not None:
            updates["system_prompt"] = system_prompt
        if color is not None:
            updates["color"] = color
        if icon is not None:
            updates["icon"] = icon
        if allowed_tools is not None:
            if allowed_tools.lower() == "all":
                updates["allowed_tools"] = None
            elif allowed_tools.lower() == "none":
                updates["allowed_tools"] = []
            else:
                updates["allowed_tools"] = [t.strip() for t in allowed_tools.split(",") if t.strip()]

        if not updates:
            return "No updates provided."

        api_put(f"/api/agent-definitions/{agent_id}", updates)
        return f"✅ Updated agent {agent_id}"
    except Exception as e:
        return f"Error updating agent: {str(e)}"


@mcp.tool()
def monkey_agent_delete(agent_id: str) -> str:
    """Delete an agent definition by ID.

    Args:
        agent_id: The UUID of the agent to delete

    Note: Default agents (is_default=true) cannot be deleted.
    """
    try:
        api_delete(f"/api/agent-definitions/{agent_id}")
        return f"✅ Deleted agent {agent_id}"
    except Exception as e:
        return f"Error deleting agent: {str(e)}"


@mcp.tool()
def monkey_apply_agent(
    agent_slug: str,
    instructions: str,
    context: str = None
) -> str:
    """Apply an agent to execute custom instructions.

    Fetches the agent's system prompt and runs Claude with it.
    Tries the local server first (faster), falls back to cloud relay.

    Args:
        agent_slug: The agent slug (e.g., "planner", "code", "test", "docs", "refactor")
        instructions: The user instructions/task for the agent to execute
        context: Optional JSON string with additional context (existing_tasks, project info, etc.)

    Returns:
        The agent's response/output

    Example:
        monkey_apply_agent(
            agent_slug="planner",
            instructions="Plan tasks for implementing user authentication with OAuth",
            context='{"existing_tasks": [], "available_agents": ["code", "test", "docs"]}'
        )
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        # Fetch agent by slug from database
        endpoint = f"/api/agent-definitions?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        agents = result.get("agents", [])

        # Find agent by slug
        agent = None
        for a in agents:
            if a.get("slug") == agent_slug:
                agent = a
                break

        if not agent:
            available = ', '.join(a.get('slug', '') for a in agents)
            return f"❌ Agent not found: `{agent_slug}`\n\nAvailable agents: {available}"

        # Add context to instructions if provided
        full_instructions = instructions
        if context:
            try:
                parsed_context = json.loads(context) if isinstance(context, str) else context
                full_instructions = f"{instructions}\n\nContext:\n```json\n{json.dumps(parsed_context, indent=2)}\n```"
            except json.JSONDecodeError:
                full_instructions = f"{instructions}\n\nContext: {context}"

        # Try local server first (faster, no relay round-trip)
        local_url = f"http://localhost:{state.LOCAL_SERVER_PORT}" if hasattr(state, 'LOCAL_SERVER_PORT') else "http://localhost:18081"
        try:
            import requests
            local_response = requests.post(
                f"{local_url}/api/local-claude/apply-agent",
                json={
                    "agent_slug": agent_slug,
                    "instructions": full_instructions,
                    "project_id": state.CURRENT_PROJECT_ID
                },
                timeout=130
            )
            if local_response.status_code == 200:
                local_result = local_response.json()
                output = local_result.get("output", "")
                return f"""# Agent: {agent.get('name')} (`{agent_slug}`)

## Instructions
{instructions}

## Response
{output}
"""
        except Exception:
            pass  # Local server not available, fall back to cloud relay

        # Fall back to cloud relay
        payload = {
            "agent_id": agent.get("id"),
            "agent_slug": agent_slug,
            "agent_name": agent.get("name"),
            "system_prompt": agent.get("system_prompt"),
            "instructions": full_instructions,
            "project_id": state.CURRENT_PROJECT_ID
        }

        result = api_post("/api/relay/apply-agent", payload)

        if result.get("error"):
            return f"❌ Agent execution failed: {result.get('error')}"

        output = result.get("output", result.get("result", ""))

        return f"""# Agent: {agent.get('name')} (`{agent_slug}`)

## Instructions
{instructions}

## Response
{output}
"""

    except Exception as e:
        return f"Error applying agent: {str(e)}"
