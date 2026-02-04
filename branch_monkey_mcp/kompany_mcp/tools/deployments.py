"""
Deployment configuration management tools.

Allows agents to manage deployment settings for projects - production URLs,
platforms, branches, and custom configuration.
"""

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp


@mcp.tool()
def monkey_deploy_list() -> str:
    """List all deployment configurations for the current project.

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        endpoint = f"/api/deployments?project_id={state.CURRENT_PROJECT_ID}"
        result = api_get(endpoint)
        deployments = result.get("deployments", [])

        if not deployments:
            return f"No deployments configured for project **{state.CURRENT_PROJECT_NAME}**.\n\nUse `monkey_deploy_create` to add one."

        output = f"# Deployments (Project: {state.CURRENT_PROJECT_NAME})\n\n"
        for d in deployments:
            env_icon = {"production": "🚀", "staging": "🔶", "preview": "👁️"}.get(d.get("environment"), "📦")
            output += f"{env_icon} **{d.get('name')}** ({d.get('environment')})\n"
            output += f"   Platform: {d.get('platform', 'unknown')}\n"
            if d.get('url'):
                output += f"   URL: {d.get('url')}\n"
            if d.get('branch'):
                output += f"   Branch: `{d.get('branch')}`\n"
            output += f"   ID: `{d.get('id')}`\n\n"

        return output
    except Exception as e:
        return f"Error fetching deployments: {str(e)}"


@mcp.tool()
def monkey_deploy_create(
    name: str,
    platform: str,
    environment: str = "production",
    url: str = None,
    branch: str = "main",
    auto_deploy: bool = True,
    config: str = None
) -> str:
    """Create a new deployment configuration.

    Args:
        name: Display name (e.g., "Production", "Staging")
        platform: Deployment platform (cloudflare, vercel, netlify, railway, render, fly, other)
        environment: Environment type (production, staging, preview, development)
        url: The deployed URL (e.g., https://myapp.pages.dev)
        branch: Git branch that triggers deployment (default: main)
        auto_deploy: Whether pushes auto-deploy (default: True)
        config: JSON string with platform-specific config (optional)

    Requires a project to be focused first using monkey_project_focus.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        data = {
            "name": name,
            "platform": platform,
            "environment": environment,
            "branch": branch,
            "auto_deploy": auto_deploy,
            "project_id": state.CURRENT_PROJECT_ID
        }
        if url:
            data["url"] = url
        if config:
            data["config"] = config

        result = api_post("/api/deployments", data)
        deployment = result.get("deployment", result)
        return f"✅ Created deployment: {name} ({platform}) → {url or 'no URL yet'}"
    except Exception as e:
        return f"Error creating deployment: {str(e)}"


@mcp.tool()
def monkey_deploy_get(deployment_id: str) -> str:
    """Get details of a specific deployment configuration.

    Args:
        deployment_id: The UUID of the deployment to retrieve
    """
    try:
        result = api_get(f"/api/deployments/{deployment_id}")
        d = result.get("deployment", result)

        output = f"# Deployment: {d.get('name')}\n\n"
        output += f"**ID:** `{d.get('id')}`\n"
        output += f"**Environment:** {d.get('environment', 'unknown')}\n"
        output += f"**Platform:** {d.get('platform', 'unknown')}\n"
        output += f"**URL:** {d.get('url') or '(not set)'}\n"
        output += f"**Branch:** `{d.get('branch', 'main')}`\n"
        output += f"**Auto-deploy:** {'Yes' if d.get('auto_deploy') else 'No'}\n"

        if d.get('config'):
            output += f"\n**Config:**\n```json\n{d.get('config')}\n```\n"

        if d.get('last_deployed_at'):
            output += f"\n**Last deployed:** {d.get('last_deployed_at')}\n"
        if d.get('last_deployed_commit'):
            output += f"**Last commit:** `{d.get('last_deployed_commit')[:8]}`\n"

        return output
    except Exception as e:
        return f"Error fetching deployment: {str(e)}"


@mcp.tool()
def monkey_deploy_update(
    deployment_id: str,
    name: str = None,
    platform: str = None,
    environment: str = None,
    url: str = None,
    branch: str = None,
    auto_deploy: bool = None,
    config: str = None,
    last_deployed_at: str = None,
    last_deployed_commit: str = None
) -> str:
    """Update a deployment configuration.

    Args:
        deployment_id: The UUID of the deployment to update
        name: New display name (optional)
        platform: New platform (optional)
        environment: New environment type (optional)
        url: New deployed URL (optional)
        branch: New deploy branch (optional)
        auto_deploy: Enable/disable auto-deploy (optional)
        config: New JSON config string (optional)
        last_deployed_at: Timestamp of last deployment (optional)
        last_deployed_commit: Git commit SHA of last deployment (optional)
    """
    try:
        updates = {}
        if name is not None:
            updates["name"] = name
        if platform is not None:
            updates["platform"] = platform
        if environment is not None:
            updates["environment"] = environment
        if url is not None:
            updates["url"] = url
        if branch is not None:
            updates["branch"] = branch
        if auto_deploy is not None:
            updates["auto_deploy"] = auto_deploy
        if config is not None:
            updates["config"] = config
        if last_deployed_at is not None:
            updates["last_deployed_at"] = last_deployed_at
        if last_deployed_commit is not None:
            updates["last_deployed_commit"] = last_deployed_commit

        if not updates:
            return "⚠️ No updates provided."

        result = api_put(f"/api/deployments/{deployment_id}", updates)
        d = result.get("deployment", result)
        return f"✅ Updated deployment: {d.get('name', deployment_id)}"
    except Exception as e:
        return f"Error updating deployment: {str(e)}"


@mcp.tool()
def monkey_deploy_delete(deployment_id: str) -> str:
    """Delete a deployment configuration.

    Args:
        deployment_id: The UUID of the deployment to delete
    """
    try:
        api_delete(f"/api/deployments/{deployment_id}")
        return f"✅ Deleted deployment (ID: {deployment_id})"
    except Exception as e:
        return f"Error deleting deployment: {str(e)}"


@mcp.tool()
def monkey_deploy_detect() -> str:
    """Auto-detect deployment configuration from the current project's codebase.

    Looks for common config files:
    - wrangler.toml (Cloudflare)
    - vercel.json (Vercel)
    - netlify.toml (Netlify)
    - railway.json (Railway)
    - fly.toml (Fly.io)
    - render.yaml (Render)

    Returns detected configuration that can be used with monkey_deploy_create.
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    # This is a hint for the agent - actual detection happens in the agent's context
    return """To detect deployment configuration, check for these files in your codebase:

**Cloudflare Pages:**
- `wrangler.toml` - look for `name`, `pages_build_output_dir`

**Vercel:**
- `vercel.json` - look for `builds`, `routes`
- Check GitHub repo settings for connected Vercel project

**Netlify:**
- `netlify.toml` - look for `[build]` section
- `_redirects` file

**Railway:**
- `railway.json` or `railway.toml`

**Fly.io:**
- `fly.toml` - look for `app` name

**Render:**
- `render.yaml`

Once detected, use `monkey_deploy_create` with the found settings."""
