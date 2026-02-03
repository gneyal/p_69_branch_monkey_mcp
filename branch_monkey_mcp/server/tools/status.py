"""
Status and authentication tools.
"""

from .. import state
from ..auth import get_token_path, clear_token, device_code_flow, save_token
from ..api_client import api_get, reset_session
from ..mcp_app import mcp


@mcp.tool()
def monkey_status() -> str:
    """Get the current status of Branch Monkey."""
    try:
        token_path = get_token_path()
        auth_status = "Device Token" if token_path.exists() else "API Key"

        if not state.CURRENT_PROJECT_ID:
            # No project focused - show guidance
            return f"""# Branch Monkey Status

**Connected to:** {state.API_URL}
**Auth:** {auth_status}
**Project Focus:** ⚠️ None

## Getting Started

To use Branch Monkey, you must first select a project to work on:

1. Run `monkey_project_list` to see available projects
2. Run `monkey_project_focus <project_id>` to set the active project

All tasks, machines, versions, team members, and domains are scoped to the focused project.
"""

        # Get counts filtered by project
        task_endpoint = f"/api/tasks?project_id={state.CURRENT_PROJECT_ID}"
        tasks = api_get(task_endpoint)
        task_count = len(tasks.get("tasks", []))

        version_endpoint = f"/api/versions?project_id={state.CURRENT_PROJECT_ID}"
        versions = api_get(version_endpoint)
        version_count = len(versions.get("versions", []))

        machine_endpoint = f"/api/machines?project_id={state.CURRENT_PROJECT_ID}"
        machines = api_get(machine_endpoint)
        machine_count = len(machines.get("machines", []))

        return f"""# Branch Monkey Status

**Connected to:** {state.API_URL}
**Auth:** {auth_status}
**Project Focus:** 🎯 **{state.CURRENT_PROJECT_NAME}**

## Project Stats
- **Tasks:** {task_count}
- **Machines:** {machine_count}
- **Versions:** {version_count}

## Available Commands
- `monkey_task_list` - List tasks for this project
- `monkey_task_create` - Create a new task
- `monkey_machine_list` - List machines
- `monkey_version_list` - List versions
- `monkey_team_list` - List team members
- `monkey_project_clear` - Clear project focus
"""
    except Exception as e:
        return f"Error connecting to API: {str(e)}"


@mcp.tool()
def monkey_logout() -> str:
    """Log out and clear stored authentication token."""
    try:
        clear_token()
        return """# Logged Out

Your authentication token has been cleared.
On next use, you'll be prompted to approve the device again via your browser.

To re-authenticate now, use `monkey_login`."""
    except Exception as e:
        return f"Error logging out: {str(e)}"


@mcp.tool()
def monkey_login() -> str:
    """Force re-authentication via browser approval. Use this if you're having auth issues."""
    try:
        # Clear existing token
        clear_token()

        # Reset session to clear cached auth
        reset_session()

        # Run device code flow
        auth_result = device_code_flow(state.API_URL)

        if auth_result:
            state.API_KEY = auth_result.get("access_token")
            state.ORG_ID = auth_result.get("org_id")
            save_token(state.API_KEY, state.API_URL, state.ORG_ID)
            return """# Login Successful

You are now authenticated with Branch Monkey Cloud.
Your token has been saved for future sessions.

Use `monkey_status` to verify your connection."""
        else:
            return """# Login Failed

Authentication was not completed. This could be because:
- The browser approval was denied
- The code expired (15 minute timeout)
- Network connectivity issues

Please try again with `monkey_login`."""
    except Exception as e:
        return f"""# Login Error

Failed to authenticate: {str(e)}

If this persists, check:
1. Network connectivity to {state.API_URL}
2. That you can access {state.API_URL}/approve in your browser
3. Try logging out with `monkey_logout` and restart Claude Code"""
