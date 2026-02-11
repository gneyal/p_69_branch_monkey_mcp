"""
Git status and commit endpoints for the local server.
"""

import os
import subprocess
import time
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
def get_branch_graph(limit: int = 50, path: Optional[str] = None):
    """Get commits with branch graph data for visualization.

    Returns commits with parent relationships and branch info for
    rendering a visual branch graph like GitHub/GitKraken.

    Args:
        limit: Maximum number of commits to return
        path: Optional directory path to use. Defaults to working directory.
    """
    work_dir = path or get_default_working_dir()
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

        # Filter out branches already merged into main/master
        main_ref = "main" if "main" in branches else ("master" if "master" in branches else None)
        merged_branches = set()
        if main_ref:
            merged_result = subprocess.run(
                ["git", "branch", "-a", "--merged", main_ref, "--format=%(refname:short)"],
                cwd=git_root,
                capture_output=True,
                text=True
            )
            if merged_result.returncode == 0:
                for line in merged_result.stdout.strip().split('\n'):
                    name = line.strip()
                    if name and name not in (main_ref, f"origin/{main_ref}", "origin/HEAD"):
                        if name.startswith('origin/'):
                            merged_branches.add(name.replace('origin/', '', 1))
                        merged_branches.add(name)

            # Remove merged branches from the dict
            for name in list(branches.keys()):
                if name in merged_branches or name.replace('origin/', '', 1) in merged_branches:
                    if name not in ('main', 'master'):
                        del branches[name]

        # Also filter stale task branches (catches squash-merged branches).
        # Get committer date for each branch tip in a single git call.
        STALE_DAYS = 14
        stale_cutoff = int(time.time()) - (STALE_DAYS * 86400)

        dates_result = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname:short)|%(committerdate:unix)",
             "refs/heads/", "refs/remotes/"],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        branch_dates = {}
        for line in dates_result.stdout.strip().split('\n'):
            if '|' in line:
                parts = line.split('|', 1)
                try:
                    branch_dates[parts[0]] = int(parts[1])
                except (ValueError, IndexError):
                    pass

        for name in list(branches.keys()):
            if name in ('main', 'master'):
                continue
            # Only apply staleness filter to task branches
            if not (name.startswith('task/') or name.startswith('task-')):
                continue
            commit_date = branch_dates.get(name, 0)
            if commit_date < stale_cutoff:
                merged_branches.add(name)
                del branches[name]

        # Get commits only for the branches we kept (not --all)
        log_cmd = ["git", "log", f"-{limit}", "--pretty=format:%H|%P|%D|%s|%an|%ar|%ai", "--topo-order"]
        for b in branches:
            log_cmd.append(b)
        commits_result = subprocess.run(
            log_cmd,
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

                # Filter out merged branches from commit refs
                branch_refs = [b for b in branch_refs if b not in merged_branches]

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

        # Get remote URL for repo identification
        remote_url = None
        repo_name = os.path.basename(git_root)
        try:
            remote_result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=git_root,
                capture_output=True,
                text=True
            )
            if remote_result.returncode == 0:
                remote_url = remote_result.stdout.strip()
                # Extract repo name from URL (e.g. "org/repo" from github.com/org/repo.git)
                if remote_url:
                    clean = remote_url.rstrip('/').removesuffix('.git')
                    parts = clean.split('/')
                    if len(parts) >= 2:
                        repo_name = '/'.join(parts[-2:])
        except Exception:
            pass

        return {
            "branches": list(branches.values()),
            "commits": commits,
            "lanes": lanes,
            "main_branch": main_branch or "main",
            "repo_name": repo_name,
            "remote_url": remote_url,
            "git_root": git_root
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/local-claude/checkout/{sha}")
def checkout_commit(sha: str, auto_stash: bool = True, path: Optional[str] = None):
    """Checkout to a specific commit.

    This will checkout the working directory to the specified commit.
    If auto_stash is True (default), uncommitted changes will be automatically
    stashed before checkout and can be restored later.

    Args:
        sha: The commit hash to checkout
        auto_stash: Whether to auto-stash uncommitted changes (default True)
        path: Optional directory path to use. Defaults to working directory.
    """
    work_dir = path or get_default_working_dir()
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
