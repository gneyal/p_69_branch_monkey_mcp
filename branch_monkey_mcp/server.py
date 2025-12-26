"""
Branch Monkey MCP Server

MCP server for Claude Code that connects to Branch Monkey Cloud.
No API key needed - authenticates via browser approval.

Usage:
    Add to .mcp.json in your project:
        {
            "mcpServers": {
                "branch-monkey": {
                    "command": "uvx",
                    "args": ["--from", "git+https://github.com/gneyal/branch-monkey-mcp.git", "branch-monkey-mcp"],
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
            print("Authentication failed. Please try again.", file=sys.stderr)
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
    url = f"{API_URL.rstrip('/')}/{endpoint.lstrip('/')}"

    headers = kwargs.pop("headers", {})
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    headers["Content-Type"] = "application/json"

    kwargs["headers"] = headers
    kwargs.setdefault("timeout", REQUEST_TIMEOUT)

    session = get_session()
    response = session.request(method, url, **kwargs)
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
    """Get the current status of the repository (cloud mode)."""
    try:
        tasks = api_get("/api/tasks")
        task_count = len(tasks.get("tasks", []))

        versions = api_get("/api/versions")
        version_count = len(versions.get("versions", []))

        token_path = get_token_path()
        auth_status = "Device Token" if token_path.exists() else "API Key"

        return f"""# Branch Monkey Status

**Connected to:** {API_URL}
**Auth:** {auth_status}
**Tasks:** {task_count}
**Versions:** {version_count}
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

To re-authenticate now, restart Claude Code."""
    except Exception as e:
        return f"Error logging out: {str(e)}"


# ============================================================
# TASKS
# ============================================================

@mcp.tool()
def monkey_task_list() -> str:
    """List all tasks for this project."""
    try:
        result = api_get("/api/tasks")
        tasks = result.get("tasks", [])

        if not tasks:
            return "No tasks found."

        output = "# Tasks\n\n"
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
    """Create a new task."""
    try:
        data = {
            "title": title,
            "description": description,
            "status": status,
            "priority": priority,
            "version": version or "backlog"
        }
        if machine_id:
            data["machine_id"] = machine_id

        result = api_post("/api/tasks", data)
        task = result.get("task", result)

        return f"Created task #{task.get('task_number', task.get('id'))}: {title}"
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
        return f"Updated task #{task_id}"
    except Exception as e:
        return f"Error updating task: {str(e)}"


@mcp.tool()
def monkey_task_delete(task_id: str) -> str:
    """Delete a task by UUID."""
    try:
        api_delete(f"/api/tasks/{task_id}")
        return f"Deleted task {task_id}"
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

        return f"""# Working on Task {task_id}: {task.get('title', 'Unknown')}

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
        return f"Logged update to task #{task_id}"
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

        return f"Task {task_id} completed: {task.get('title', 'Unknown')}\n\nSummary: {summary}"
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


@mcp.tool()
def monkey_get_recent_tasks(hours: int = 24, limit: int = 10) -> str:
    """Get tasks worked on recently (in_progress or updated within time window)."""
    try:
        result = api_get(f"/api/tasks/recent?hours={hours}&limit={limit}")
        tasks = result.get("tasks", [])

        if not tasks:
            return "No recent tasks found."

        output = "# Recent Tasks\n\n"
        for task in tasks:
            status_icon = {"todo": "⬜", "in_progress": "🔄", "done": "✅"}.get(task.get("status"), "⬜")
            task_num = task.get('task_number', 'N/A')
            updated = task.get('updated_at', '')[:16].replace('T', ' ')
            output += f"{status_icon} **#{task_num}**: {task.get('title')}\n"
            output += f"   Last updated: {updated}\n\n"

        return output
    except Exception as e:
        return f"Error fetching recent tasks: {str(e)}"


