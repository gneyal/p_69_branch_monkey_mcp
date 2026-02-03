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
from ..git_utils import get_git_root

router = APIRouter()


class WorkingDirectoryRequest(BaseModel):
    """Request to set working directory."""
    directory: str


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


@router.get("/config")
async def get_config():
    """Get app configuration, proxied from cloud with caching."""
    return await get_app_config()
