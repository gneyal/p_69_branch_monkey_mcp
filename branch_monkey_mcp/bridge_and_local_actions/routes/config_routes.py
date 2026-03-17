"""
Configuration endpoints for the local server.
"""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import (
    get_home_directory,
    get_default_working_dir,
    set_default_working_dir,
    get_app_config,
)
from ..cli_providers import get_available_providers, get_default_cli, set_default_cli, get_provider
from ..git_utils import get_git_root

router = APIRouter()


class WorkingDirectoryRequest(BaseModel):
    """Request to set working directory."""
    directory: str


class CliPreferenceRequest(BaseModel):
    """Request to set default CLI tool."""
    cli_tool: str


class CliApiKeyRequest(BaseModel):
    """Request to set an API key for a CLI provider."""
    cli_tool: str
    api_key: str


class CliDeviceAuthRequest(BaseModel):
    """Request to start device auth for a CLI provider."""
    cli_tool: str


@router.get("/config/working-directory")
def get_working_directory():
    """Get the home and current project directory for agent execution."""
    home_dir = get_home_directory()
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)

    # Check if it's a valid git repo
    is_git = git_root is not None

    # Count worktrees if it's a git repo
    worktree_count = 0
    if git_root:
        worktrees_dir = Path(git_root) / ".worktrees"
        if worktrees_dir.exists():
            worktree_count = len([d for d in worktrees_dir.iterdir() if d.is_dir()])

    return {
        "home_directory": home_dir,
        "working_directory": work_dir,
        "git_root": git_root,
        "is_git_repo": is_git,
        "worktree_count": worktree_count
    }


@router.post("/config/working-directory")
def set_working_directory_endpoint(request: WorkingDirectoryRequest):
    """Set the working directory for agent execution."""
    directory = request.directory

    # Validate directory exists
    if not os.path.isdir(directory):
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {directory}")

    # Resolve to absolute path
    abs_path = os.path.abspath(directory)

    # Check if it's a git repo
    git_root = get_git_root(abs_path)
    home_dir = get_home_directory()

    # Allow setting to home directory (to clear project selection) or any git repo
    if not git_root and abs_path != home_dir:
        raise HTTPException(status_code=400, detail=f"Not a git repository: {abs_path}")

    # Set the new working directory
    set_default_working_dir(abs_path)

    # Count worktrees
    worktree_count = 0
    if git_root:
        worktrees_dir = Path(git_root) / ".worktrees"
        if worktrees_dir.exists():
            worktree_count = len([d for d in worktrees_dir.iterdir() if d.is_dir()])

    return {
        "status": "ok",
        "home_directory": home_dir,
        "working_directory": abs_path,
        "git_root": git_root,
        "is_git_repo": git_root is not None,
        "worktree_count": worktree_count
    }


@router.get("/config/cli")
def get_cli_config():
    """Get CLI provider configuration: default CLI and available providers."""
    return {
        "default_cli": get_default_cli(),
        "providers": get_available_providers(),
    }


@router.post("/config/cli")
def set_cli_config(request: CliPreferenceRequest):
    """Set the default CLI provider."""
    try:
        set_default_cli(request.cli_tool)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "status": "ok",
        "default_cli": request.cli_tool,
        "providers": get_available_providers(),
    }


@router.post("/config/cli/api-key")
def set_cli_api_key(request: CliApiKeyRequest):
    """Set an API key for a CLI provider."""
    try:
        provider = get_provider(request.cli_tool)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not provider.api_key_config:
        raise HTTPException(status_code=400, detail=f"{provider.display_name} does not support API key auth")

    provider.set_api_key(request.api_key)

    return {
        "status": "ok",
        "cli_tool": request.cli_tool,
        "auth": provider.get_auth_status(),
    }


@router.delete("/config/cli/api-key/{cli_tool}")
def clear_cli_api_key(cli_tool: str):
    """Remove a stored API key for a CLI provider."""
    try:
        provider = get_provider(cli_tool)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    provider.clear_api_key()

    return {
        "status": "ok",
        "cli_tool": cli_tool,
        "auth": provider.get_auth_status(),
    }


@router.post("/config/cli/device-auth")
def start_cli_device_auth(request: CliDeviceAuthRequest):
    """Start device auth flow for a CLI provider. Returns URL + code."""
    try:
        provider = get_provider(request.cli_tool)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    result = provider.start_device_auth()
    if not result:
        raise HTTPException(status_code=400, detail=f"{provider.display_name} does not support device auth or is not installed")

    # Don't return internal _process handle
    result.pop("_process", None)

    return {
        "status": "ok",
        "cli_tool": request.cli_tool,
        **result,
    }


@router.get("/config/cli/auth/{cli_tool}")
def get_cli_auth_status(cli_tool: str):
    """Get auth status for a specific CLI provider."""
    try:
        provider = get_provider(cli_tool)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "cli_tool": cli_tool,
        **provider.get_auth_status(),
    }


@router.get("/config")
async def get_config():
    """Get app configuration, proxied from cloud with caching."""
    return await get_app_config()
