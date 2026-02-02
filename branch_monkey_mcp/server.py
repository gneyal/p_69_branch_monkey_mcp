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


def save_token(token: str, api_url: str, org_id: str = None):
    """Save token to disk."""
    token_path = get_token_path()
    data = {
        "access_token": token,
        "api_url": api_url,
        "saved_at": time.time()
    }
    if org_id:
        data["org_id"] = org_id
    with open(token_path, "w") as f:
        json.dump(data, f)
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


def device_code_flow(api_url: str) -> Optional[dict]:
    """Run the device code flow to authenticate.

    Returns dict with 'access_token' and 'org_id' on success, None on failure.
    """
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
                print("  Approved! You can now use Branch Monkey.", file=sys.stderr)
                return {
                    "access_token": poll_data.get("access_token"),
                    "org_id": poll_data.get("org_id")
                }

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

# Fallback URL if config fetch fails
FALLBACK_API_URL = "https://p-63-branch-monkey.pages.dev"

def _fetch_api_url() -> str:
    """Fetch API URL from /api/config endpoint."""
    try:
        import httpx
        response = httpx.get(f"{FALLBACK_API_URL}/api/config", timeout=5.0)
        if response.status_code == 200:
            config = response.json()
            app_domain = config.get("appDomain")
            if app_domain:
                return f"https://{app_domain}"
    except Exception:
        pass
    return FALLBACK_API_URL

API_URL = os.environ.get("BRANCH_MONKEY_API_URL") or _fetch_api_url()
API_KEY = os.environ.get("BRANCH_MONKEY_API_KEY")
ORG_ID: Optional[str] = None  # Set after auth
REQUEST_TIMEOUT = 30

