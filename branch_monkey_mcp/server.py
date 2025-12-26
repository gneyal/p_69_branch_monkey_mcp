"""
Branch Monkey MCP Server

MCP server for Claude Code that connects to Branch Monkey Cloud.
No API key needed - authenticates via browser approval.

Usage:
    Add to .mcp.json in your project:
        {
            "mcpServers": {
                "branch-monkey-cloud": {
                    "command": "uvx",
                    "args": ["--from", "git+https://github.com/gneyal/p_69_branch_monkey_mcp.git", "branch-monkey-mcp"],
                    "env": {
                        "BRANCH_MONKEY_API_URL": "https://p-63-branch-monkey.pages.dev"
                    }
                }
            }
        }

On first run, a browser opens for you to log in and approve. Token is saved for future use.
"""

import os
import sys
import subprocess
import json
import time
import webbrowser
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Any, Optional
from pathlib import Path
import uuid


# ============================================================
# TOKEN STORAGE
# ============================================================

def get_token_path() -> Path:
    """Get the path to the stored token file."""
    config_dir = Path.home() / ".branch-monkey"
    config_dir.mkdir(exist_ok=True)
    return config_dir / "token.json"


def load_stored_token(api_url: str) -> Optional[dict]:
    """Load stored token from disk."""
    token_path = get_token_path()
    if token_path.exists():
        try:
            with open(token_path) as f:
                data = json.load(f)
                if data.get("api_url") == api_url:
                    return data
        except Exception:
            pass
    return None


def save_token(token: str, api_url: str):
    """Save token to disk."""
    token_path = get_token_path()
    with open(token_path, "w") as f:
        json.dump({
            "access_token": token,
            "api_url": api_url,
            "saved_at": time.time()
        }, f)
    os.chmod(token_path, 0o600)


def clear_token():
    """Remove stored token."""
    token_path = get_token_path()
    if token_path.exists():
        token_path.unlink()


# ============================================================
# DEVICE CODE FLOW
# ============================================================

def get_machine_name() -> str:
    """Get a name for this machine."""
    try:
        cwd = os.getcwd()
        project_name = os.path.basename(cwd)
        import socket
        hostname = socket.gethostname()
        return f"{project_name} on {hostname}"
    except Exception:
        return "Claude Code MCP"


