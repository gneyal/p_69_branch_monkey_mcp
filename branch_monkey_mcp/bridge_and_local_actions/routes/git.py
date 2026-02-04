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


@router.get("/local-claude/branch-graph")
def get_branch_graph(limit: int = 50):
    """Get commits with branch graph data for visualization.

    Returns commits with parent relationships and branch info for
    rendering a visual branch graph like GitHub/GitKraken.
    """
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        # Get all branches with their current commits
        branches_result = subprocess.run(
            ["git", "branch", "-a", "--format=%(refname:short)|%(objectname)|%(upstream:short)"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        branches = {}
        branch_colors = ["#22c55e", "#3b82f6", "#f97316", "#a855f7", "#ec4899", "#14b8a6"]
        color_idx = 0

        for line in branches_result.stdout.strip().split('\n'):
            if not line or line.startswith('origin/HEAD'):
                continue
            parts = line.split('|')
            if len(parts) >= 2:
                branch_name = parts[0]
                commit_sha = parts[1]
                # Skip remote tracking refs that duplicate local branches
                if branch_name.startswith('origin/'):
                    local_name = branch_name.replace('origin/', '')
                    if local_name in branches:
                        continue

                # Assign color (main/master get green)
                if branch_name in ['main', 'master']:
                    color = "#22c55e"  # Green for main
                else:
                    color = branch_colors[color_idx % len(branch_colors)]
                    color_idx += 1

                branches[branch_name] = {
                    "name": branch_name,
                    "head": commit_sha,
                    "color": color
                }

        # Get commits with parent info - format: hash|parents|refs|message|author|date
        commits_result = subprocess.run(
            ["git", "log", f"-{limit}", "--all", "--pretty=format:%H|%P|%D|%s|%an|%ar|%ai", "--topo-order"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        commits = []
        commit_to_branches = {}  # Map commit -> branches that contain it

        for line in commits_result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('|', 6)
            if len(parts) >= 7:
                sha = parts[0]
                parents = parts[1].split() if parts[1] else []
                refs = parts[2]
                message = parts[3]
                author = parts[4]
                relative_date = parts[5]
                date = parts[6]

                # Parse refs to find branch names
                branch_refs = []
                is_head = False
                is_main = False
                if refs:
                    for ref in refs.split(', '):
                        ref = ref.strip()
                        if ref == 'HEAD':
                            is_head = True
                        elif ref.startswith('HEAD -> '):
                            is_head = True
                            branch_refs.append(ref.replace('HEAD -> ', ''))
                        elif not ref.startswith('origin/') and not ref.startswith('tag:'):
                            branch_refs.append(ref)

                        if 'main' in ref or 'master' in ref:
                            is_main = True

                commits.append({
                    "hash": sha,
                    "short_hash": sha[:7],
                    "parents": parents,
                    "parent_short": [p[:7] for p in parents],
                    "branches": branch_refs,
                    "is_head": is_head,
                    "is_main": is_main,
                    "message": message,
                    "author": author,
                    "relative_date": relative_date,
                    "date": date,
                    "refs": refs
                })

        # Calculate lane assignments for visualization
        # Main branch gets lane 0, other branches get assigned dynamically
        lanes = {}
        lane_usage = {}  # Track which commits are using which lane

        # First pass: assign main/master to lane 0
        main_branch = None
        for branch_name in branches:
            if branch_name in ['main', 'master']:
                main_branch = branch_name
                lanes[branch_name] = 0
                break

        # Assign other branches to lanes
        next_lane = 1
        for branch_name in branches:
            if branch_name not in lanes:
                lanes[branch_name] = next_lane
                next_lane += 1

        # For each commit, determine its lane based on first branch ref
        for commit in commits:
            if commit['branches']:
                # Use first branch as the commit's lane
                for branch in commit['branches']:
                    if branch in lanes:
                        commit['lane'] = lanes[branch]
                        commit['color'] = branches.get(branch, {}).get('color', '#6366f1')
                        break

            if 'lane' not in commit:
                # Commits not on a branch tip - try to find their branch
                # by looking at which branch head is an ancestor
                commit['lane'] = 0  # Default to main lane
                commit['color'] = '#6b7280'  # Gray for non-tip commits

        return {
            "branches": list(branches.values()),
            "commits": commits,
            "lanes": lanes,
            "main_branch": main_branch or "main"
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/checkout/{sha}")
def checkout_commit(sha: str, auto_stash: bool = True):
    """Checkout to a specific commit.

    This will checkout the working directory to the specified commit.
    If auto_stash is True (default), uncommitted changes will be automatically
    stashed before checkout and can be restored later.
    """
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    stashed = False

    try:
        # Check if there are uncommitted changes
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        has_changes = bool(status_result.stdout.strip())

        if has_changes:
            if auto_stash:
                # Auto-stash changes with a descriptive message
                stash_result = subprocess.run(
                    ["git", "stash", "push", "-m", f"Auto-stash before checkout to {sha[:7]}"],
                    cwd=git_root,
                    capture_output=True,
                    text=True
                )
                if stash_result.returncode == 0:
                    stashed = True
                else:
                    raise HTTPException(
                        status_code=400,
                        detail=f"Failed to stash changes: {stash_result.stderr}"
                    )
            else:
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
            # If checkout failed and we stashed, restore the stash
            if stashed:
                subprocess.run(["git", "stash", "pop"], cwd=git_root, capture_output=True)
            raise HTTPException(status_code=400, detail=f"Checkout failed: {result.stderr}")

        # Get current branch/commit info after checkout
        branch_result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        current_ref = branch_result.stdout.strip()

        message = f"Restored to version {sha[:7]}"
        if stashed:
            message += " (your work-in-progress was saved)"

        return {
            "success": True,
            "sha": sha,
            "current_ref": current_ref,
            "detached": current_ref == "HEAD",
            "stashed": stashed,
            "message": message
        }
    except HTTPException:
        raise
    except Exception as e:
        # If something failed and we stashed, try to restore
        if stashed:
            subprocess.run(["git", "stash", "pop"], cwd=git_root, capture_output=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/stash/pop")
def pop_stash():
    """Restore the most recent stashed changes.

    Use this to get back work-in-progress after restoring a version.
    """
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        # Check if there's anything in the stash
        list_result = subprocess.run(
            ["git", "stash", "list"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if not list_result.stdout.strip():
            return {"success": True, "message": "No saved work to restore"}

        # Pop the stash
        result = subprocess.run(
            ["git", "stash", "pop"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to restore work: {result.stderr}"
            )

        return {
            "success": True,
            "message": "Your work-in-progress has been restored"
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/local-claude/stash/list")
def list_stash():
    """List all stashed changes."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        result = subprocess.run(
            ["git", "stash", "list", "--pretty=format:%gd|%s|%ar"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        stashes = []
        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            parts = line.split('|', 2)
            if len(parts) >= 3:
                stashes.append({
                    "ref": parts[0],
                    "message": parts[1],
                    "relative_date": parts[2]
                })

        return {"stashes": stashes, "count": len(stashes)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
