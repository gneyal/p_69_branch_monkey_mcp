"""
Worktree management endpoints for the local server.
"""

import os
import re
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException

from ..config import get_default_working_dir
from ..git_utils import get_git_root, get_current_branch
from ..worktree import remove_worktree, find_worktree_path

router = APIRouter()


@router.delete("/worktree")
def delete_worktree_endpoint(task_number: int, worktree_path: str):
    """Delete a git worktree."""
    try:
        work_dir = get_default_working_dir()
        git_root = get_git_root(work_dir) or work_dir
        remove_worktree(git_root, worktree_path)
        return {"success": True, "message": f"Worktree deleted: {worktree_path}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete worktree: {str(e)}")


@router.get("/worktrees")
def list_worktrees():
    """List all git worktrees in the repository with detailed info."""
    try:
        work_dir = get_default_working_dir()
        git_root = get_git_root(work_dir)
        if not git_root:
            return {"worktrees": [], "error": "Not in a git repository"}

        # Get worktree list
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=git_root, capture_output=True, text=True
        )

        if result.returncode != 0:
            return {"worktrees": [], "error": "Failed to list worktrees"}

        worktrees = []
        current_wt = {}

        for line in result.stdout.strip().split('\n'):
            if not line:
                if current_wt:
                    worktrees.append(current_wt)
                    current_wt = {}
                continue

            if line.startswith('worktree '):
                current_wt['path'] = line[9:]
            elif line.startswith('HEAD '):
                current_wt['head'] = line[5:]
            elif line.startswith('branch '):
                branch = line[7:]
                if branch.startswith('refs/heads/'):
                    branch = branch[11:]
                current_wt['branch'] = branch
            elif line == 'bare':
                current_wt['bare'] = True
            elif line == 'detached':
                current_wt['detached'] = True

        if current_wt:
            worktrees.append(current_wt)

        # Filter to only task worktrees
        task_worktrees = [
            wt for wt in worktrees
            if '.worktrees' in wt.get('path', '') or wt.get('branch', '').startswith('task/')
        ]

        # Enrich each worktree with additional info
        for wt in task_worktrees:
            path = wt.get('path', '')
            branch = wt.get('branch', '')

            # Extract task number
            task_number = None
            match = re.search(r'task-(\d+)', path)
            if match:
                task_number = int(match.group(1))
            elif branch:
                match = re.search(r'task/(\d+)', branch)
                if match:
                    task_number = int(match.group(1))

            wt['task_number'] = task_number

            # Check git status in worktree
            if os.path.isdir(path):
                try:
                    status_result = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=path, capture_output=True, text=True, timeout=5
                    )
                    changes = [l for l in status_result.stdout.strip().split('\n') if l]
                    wt['changes_count'] = len(changes)
                    wt['is_clean'] = len(changes) == 0
                except Exception:
                    wt['changes_count'] = None
                    wt['is_clean'] = None

                # Get last commit info
                try:
                    log_result = subprocess.run(
                        ["git", "log", "-1", "--pretty=format:%s|%ar"],
                        cwd=path, capture_output=True, text=True, timeout=5
                    )
                    if log_result.returncode == 0 and log_result.stdout:
                        parts = log_result.stdout.split('|')
                        if len(parts) >= 2:
                            wt['last_commit_message'] = parts[0][:80]
                            wt['last_commit_time'] = parts[1]
                except Exception:
                    pass

        return {"worktrees": task_worktrees, "git_root": git_root}
    except Exception as e:
        return {"worktrees": [], "error": str(e)}


@router.get("/worktree/{task_number}")
def get_worktree(task_number: int):
    """Get worktree info for a specific task."""
    worktree_path = find_worktree_path(task_number)
    if not worktree_path:
        raise HTTPException(status_code=404, detail=f"No worktree found for task {task_number}")

    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)

    # Get branch name
    branch = None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=worktree_path,
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            branch = result.stdout.strip()
    except Exception:
        pass

    # Get changes count
    changes_count = 0
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_path,
            capture_output=True,
            text=True
        )
        if result.returncode == 0:
            changes = [l for l in result.stdout.strip().split('\n') if l]
            changes_count = len(changes)
    except Exception:
        pass

    return {
        "task_number": task_number,
        "path": worktree_path,
        "branch": branch,
        "changes_count": changes_count,
        "git_root": git_root
    }