if not API_KEY:
    stored = load_stored_token(API_URL)
    if stored:
        API_KEY = stored.get("access_token")
        ORG_ID = stored.get("org_id")
    else:
        auth_result = device_code_flow(API_URL)
        if auth_result:
            API_KEY = auth_result.get("access_token")
            ORG_ID = auth_result.get("org_id")
            save_token(API_KEY, API_URL, ORG_ID)
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
    global API_KEY, ORG_ID, _session
    url = f"{API_URL.rstrip('/')}/{endpoint.lstrip('/')}"

    headers = kwargs.pop("headers", {})
    if API_KEY:
        headers["Authorization"] = f"Bearer {API_KEY}"
    if ORG_ID:
        headers["X-Org-Id"] = ORG_ID
    if CURRENT_PROJECT_ID:
        headers["X-Project-Id"] = CURRENT_PROJECT_ID
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

        auth_result = device_code_flow(API_URL)
        if auth_result:
            API_KEY = auth_result.get("access_token")
            ORG_ID = auth_result.get("org_id")
            save_token(API_KEY, API_URL, ORG_ID)

            # Retry the request with new token
            headers["Authorization"] = f"Bearer {API_KEY}"
            if ORG_ID:
                headers["X-Org-Id"] = ORG_ID
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
    global API_KEY, ORG_ID, _session
    try:
        # Clear existing token
        clear_token()

        # Reset session to clear cached auth
        _session = None

        # Run device code flow
        auth_result = device_code_flow(API_URL)

        if auth_result:
            API_KEY = auth_result.get("access_token")
            ORG_ID = auth_result.get("org_id")
            save_token(API_KEY, API_URL, ORG_ID)
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
    machine_id: str = None
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
    task_id: str,
    title: str = None,
    description: str = None,
    status: str = None,
    priority: int = None,
    version: str = None,
    machine_id: str = None
) -> str:
    """Update an existing task.

    Args:
        task_id: Task number (e.g., "123") or UUID (e.g., "abc-def-...")
    """
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
            updates["machine_id"] = machine_id if machine_id else None

        api_put(f"/api/tasks/{task_id}", updates)
        return f"✅ Updated task {task_id}"
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
def monkey_task_work(task_id: int, workflow: str = "execute") -> str:
    """Start working on a task. Sets status to in_progress and logs start.

    Args:
        task_id: The task number to work on
        workflow: Required workflow type:
            - "ask": Quick question/research - answer directly, no code changes
            - "plan": Design/architecture - create plan, get approval before implementing
            - "execute": Implementation - create worktree, code, PR, complete with context
    """
    global CURRENT_TASK_ID, CURRENT_TASK_TITLE

    # Validate workflow
    valid_workflows = ["ask", "plan", "execute"]
    if workflow not in valid_workflows:
        return f"❌ Invalid workflow '{workflow}'. Must be one of: {', '.join(valid_workflows)}"

    try:
        # Start working on task (workflow is guidance only, not stored)
        result = api_post(f"/api/tasks/{task_id}/work")
        task = result.get("task", {})

        CURRENT_TASK_ID = task_id
        CURRENT_TASK_TITLE = task.get('title', 'Unknown')

        auto_log_activity("task_work_start", duration=1)

        # Workflow-specific instructions
        if workflow == "ask":
            next_steps = """**Next Steps (Ask Workflow):**
1. Research/explore to answer the question
2. Use `monkey_task_log` to record findings
3. Use `monkey_task_update` to mark done when answered"""
        elif workflow == "plan":
            next_steps = """**Next Steps (Plan Workflow):**
1. Research the codebase and requirements
2. Create a plan/design document
3. Use `monkey_task_log` to record the plan
4. Get user approval before implementing
5. If approved, switch to execute workflow or create sub-tasks"""
        else:  # execute
            next_steps = f"""**Next Steps (Execute Workflow):**

**Step 1: Create Worktree** (isolates your changes)
```bash
git worktree add .worktrees/task-{task_id} -b task/{task_id}-short-description
cd .worktrees/task-{task_id}
```

**Step 2: Implement Changes**
- Make changes in the worktree (NOT the main repo)
- Use `monkey_task_log()` to record progress

**Step 3: Commit & Push**
```bash
git add .
git commit -m "Task #{task_id}: description

Co-Authored-By: Kompany.dev via Claude Code"
git push -u origin task/{task_id}-short-description
```

**Step 4: Complete Task**
`monkey_task_complete(task_id={task_id}, summary="...", worktree_path=".worktrees/task-{task_id}")`

This creates a GitHub PR. The user reviews and merges it (NOT auto-merged)."""

        return f"""# Working on Task {task_id}: {task.get('title', 'Unknown')}

**Workflow:** {workflow.upper()}
**Status:** in_progress
**Description:** {task.get('description') or '(none)'}
**Version:** {task.get('version') or 'backlog'}

{next_steps}"""
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
def monkey_task_complete(
    task_id: int,
    summary: str,
    worktree_path: str = None,
    files_changed: str = None,
    context_name: str = None
) -> str:
    """Mark a task as complete, create a PR using gh CLI, and link everything.

    This will:
    1. Run 'gh pr create --fill' to create a GitHub PR (from worktree directory)
    2. Mark the task as complete with the PR URL
    3. Create a linked context with the summary

    Args:
        task_id: The task number to complete
        summary: Summary of what was done
        worktree_path: Path to the worktree directory (required for PR creation)
        files_changed: Comma-separated list of files that were modified (e.g., "src/foo.ts, src/bar.ts")
        context_name: Optional name for the context (defaults to task title)
    """
    global CURRENT_TASK_ID, CURRENT_TASK_TITLE
    try:
        github_pr_url = None
        pr_output = ""

        # Try to create PR using gh CLI
        try:
            pr_result = subprocess.run(
                ["gh", "pr", "create", "--fill"],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=worktree_path if worktree_path else None
            )
            pr_output = pr_result.stdout + pr_result.stderr

            # Extract PR URL from output (gh pr create outputs the URL)
            import re
            pr_match = re.search(r'https://github\.com/[^/]+/[^/]+/pull/\d+', pr_output)
            if pr_match:
                github_pr_url = pr_match.group(0)
        except FileNotFoundError:
            pr_output = "gh CLI not found - skipping PR creation"
        except subprocess.TimeoutExpired:
            pr_output = "gh pr create timed out"
        except Exception as e:
            pr_output = f"PR creation failed: {str(e)}"

        payload = {"summary": summary}
        if github_pr_url:
            payload["github_pr_url"] = github_pr_url
        if files_changed:
            payload["files_changed"] = files_changed
        # Use /in_review endpoint to move task to "In Review" status for human verification
        result = api_post(f"/api/tasks/{task_id}/in_review", payload)
        task = result.get("task", {})
        task_title = task.get('title', 'Unknown')
        task_uuid = task.get('id')

        auto_log_activity("task_complete", duration=1)

        CURRENT_TASK_ID = None
        CURRENT_TASK_TITLE = None

        output = f"✅ Task {task_id} completed: {task_title}\n\nSummary: {summary}"
        if github_pr_url:
            output += f"\n\n🔗 PR created: {github_pr_url}"
        elif pr_output:
            output += f"\n\n⚠️ PR: {pr_output}"

        # Auto-create and link context if project is focused
        if CURRENT_PROJECT_ID and task_uuid:
            try:
                # Build context content
                context_content = ""
                if files_changed:
                    context_content += f"Files changed:\n"
                    for f in files_changed.split(","):
                        context_content += f"- {f.strip()}\n"
                    context_content += "\n"
                context_content += summary

                # Create context
                ctx_name = context_name or f"Task #{task_id}: {task_title[:50]}"
                ctx_result = api_post("/api/contexts", {
                    "name": ctx_name,
                    "content": context_content,
                    "context_type": "code",
                    "project_id": CURRENT_PROJECT_ID
                })
                context = ctx_result.get("context", ctx_result)
                context_id = context.get("id")

                # Link context to task
                if context_id:
                    api_post(f"/api/contexts/task/{task_uuid}", {"context_id": context_id})
                    output += f"\n\n📎 Context created and linked: {ctx_name}"

            except Exception as ctx_err:
                output += f"\n\n⚠️ Could not create context: {str(ctx_err)}"

        return output
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
            status_icon = {"todo": "⬜", "in_progress": "🔄", "done": "✅", "in_review": "👀"}.get(task.get("status"), "⬜")
            task_num = task.get('task_number', 'None')
            task_uuid = task.get('id', 'N/A')
            output += f"{status_icon} **#{task_num}** `{task_uuid}`: {task.get('title')}\n"
            if task.get("description"):
                desc = task.get('description', '')
                # Show full description, truncate if very long
                if len(desc) > 500:
                    desc = desc[:500] + "..."
                output += f"   📝 {desc}\n"

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


