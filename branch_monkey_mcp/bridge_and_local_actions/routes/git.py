"""
Git status and commit endpoints for the local server.
"""

import os
import subprocess
from typing import List, Optional

from fastapi import APIRouter, HTTPException

from ..config import get_default_working_dir
from ..git_utils import is_git_repo, get_git_root, get_current_branch

router = APIRouter()


@router.get("/git-status")
def get_git_status(path: str = None):
    """Get git status for a directory.

    Args:
        path: Directory path to check. Defaults to working directory.

    Returns:
        {
            is_clean: bool - True if working tree is clean,
            changes_count: int - Number of changed files,
            branch: str - Current branch name,
            staged: int - Number of staged files,
            unstaged: int - Number of unstaged files,
            untracked: int - Number of untracked files
        }
    """
    directory = path or get_default_working_dir()

    if not directory or not os.path.isdir(directory):
        return {"error": "Invalid directory", "is_clean": None, "changes_count": 0}

    if not is_git_repo(directory):
        return {"error": "Not a git repository", "is_clean": None, "changes_count": 0}

    try:
        # Get current branch
        branch = get_current_branch(directory) or "unknown"

        # Get porcelain status
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True
        )

        lines = [l for l in result.stdout.strip().split('\n') if l]

        staged = 0
        unstaged = 0
        untracked = 0

        for line in lines:
            if len(line) >= 2:
                index_status = line[0]
                worktree_status = line[1]

                if index_status == '?':
                    untracked += 1
                elif index_status != ' ':
                    staged += 1

                if worktree_status not in (' ', '?'):
                    unstaged += 1

        changes_count = len(lines)

        return {
            "is_clean": changes_count == 0,
            "changes_count": changes_count,
            "branch": branch,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked
        }
    except subprocess.CalledProcessError as e:
        return {"error": str(e), "is_clean": None, "changes_count": 0}
    except Exception as e:
        return {"error": str(e), "is_clean": None, "changes_count": 0}


@router.get("/local-claude/commits")
def list_commits(limit: int = 10, branch: Optional[str] = None, all_branches: bool = False):
    """List recent commits."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        cmd = ["git", "log", f"-{limit}", "--pretty=format:%H|%s|%an|%ar|%ai"]
        if all_branches:
            cmd.append("--all")
        elif branch:
            cmd.append(branch)

        result = subprocess.run(
            cmd,
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            return {"commits": [], "error": result.stderr}

        commits = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('|', 4)
            if len(parts) >= 5:
                commits.append({
                    "hash": parts[0],
                    "shortHash": parts[0][:7],
                    "message": parts[1],
                    "author": parts[2],
                    "relativeDate": parts[3],
                    "date": parts[4]
                })

        return {"commits": commits}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/local-claude/commit-diff/{sha}")
def get_commit_diff(sha: str):
    """Get diff for a specific commit."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        result = subprocess.run(
            ["git", "show", sha, "--stat", "--patch"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise HTTPException(status_code=404, detail=f"Commit not found: {sha}")

        return {"sha": sha, "diff": result.stdout}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/checkout/{sha}")
def checkout_commit(sha: str):
    """Checkout to a specific commit.

    This will checkout the working directory to the specified commit.
    Warning: This will detach HEAD if checking out a commit that's not a branch tip.
    """
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        # First check if there are uncommitted changes
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if status_result.stdout.strip():
            raise HTTPException(
                status_code=400,
                detail="Cannot checkout: you have uncommitted changes. Please commit or stash them first."
            )

        # Checkout to the commit
        result = subprocess.run(
            ["git", "checkout", sha],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise HTTPException(status_code=400, detail=f"Checkout failed: {result.stderr}")

        # Get current branch/commit info after checkout
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        current_ref = branch_result.stdout.strip()

        return {
            "success": True,
            "sha": sha,
            "current_ref": current_ref,
            "detached": current_ref == "HEAD",
            "message": f"Checked out to {sha[:7]}"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