@mcp.tool()
def monkey_auto_resume(user_prompt: str) -> str:
    """Check if the user's prompt relates to a recent task and auto-resume if matched."""
    global CURRENT_TASK_ID, CURRENT_TASK_TITLE
    try:
        result = api_get("/api/tasks/recent?hours=24&limit=10")
        tasks = result.get("tasks", [])

        if not tasks:
            return "No recent tasks found. Consider creating a new task."

        in_progress = [t for t in tasks if t.get("status") == "in_progress"]
        prompt_lower = user_prompt.lower()
        prompt_words = set(prompt_lower.split())

        def score_task(task):
            score = 0
            title = (task.get('title') or '').lower()
            desc = (task.get('description') or '').lower()
            title_words = set(title.split())
            desc_words = set(desc.split())
            score += len(prompt_words & title_words) * 3
            score += len(prompt_words & desc_words) * 1
            if task.get("status") == "in_progress":
                score += 5
            return score

        scored_tasks = [(score_task(t), t) for t in tasks]
        scored_tasks.sort(key=lambda x: x[0], reverse=True)

        best_score, best_task = scored_tasks[0]

        output = "# Task Context Check\n\n"

        if best_score >= 3:
            task_id = best_task.get('task_number')
            task_title = best_task.get('title')

            CURRENT_TASK_ID = task_id
            CURRENT_TASK_TITLE = task_title

            output += f"## Resuming Task #{task_id}: {task_title}\n\n"
            output += f"**Status:** {best_task.get('status')}\n"
            output += f"\nAll prompts will be tracked under this task.\n"

            auto_log_activity("auto_resume", duration=1)

        elif in_progress:
            task = in_progress[0]
            output += f"## Active Task Found\n\n"
            output += f"**#{task.get('task_number')}**: {task.get('title')}\n\n"
            output += f"Use `monkey_task_work({task.get('task_number')})` to continue it.\n"
        else:
            output += "## No Matching Task Found\n\n"
            output += "Consider creating a new task if this is trackable work.\n"

        return output

    except Exception as e:
        return f"Error checking context: {str(e)}"


# ============================================================
# VERSIONS
# ============================================================

@mcp.tool()
def monkey_version_list() -> str:
    """List all versions for this project."""
    try:
        result = api_get("/api/versions")
        versions = result.get("versions", [])

        if not versions:
            return "No versions found."

        output = "# Versions\n\n"
        for v in versions:
            locked = " (locked)" if v.get("locked") else ""
            output += f"- **{v.get('key')}**: {v.get('label')}{locked}\n"

        return output
    except Exception as e:
        return f"Error fetching versions: {str(e)}"


@mcp.tool()
def monkey_version_create(key: str, label: str, description: str = "", sort_order: int = 0) -> str:
    """Create a new version."""
    try:
        api_post("/api/versions", {
            "key": key,
            "label": label,
            "description": description,
            "sort_order": sort_order
        })
        return f"Created version: {label}"
    except Exception as e:
        return f"Error creating version: {str(e)}"


# ============================================================
# TEAM MEMBERS
# ============================================================

@mcp.tool()
def monkey_team_list() -> str:
    """List all team members for this project."""
    try:
        result = api_get("/api/team")
        members = result.get("members", [])

        if not members:
            return "No team members found."

        output = "# Team Members\n\n"
        for m in members:
            output += f"- **{m.get('name')}** ({m.get('role') or 'member'})\n"

        return output
    except Exception as e:
        return f"Error fetching team: {str(e)}"


@mcp.tool()
def monkey_team_add(name: str, email: str = "", role: str = "", color: str = "#6366f1") -> str:
    """Add a new team member."""
    try:
        api_post("/api/team", {
            "name": name,
            "email": email,
            "role": role,
            "color": color
        })
        return f"Added team member: {name}"
    except Exception as e:
        return f"Error adding team member: {str(e)}"


# ============================================================
# MACHINES
# ============================================================

@mcp.tool()
def monkey_machine_list() -> str:
    """List all machines for this project."""
    try:
        result = api_get("/api/machines")
        machines = result.get("machines", [])

        if not machines:
            return "No machines found."

        output = "# Machines\n\n"
        for m in machines:
            status_icon = {"active": "ON", "paused": "PAUSED", "draft": "DRAFT"}.get(m.get("status"), "?")
            output += f"- [{status_icon}] **{m.get('name')}**\n"
            if m.get("description"):
                output += f"  {m.get('description')[:80]}...\n"

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
    """Create a new machine (automated business process)."""
    try:
        result = api_post("/api/machines", {
            "name": name,
            "description": description,
            "goal": goal,
            "status": status
        })
        machine = result.get("machine", result)
        return f"Created machine: {name} (ID: {machine.get('id')})"
    except Exception as e:
        return f"Error creating machine: {str(e)}"


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