@mcp.tool()
def monkey_machine_get(machine_id: str) -> str:
    """Get a specific machine by ID.

    Args:
        machine_id: The UUID of the machine to retrieve
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/machines/{machine_id}")
        machine = result.get("machine", result)

        output = f"# Machine: {machine.get('name')}\n\n"
        output += f"**ID:** `{machine.get('id')}`\n"
        output += f"**Status:** {machine.get('status', 'unknown')}\n"
        if machine.get('description'):
            output += f"**Description:** {machine.get('description')}\n"
        if machine.get('goal'):
            output += f"**Goal:** {machine.get('goal')}\n"
        output += f"**Position:** ({machine.get('position_x', 0)}, {machine.get('position_y', 0)})\n"

        return output
    except Exception as e:
        return f"Error fetching machine: {str(e)}"


@mcp.tool()
def monkey_machine_update(
    machine_id: str,
    name: str = None,
    description: str = None,
    goal: str = None,
    status: str = None,
    position_x: float = None,
    position_y: float = None
) -> str:
    """Update an existing machine.

    Args:
        machine_id: The UUID of the machine to update
        name: New name (optional)
        description: New description (optional)
        goal: New goal (optional)
        status: New status - active, paused, or draft (optional)
        position_x: New X position (optional)
        position_y: New Y position (optional)
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        updates = {}
        if name is not None:
            updates["name"] = name
        if description is not None:
            updates["description"] = description
        if goal is not None:
            updates["goal"] = goal
        if status is not None:
            updates["status"] = status
        if position_x is not None:
            updates["position_x"] = position_x
        if position_y is not None:
            updates["position_y"] = position_y

        if not updates:
            return "⚠️ No updates provided. Specify at least one field to update."

        result = api_put(f"/api/machines/{machine_id}", updates)
        machine = result.get("machine", result)
        return f"✅ Updated machine: {machine.get('name')} (ID: {machine_id})"
    except Exception as e:
        return f"Error updating machine: {str(e)}"