def device_code_flow(api_url: str) -> Optional[str]:
    """Run the device code flow to authenticate."""
    print("\n" + "=" * 60, file=sys.stderr)
    print("  Branch Monkey - Authentication Required", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    machine_name = get_machine_name()

    try:
        response = requests.post(
            f"{api_url}/api/auth/device",
            json={"machine_name": machine_name},
            timeout=30
        )

        if not response.ok:
            print(f"Error: Failed to start authentication: {response.text}", file=sys.stderr)
            return None

        data = response.json()
        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_uri = data["verification_uri"]
        expires_in = data.get("expires_in", 900)
        interval = data.get("interval", 5)

        print(f"\n  To authorize this device, visit:\n", file=sys.stderr)
        print(f"    {verification_uri}", file=sys.stderr)
        print(f"\n  Or enter this code at {api_url}/approve:\n", file=sys.stderr)
        print(f"    {user_code}", file=sys.stderr)
        print(f"\n  Waiting for approval...", file=sys.stderr)
        print("=" * 60 + "\n", file=sys.stderr)

        try:
            webbrowser.open(verification_uri)
        except Exception:
            pass

        start_time = time.time()
        while time.time() - start_time < expires_in:
            time.sleep(interval)

            poll_response = requests.get(
                f"{api_url}/api/auth/device",
                params={"device_code": device_code},
                timeout=30
            )

            if not poll_response.ok:
                error = poll_response.json().get("error", "unknown")
                if error == "expired_token":
                    print("  Code expired. Please try again.", file=sys.stderr)
                    return None
                elif error == "access_denied":
                    print("  Access denied.", file=sys.stderr)
                    return None
                continue

            poll_data = poll_response.json()

            if poll_data.get("status") == "approved":
                token = poll_data.get("access_token")
                print("  Approved! You can now use Branch Monkey.", file=sys.stderr)
                return token

        print("  Timeout waiting for approval.", file=sys.stderr)
        return None

    except Exception as e:
        print(f"Error during authentication: {e}", file=sys.stderr)
        return None


# ============================================================
# INITIALIZATION
# ============================================================

def get_git_user_email() -> Optional[str]:
    """Get the git user email from the current repository or global config."""
    try:
        result = subprocess.run(
            ['git', 'config', 'user.email'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


GIT_USER_EMAIL = get_git_user_email()

CURRENT_TASK_ID: Optional[int] = None
CURRENT_TASK_TITLE: Optional[str] = None
CURRENT_SESSION_ID: Optional[str] = None

# Track current project focus - all operations will be scoped to this project
CURRENT_PROJECT_ID: Optional[str] = None
CURRENT_PROJECT_NAME: Optional[str] = None

CURRENT_SESSION_ID = str(uuid.uuid4())[:8]


def auto_log_activity(tool_name: str, duration: float = 0):
    """Automatically log tool activity when a task is active."""
    if CURRENT_TASK_ID is None:
        return

    try:
        from datetime import datetime

        data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "provider": "mcp",
            "model": "claude-tool-call",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cost": 0,
            "duration": duration,
            "prompt_preview": f"Tool: {tool_name}",
            "response_preview": "",
            "status": "success",
            "session_id": CURRENT_SESSION_ID,
            "tool_name": tool_name,
            "git_email": GIT_USER_EMAIL,
            "task_id": CURRENT_TASK_ID,
            "task_title": CURRENT_TASK_TITLE
        }

        api_post("/api/prompt-logs", data)
    except Exception:
        pass


try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    print("Error: mcp package not installed.", file=sys.stderr)
    sys.exit(1)

API_URL = os.environ.get("BRANCH_MONKEY_API_URL", "https://p-63-branch-monkey.pages.dev")
API_KEY = os.environ.get("BRANCH_MONKEY_API_KEY")
REQUEST_TIMEOUT = 30

if not API_KEY:
    stored = load_stored_token(API_URL)
    if stored:
        API_KEY = stored.get("access_token")
    else:
        API_KEY = device_code_flow(API_URL)
        if API_KEY:
            save_token(API_KEY, API_URL)
        else:
            print("\n" + "=" * 60, file=sys.stderr)
            print("  AUTHENTICATION FAILED", file=sys.stderr)
            print("=" * 60, file=sys.stderr)
            print("\nCould not authenticate with Branch Monkey Cloud.", file=sys.stderr)
            print("\nPossible reasons:", file=sys.stderr)
            print("  - Browser approval was denied or timed out", file=sys.stderr)
            print("  - Network connectivity issues", file=sys.stderr)
            print(f"  - Unable to reach {API_URL}", file=sys.stderr)
            print("\nTo try again:", file=sys.stderr)
            print("  1. Restart Claude Code", file=sys.stderr)
            print("  2. Or use the `monkey_login` tool after startup", file=sys.stderr)
            print("=" * 60 + "\n", file=sys.stderr)
            sys.exit(1)

mcp = FastMCP("Branch Monkey")


# ============================================================
# HTTP CLIENT
# ============================================================

def create_session():
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

_session = None

def get_session():
    global _session
    if _session is None:
        _session = create_session()
    return _session


def api_request(method: str, endpoint: str, **kwargs) -> dict:
    global API_KEY, _session
    url = f"{API_URL.rstrip('/')}/{endpoint.lstrip('/')}"

    headers = kwargs.pop("headers", {})
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    headers["Content-Type"] = "application/json"

    kwargs["headers"] = headers
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    session = get_session()
    response = session.request(method, url, **kwargs)

    # Auto re-authenticate on 401
    if response.status_code == 401:
        print("\n[Branch Monkey] Token expired, re-authenticating...", file=sys.stderr)
        clear_token()
        _session = None

        new_token = device_code_flow(API_URL)
        if new_token:
            save_token(new_token, API_URL)
            API_KEY = new_token

            # Retry the request with new token
            headers["Authorization"] = f"Bearer {API_KEY}"
            kwargs["headers"] = headers
            session = get_session()
            response = session.request(method, url, **kwargs)
        else:
            response.raise_for_status()

    response.raise_for_status()

    return response.json() if response.content else {}


def api_get(endpoint: str, **kwargs) -> dict:
    return api_request("GET", endpoint, **kwargs)


def api_post(endpoint: str, data: dict = None, **kwargs) -> dict:
    return api_request("POST", endpoint, json=data, **kwargs)


def api_put(endpoint: str, data: dict = None, **kwargs) -> dict:
    return api_request("PUT", endpoint, json=data, **kwargs)


def api_delete(endpoint: str, **kwargs) -> dict:
    return api_request("DELETE", endpoint, **kwargs)


# ============================================================
# STATUS & AUTH
# ============================================================

@mcp.tool()
def monkey_status() -> str:
    """Get the current status of Branch Monkey."""
    try:
        token_path = get_token_path()
        auth_status = "Device Token" if token_path.exists() else "API Key"

        if not CURRENT_PROJECT_ID:
            # No project focused - show guidance
            return f"""# Branch Monkey Status

**Connected to:** {API_URL}
**Auth:** {auth_status}
**Project Focus:** ⚠️ None

## Getting Started

To use Branch Monkey, you must first select a project to work on:

1. Run `monkey_project_list` to see available projects
2. Run `monkey_project_focus <project_id>` to set the active project

All tasks, machines, versions, team members, and domains are scoped to the focused project.
"""

        # Get counts filtered by project
        task_endpoint = f"/api/tasks?project_id={CURRENT_PROJECT_ID}"
        tasks = api_get(task_endpoint)
        task_count = len(tasks.get("tasks", []))

        version_endpoint = f"/api/versions?project_id={CURRENT_PROJECT_ID}"
        versions = api_get(version_endpoint)
        version_count = len(versions.get("versions", []))

        machine_endpoint = f"/api/machines?project_id={CURRENT_PROJECT_ID}"
        machines = api_get(machine_endpoint)
        machine_count = len(machines.get("machines", []))

        return f"""# Branch Monkey Status

**Connected to:** {API_URL}
**Auth:** {auth_status}
**Project Focus:** 🎯 **{CURRENT_PROJECT_NAME}**

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
    global API_KEY, _session
    try:
        # Clear existing token
        clear_token()

        # Reset session to clear cached auth
        _session = None

        # Run device code flow
        new_token = device_code_flow(API_URL)

        if new_token:
            save_token(new_token, API_URL)
            API_KEY = new_token
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
1. Network connectivity to {API_URL}
2. That you can access {API_URL}/approve in your browser
3. Try logging out with `monkey_logout` and restart Claude Code"""


# ============================================================
# PROJECTS
# ============================================================

@mcp.tool()
def monkey_project_list() -> str:
    """List all projects available to you."""
    try:
        result = api_get("/api/projects")
        projects = result.get("projects", [])

        if not projects:
            return "No projects found."

        output = "# Projects\n\n"
        for p in projects:
            focus_marker = " 👈 **FOCUSED**" if str(p.get("id")) == str(CURRENT_PROJECT_ID) else ""
            output += f"- **{p.get('name')}** (ID: `{p.get('id')}`){focus_marker}\n"
            if p.get("description"):
                output += f"   {p.get('description')[:80]}\n"

        if CURRENT_PROJECT_ID:
            output += f"\n---\n**Current focus:** {CURRENT_PROJECT_NAME}\n"
        else:
            output += f"\n---\n⚠️ No project focused. Use `monkey_project_focus <id>` to set one.\n"

        return output
    except Exception as e:
        return f"Error fetching projects: {str(e)}"


@mcp.tool()
def monkey_project_focus(project_id: str) -> str:
    """Set the project in focus. All operations will be scoped to this project.

    Args:
        project_id: The UUID of the project to focus on
    """
    global CURRENT_PROJECT_ID, CURRENT_PROJECT_NAME
    try:
        # Fetch the project to validate and get its name
        result = api_get(f"/api/projects/{project_id}")
        project = result.get("project", {})

        if not project:
            return f"❌ Project not found: {project_id}"

        CURRENT_PROJECT_ID = str(project_id)
        CURRENT_PROJECT_NAME = project.get("name", "Unknown")

        return f"""# 🎯 Project Focused

**Project:** {CURRENT_PROJECT_NAME}
**ID:** {CURRENT_PROJECT_ID}

All operations are now scoped to this project:
- Tasks you create will be in this project
- Task lists will show only this project's tasks
- Same for machines, versions, team members, and domains

Use `monkey_project_clear` to remove focus."""
    except Exception as e:
        return f"Error focusing project: {str(e)}"


@mcp.tool()
def monkey_project_clear() -> str:
    """Clear the current project focus."""
    global CURRENT_PROJECT_ID, CURRENT_PROJECT_NAME
    CURRENT_PROJECT_ID = None
    CURRENT_PROJECT_NAME = None
    return "✅ Project focus cleared. Use `monkey_project_focus <id>` to set a new project."


@mcp.tool()
def monkey_org_list() -> str:
    """List all organizations."""
    try:
        result = api_get("/api/organizations")
        orgs = result.get("organizations", [])

        if not orgs:
            return "No organizations found."

        output = "# Organizations\n\n"
        for o in orgs:
            output += f"- **{o.get('name')}** (ID: `{o.get('id')}`)\n"
            if o.get("description"):
                output += f"   {o.get('description')[:80]}\n"

        return output
    except Exception as e:
        return f"Error fetching organizations: {str(e)}"


# ============================================================
# TASKS
# ============================================================

@mcp.tool()
def monkey_task_list() -> str:
    """List all tasks for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/tasks?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        tasks = result.get("tasks", [])

        if not tasks:
            return f"No tasks found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Tasks (Project: {CURRENT_PROJECT_NAME})\n\n"
        for task in tasks:
            status_icon = {"todo": "⬜", "in_progress": "🔄", "done": "✅"}.get(task.get("status"), "⬜")
            task_num = task.get('task_number', 'N/A')
            output += f"{status_icon} **#{task_num}**: {task.get('title')}\n"
            if task.get("description"):
                output += f"   {task.get('description')[:100]}...\n"

        return output
    except Exception as e:
        return f"Error fetching tasks: {str(e)}"


@mcp.tool()
def monkey_task_create(
    title: str,
    description: str = "",
    status: str = "todo",
    priority: int = 0,
    version: str = None,
    machine_id: int = None
) -> str:
    """Create a new task in the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        data = {
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "version": version or "backlog",
            "project_id": CURRENT_PROJECT_ID
        }
        if machine_id:
            data["machine_id"] = machine_id

        result = api_post("/api/tasks", data)
        task = result.get("task", result)

        return f"✅ Created task #{task.get('task_number', task.get('id'))}: {title} (Project: {CURRENT_PROJECT_NAME})"
    except Exception as e:
        return f"Error creating task: {str(e)}"


@mcp.tool()
def monkey_task_update(
    task_id: int,
    title: str = None,
    description: str = None,
    status: str = None,
    priority: int = None,
    version: str = None,
    machine_id: int = None
) -> str:
    """Update an existing task."""
    try:
        updates = {}
        if title is not None:
            updates["title"] = title
        if description is not None:
            updates["description"] = description
        if status is not None:
            updates["status"] = status
        if priority is not None:
            updates["priority"] = priority
        if version is not None:
            updates["version"] = version
        if machine_id is not None:
            updates["machine_id"] = machine_id if machine_id != 0 else None

        api_put(f"/api/tasks/{task_id}", updates)
        return f"✅ Updated task #{task_id}"
    except Exception as e:
        return f"Error updating task: {str(e)}"


@mcp.tool()
def monkey_task_delete(task_id: str) -> str:
    """Delete a task by UUID."""
    try:
        api_delete(f"/api/tasks/{task_id}")
        return f"✅ Deleted task {task_id}"
    except Exception as e:
        return f"Error deleting task: {str(e)}"


@mcp.tool()
def monkey_task_work(task_id: int) -> str:
    """Start working on a task. Sets status to in_progress and logs start."""
    global CURRENT_TASK_ID, CURRENT_TASK_TITLE
    try:
        result = api_post(f"/api/tasks/{task_id}/work")
        task = result.get("task", {})

        CURRENT_TASK_ID = task_id
        CURRENT_TASK_TITLE = task.get('title', 'Unknown')

        auto_log_activity("task_work_start", duration=1)

        return f"""# 🔧 Working on Task {task_id}: {task.get('title', 'Unknown')}

**Status:** in_progress
**Description:** {task.get('description') or '(none)'}
**Version:** {task.get('version') or 'backlog'}

Use `monkey_task_log` to record progress, `monkey_task_complete` when done."""
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
def monkey_task_log(task_id: int, content: str, update_type: str = "progress") -> str:
    """Log LLM work on a task."""
    try:
        api_post(f"/api/tasks/{task_id}/log", {
            "content": content,
            "update_type": update_type
        })
        auto_log_activity("task_log")
        return f"✓ Logged update to task #{task_id}"
    except Exception as e:
        return f"Error logging: {str(e)}"


@mcp.tool()
def monkey_task_complete(task_id: int, summary: str) -> str:
    """Mark a task as complete."""
    global CURRENT_TASK_ID, CURRENT_TASK_TITLE
    try:
        result = api_post(f"/api/tasks/{task_id}/complete", {"summary": summary})
        task = result.get("task", {})

        auto_log_activity("task_complete", duration=1)

        CURRENT_TASK_ID = None
        CURRENT_TASK_TITLE = None

        return f"✅ Task {task_id} completed: {task.get('title', 'Unknown')}\n\nSummary: {summary}"
    except Exception as e:
        return f"Error completing task: {str(e)}"


@mcp.tool()
def monkey_task_search(query: str, status: str = None, version: str = None) -> str:
    """Search tasks by title or description."""
    try:
        params = {"query": query}
        if status:
            params["status"] = status
        if version:
            params["version"] = version

        result = api_get("/api/tasks/search", params=params)
        tasks = result.get("tasks", [])

        if not tasks:
            return f"No tasks matching '{query}'"

        output = f"# Tasks matching '{query}'\n\n"
        for task in tasks:
            status_icon = {"todo": "⬜", "in_progress": "🔄", "done": "✅"}.get(task.get("status"), "⬜")
            output += f"{status_icon} **{task.get('task_number')}**: {task.get('title')}\n"

        return output
    except Exception as e:
        return f"Error searching: {str(e)}"


# ============================================================
# VERSIONS
# ============================================================

@mcp.tool()
def monkey_version_list() -> str:
    """List all versions for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/versions?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        versions = result.get("versions", [])

        if not versions:
            return f"No versions found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Versions (Project: {CURRENT_PROJECT_NAME})\n\n"
        for v in versions:
            locked = " 🔒" if v.get("locked") else ""
            output += f"- **{v.get('key')}**: {v.get('label')}{locked}\n"

        return output
    except Exception as e:
        return f"Error fetching versions: {str(e)}"


@mcp.tool()
def monkey_version_create(key: str, label: str, description: str = "", sort_order: int = 0) -> str:
    """Create a new version in the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_post("/api/versions", {
            "key": key,
            "label": label,
            "description": description,
            "sort_order": sort_order,
            "project_id": CURRENT_PROJECT_ID
        })
        return f"✅ Created version: {label} in project {CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating version: {str(e)}"


# ============================================================
# TEAM MEMBERS
# ============================================================

@mcp.tool()
def monkey_team_list() -> str:
    """List all team members for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/team-members?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        members = result.get("team_members", [])

        if not members:
            return f"No team members found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Team Members (Project: {CURRENT_PROJECT_NAME})\n\n"
        for m in members:
            output += f"- **{m.get('name')}** ({m.get('role') or 'member'})\n"

        return output
    except Exception as e:
        return f"Error fetching team: {str(e)}"


@mcp.tool()
def monkey_team_add(name: str, email: str = "", role: str = "", color: str = "#6366f1") -> str:
    """Add a new team member to the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_post("/api/team-members", {
            "name": name,
            "email": email,
            "role": role,
            "color": color,
            "project_id": CURRENT_PROJECT_ID
        })
        return f"✅ Added team member: {name} to project {CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error adding team member: {str(e)}"


# ============================================================
# MACHINES
# ============================================================

@mcp.tool()
def monkey_machine_list() -> str:
    """List all machines for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/machines?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        machines = result.get("machines", [])

        if not machines:
            return f"No machines found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Machines (Project: {CURRENT_PROJECT_NAME})\n\n"
        for m in machines:
            status_icon = {"active": "🟢", "paused": "⏸️", "draft": "📝"}.get(m.get("status"), "⚪")
            output += f"{status_icon} **{m.get('name')}** (ID: `{m.get('id')}`)\n"
            if m.get("description"):
                output += f"   {m.get('description')[:80]}...\n"

        return output
    except Exception as e:
        return f"Error fetching machines: {str(e)}"


@mcp.tool()
def monkey_machine_create(
    name: str,
    description: str = "",
    goal: str = "",
    status: str = "active"
) -> str:
    """Create a new machine (automated business process) in the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_post("/api/machines", {
            "name": name,
            "description": description,
            "goal": goal,
            "status": status,
            "project_id": CURRENT_PROJECT_ID
        })
        machine = result.get("machine", result)
        return f"✅ Created machine: {name} (ID: {machine.get('id')}) in project {CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating machine: {str(e)}"


# ============================================================
# DOMAINS
# ============================================================

@mcp.tool()
def monkey_domain_list() -> str:
    """List all business domains for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/domains?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        domains = result.get("domains", [])

        if not domains:
            return f"No business domains found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Business Domains (Project: {CURRENT_PROJECT_NAME})\n\n"
        for d in domains:
            output += f"- **{d.get('name')}** (ID: `{d.get('id')}`)\n"
            if d.get("description"):
                output += f"   {d.get('description')[:80]}...\n"

        return output
    except Exception as e:
        return f"Error fetching domains: {str(e)}"


# ============================================================
# MAIN
# ============================================================

def main():
    """Run the MCP server."""
    print(f"Branch Monkey MCP starting...", file=sys.stderr)
    print(f"Connecting to: {API_URL}", file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
