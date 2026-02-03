"""
Context management and task-context linking tools.
"""

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp


@mcp.tool()
def monkey_context_list() -> str:
    """List all contexts for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/contexts?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        contexts = result.get("contexts", [])

        if not contexts:
            return f"No contexts found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Contexts (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for c in contexts:
            ctx_type = c.get('context_type', 'general')
            content_preview = (c.get('content', '')[:100] + '...') if len(c.get('content', '')) > 100 else c.get('content', '')
            output += f"- **{c.get('name')}** [{ctx_type}] (ID: `{c.get('id')}`)\n"
            output += f"   {content_preview}\n\n"

        return output
    except Exception as e:
        return f"Error fetching contexts: {str(e)}"


@mcp.tool()
def monkey_context_create(
    name: str,
    content: str,
    context_type: str = "general"
) -> str:
    """Create a new reusable context in the current project.

    Contexts are reusable snippets of information that can be attached to tasks.
    Use context_type to categorize: general, code, docs, spec, requirement, etc.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_post("/api/contexts", {
            "name": name,
            "content": content,
            "context_type": context_type,
            "project_id": state.CURRENT_PROJECT_ID
        })
        context = result.get("context", result)
        return f"✅ Created context: {name} (ID: `{context.get('id')}`) in project {state.CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating context: {str(e)}"


@mcp.tool()
def monkey_context_get(context_id: str) -> str:
    """Get a specific context by ID."""
    try:
        result = api_get(f"/api/contexts/{context_id}")
        context = result.get("context", {})

        if not context:
            return f"❌ Context not found: {context_id}"

        output = f"# Context: {context.get('name')}\n\n"
        output += f"**ID:** `{context.get('id')}`\n"
        output += f"**Type:** {context.get('context_type', 'general')}\n"
        output += f"**Created:** {context.get('created_at', '')[:19]}\n"
        output += f"**Updated:** {context.get('updated_at', '')[:19]}\n\n"
        output += f"## Content\n\n{context.get('content', '')}\n"

        return output
    except Exception as e:
        return f"Error fetching context: {str(e)}"


@mcp.tool()
def monkey_context_update(
    context_id: str,
    name: str = None,
    content: str = None,
    context_type: str = None
) -> str:
    """Update an existing context."""
    try:
        updates = {}
        if name is not None:
            updates["name"] = name
        if content is not None:
            updates["content"] = content
        if context_type is not None:
            updates["context_type"] = context_type

        if not updates:
            return "No updates provided."

        api_put(f"/api/contexts/{context_id}", updates)
        return f"✅ Updated context {context_id}"
    except Exception as e:
        return f"Error updating context: {str(e)}"


@mcp.tool()
def monkey_context_delete(context_id: str) -> str:
    """Delete a context by ID."""
    try:
        api_delete(f"/api/contexts/{context_id}")
        return f"✅ Deleted context {context_id}"
    except Exception as e:
        return f"Error deleting context: {str(e)}"


@mcp.tool()
def monkey_context_search(query: str) -> str:
    """Search contexts by name or content."""
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/contexts/search/{query}?project_id={state.CURRENT_PROJECT_ID}")
        contexts = result.get("contexts", [])

        if not contexts:
            return f"No contexts matching '{query}'"

        output = f"# Contexts matching '{query}'\n\n"
        for c in contexts:
            ctx_type = c.get('context_type', 'general')
            output += f"- **{c.get('name')}** [{ctx_type}] (ID: `{c.get('id')}`)\n"

        return output
    except Exception as e:
        return f"Error searching contexts: {str(e)}"


@mcp.tool()
def monkey_context_recent(limit: int = 10) -> str:
    """Get recently used contexts for the current project."""
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/contexts/recent?limit={limit}&project_id={state.CURRENT_PROJECT_ID}")
        contexts = result.get("contexts", [])

        if not contexts:
            return f"No recent contexts found for project **{state.CURRENT_PROJECT_NAME}**."

        output = f"# Recent Contexts (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for c in contexts:
            ctx_type = c.get('context_type', 'general')
            last_used = c.get('last_used', '')[:19] if c.get('last_used') else 'Never'
            output += f"- **{c.get('name')}** [{ctx_type}] - Last used: {last_used}\n"
            output += f"   ID: `{c.get('id')}`\n"

        return output
    except Exception as e:
        return f"Error fetching recent contexts: {str(e)}"


# Task-Context linking tools

@mcp.tool()
def monkey_task_contexts(task_id: str) -> str:
    """Get all contexts linked to a specific task."""
    try:
        result = api_get(f"/api/contexts/task/{task_id}")
        contexts = result.get("contexts", [])

        if not contexts:
            return f"No contexts linked to task {task_id}"

        output = f"# Contexts for Task {task_id}\n\n"
        for c in contexts:
            ctx_type = c.get('context_type', 'general')
            added = c.get('added_at', '')[:19] if c.get('added_at') else ''
            output += f"- **{c.get('name')}** [{ctx_type}]\n"
            output += f"   ID: `{c.get('id')}` | Added: {added}\n"

        return output
    except Exception as e:
        return f"Error fetching task contexts: {str(e)}"


@mcp.tool()
def monkey_task_link_context(task_id: str, context_id: str) -> str:
    """Link an existing context to a task."""
    try:
        api_post(f"/api/contexts/task/{task_id}", {"context_id": context_id})
        return f"✅ Linked context {context_id} to task {task_id}"
    except Exception as e:
        return f"Error linking context: {str(e)}"


@mcp.tool()
def monkey_task_unlink_context(task_id: str, context_id: str) -> str:
    """Unlink a context from a task."""
    try:
        api_delete(f"/api/contexts/task/{task_id}/{context_id}")
        return f"✅ Unlinked context {context_id} from task {task_id}"
    except Exception as e:
        return f"Error unlinking context: {str(e)}"


@mcp.tool()
def monkey_task_similar_contexts(task_id: str) -> str:
    """Find contexts from similar tasks that might be relevant.

    Use this when starting a new task to find reusable contexts from related work.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/contexts/similar/{task_id}?project_id={state.CURRENT_PROJECT_ID}")
        contexts = result.get("contexts", [])

        if not contexts:
            return f"No similar contexts found for task {task_id}"

        output = f"# Similar Contexts for Task {task_id}\n\n"
        output += "These contexts were used in tasks with similar titles/descriptions:\n\n"

        for c in contexts:
            ctx_type = c.get('context_type', 'general')
            from_task = c.get('from_task_title', 'Unknown task')
            output += f"- **{c.get('name')}** [{ctx_type}]\n"
            output += f"   From: {from_task}\n"
            output += f"   ID: `{c.get('id')}`\n\n"

        output += "\nUse `monkey_task_link_context(task_id, context_id)` to reuse any of these."

        return output
    except Exception as e:
        return f"Error finding similar contexts: {str(e)}"