@mcp.tool()
def monkey_machine_delete(machine_id: str) -> str:
    """Delete a machine by ID.

    Args:
        machine_id: The UUID of the machine to delete
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_delete(f"/api/machines/{machine_id}")
        return f"✅ Deleted machine (ID: {machine_id})"
    except Exception as e:
        return f"Error deleting machine: {str(e)}"


# ============================================================
# COMPANY NOTES
# ============================================================

@mcp.tool()
def monkey_note_list() -> str:
    """List all company notes for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        endpoint = f"/api/company-notes?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        notes = result.get("notes", [])

        if not notes:
            return f"No notes found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Notes (Project: {CURRENT_PROJECT_NAME})\n\n"
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
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_post("/api/company-notes", {
            "content": content,
            "color": color,
            "position_x": position_x,
            "position_y": position_y,
            "width": width,
            "height": height,
            "project_id": CURRENT_PROJECT_ID
        })
        note = result.get("note", result)
        return f"✅ Created note (ID: {note.get('id')}) in project {CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating note: {str(e)}"


@mcp.tool()
def monkey_note_get(note_id: str) -> str:
    """Get a specific note by ID.

    Args:
        note_id: The UUID of the note to retrieve
    """
    if not CURRENT_PROJECT_ID:
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
    if not CURRENT_PROJECT_ID:
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

        result = api_put(f"/api/company-notes/{note_id}", updates)
        return f"✅ Updated note (ID: {note_id})"
    except Exception as e:
        return f"Error updating note: {str(e)}"


@mcp.tool()
def monkey_note_delete(note_id: str) -> str:
    """Delete a note by ID.

    Args:
        note_id: The UUID of the note to delete
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        api_delete(f"/api/company-notes/{note_id}")
        return f"✅ Deleted note (ID: {note_id})"
    except Exception as e:
        return f"Error deleting note: {str(e)}"


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
# CONTEXTS
# ============================================================

@mcp.tool()
def monkey_context_list() -> str:
    """List all contexts for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/contexts?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        contexts = result.get("contexts", [])

        if not contexts:
            return f"No contexts found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Contexts (Project: {CURRENT_PROJECT_NAME})\n\n"
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
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_post("/api/contexts", {
            "name": name,
            "content": content,
            "context_type": context_type,
            "project_id": CURRENT_PROJECT_ID
        })
        context = result.get("context", result)
        return f"✅ Created context: {name} (ID: `{context.get('id')}`) in project {CURRENT_PROJECT_NAME}"
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
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/contexts/search/{query}?project_id={CURRENT_PROJECT_ID}")
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
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/contexts/recent?limit={limit}&project_id={CURRENT_PROJECT_ID}")
        contexts = result.get("contexts", [])

        if not contexts:
            return f"No recent contexts found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Recent Contexts (Project: {CURRENT_PROJECT_NAME})\n\n"
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
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/contexts/similar/{task_id}?project_id={CURRENT_PROJECT_ID}")
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


# ============================================================
# AGENT DEFINITIONS
# ============================================================

@mcp.tool()
def monkey_agent_list() -> str:
    """List all agent definitions for the current project.

    Agents are custom AI personas with system prompts that can be assigned to tasks.
    Requires a project to be focused first using monkey_project_focus.
    """
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first.\n\nUse `monkey_project_list` to see available projects."

    try:
        endpoint = f"/api/agent-definitions?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        agents = result.get("agents", [])

        if not agents:
            return f"No agents found for project **{CURRENT_PROJECT_NAME}**."

        output = f"# Agent Definitions (Project: {CURRENT_PROJECT_NAME})\n\n"
        for a in agents:
            is_default = "✓" if a.get('is_default') else ""
            tools_info = ""
            if a.get('allowed_tools') is not None:
                tool_count = len(a.get('allowed_tools', []))
                tools_info = f" | {tool_count} tools enabled"
            output += f"- **{a.get('name')}** (`{a.get('slug')}`) {is_default}{tools_info}\n"
            output += f"   {a.get('description', '')}\n"
            output += f"   Color: {a.get('color', '#6366f1')} | ID: `{a.get('id')}`\n\n"

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
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        payload = {
            "name": name,
            "system_prompt": system_prompt,
            "description": description,
            "color": color,
            "icon": icon,
            "project_id": CURRENT_PROJECT_ID
        }

        if allowed_tools is not None:
            payload["allowed_tools"] = [t.strip() for t in allowed_tools.split(",") if t.strip()]

        result = api_post("/api/agent-definitions", payload)
        agent = result.get("agent", result)
        return f"✅ Created agent: **{name}** (slug: `{agent.get('slug')}`, ID: `{agent.get('id')}`) in project {CURRENT_PROJECT_NAME}"
    except Exception as e:
        return f"Error creating agent: {str(e)}"


@mcp.tool()
def monkey_agent_get(agent_id: str) -> str:
    """Get a specific agent definition by ID.

    Args:
        agent_id: The UUID of the agent to retrieve
    """
    try:
        result = api_get(f"/api/agent-definitions/{agent_id}")
        agent = result.get("agent", {})

        if not agent:
            return f"❌ Agent not found: {agent_id}"

        output = f"# Agent: {agent.get('name')}\n\n"
        output += f"**ID:** `{agent.get('id')}`\n"
        output += f"**Slug:** `{agent.get('slug')}`\n"
        output += f"**Description:** {agent.get('description', 'N/A')}\n"
        output += f"**Color:** {agent.get('color', '#6366f1')}\n"
        output += f"**Icon:** {agent.get('icon', 'bot')}\n"
        output += f"**Default:** {'Yes' if agent.get('is_default') else 'No'}\n"
        output += f"**Created:** {agent.get('created_at', '')[:19]}\n"
        output += f"**Updated:** {agent.get('updated_at', '')[:19]}\n\n"

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

    Fetches the agent's system prompt from the database and executes it
    with the provided instructions via the local relay.

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
    if not CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        # Fetch agent by slug from database
        endpoint = f"/api/agent-definitions?project_id={CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        agents = result.get("agents", [])

        # Find agent by slug
        agent = None
        for a in agents:
            if a.get("slug") == agent_slug:
                agent = a
                break

        if not agent:
            return f"❌ Agent not found: {agent_slug}\n\nAvailable agents: {', '.join(a.get('slug', '') for a in agents)}"

        # Build payload for relay
        payload = {
            "agent_id": agent.get("id"),
            "agent_slug": agent_slug,
            "agent_name": agent.get("name"),
            "system_prompt": agent.get("system_prompt"),
            "instructions": instructions,
            "project_id": CURRENT_PROJECT_ID
        }

        # Parse and add context if provided
        if context:
            try:
                import json
                payload["context"] = json.loads(context) if isinstance(context, str) else context
            except json.JSONDecodeError:
                payload["context"] = {"raw": context}

        # Send to relay for execution
        result = api_post("/api/relay/apply-agent", payload)

        if result.get("error"):
            return f"❌ Agent execution failed: {result.get('error')}"

        output = result.get("output", result.get("result", ""))

        return f"""# Agent: {agent.get('name')} ({agent_slug})

## Instructions
{instructions}

## Response
{output}
"""

    except Exception as e:
        return f"Error applying agent: {str(e)}"


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
