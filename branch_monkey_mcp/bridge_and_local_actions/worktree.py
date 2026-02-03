"""
Git worktree management for the local server.

This module handles creation, deletion, and discovery of git worktrees
used for isolated task execution.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import get_default_working_dir
from .git_utils import get_git_root, branch_exists


def create_worktree(repo_dir: str, branch: str, task_number: int, run_id: str) -> dict:
    """Create a git worktree for a task run."""
    try:
        git_root = get_git_root(repo_dir) or repo_dir
        worktrees_dir = Path(git_root) / ".worktrees"
        worktree_path = worktrees_dir / f"task-{task_number}-{run_id}"

        print(f"[Worktree] Creating: {worktree_path} -> {branch}")

        worktrees_dir.mkdir(parents=True, exist_ok=True)

        branch_created = False
        if not branch_exists(git_root, branch):
            print(f"[Worktree] Creating branch: {branch}")
            result = subprocess.run(
                ["git", "branch", branch],
                cwd=git_root,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                raise Exception(f"Failed to create branch: {result.stderr}")
            branch_created = True

        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            raise Exception(f"Failed to create worktree: {result.stderr}")

        # Create .claude/settings.local.json for auto-accept permissions
        claude_dir = worktree_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file = claude_dir / "settings.local.json"
        settings = {
            "permissions": {
                "allow": [
                    "Edit", "Write",
                    "Bash(git:*)", "Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)",
                    "Bash(mkdir:*)", "Bash(rm:*)", "Bash(cp:*)", "Bash(mv:*)",
                    "Bash(cat:*)", "Bash(ls:*)", "Bash(pwd:*)", "Bash(cd:*)",
                    "Bash(echo:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(head:*)",
                    "Bash(tail:*)", "Bash(wc:*)", "Bash(sort:*)", "Bash(sed:*)",
                    "Bash(awk:*)", "Bash(curl:*)", "Bash(python:*)", "Bash(python3:*)",
                    "Bash(pip:*)", "Bash(pip3:*)", "Bash(uv:*)", "Bash(cargo:*)",
                    "Bash(go:*)", "Bash(make:*)", "Bash(gh:*)"
                ],
                "deny": []
            },
            "trust": [str(worktree_path), str(git_root)]
        }
        with open(settings_file, "w") as f:
            json.dump(settings, f, indent=2)

        # Copy .env files from main repo to worktree (they're gitignored)
        copied_envs = []
        for env_pattern in [".env", ".env.local", ".env.development", ".env.development.local"]:
            # Check common locations: root, frontend/, src/
            for subdir in ["", "frontend", "src"]:
                src_env = Path(git_root) / subdir / env_pattern if subdir else Path(git_root) / env_pattern
                if src_env.exists():
                    dst_env = worktree_path / subdir / env_pattern if subdir else worktree_path / env_pattern
                    dst_env.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_env, dst_env)
                    copied_envs.append(str(dst_env.relative_to(worktree_path)))

        if copied_envs:
            print(f"[Worktree] Copied env files: {copied_envs}")

        return {
            "success": True,
            "worktree_path": str(worktree_path),
            "message": f"Created worktree with {'new' if branch_created else 'existing'} branch: {branch}",
            "branch_created": branch_created,
            "copied_env_files": copied_envs
        }

    except Exception as e:
        print(f"[Worktree] Error: {e}")
        return {
            "success": False,
            "worktree_path": "",
            "message": str(e),
            "branch_created": False
        }


def remove_worktree(repo_dir: str, worktree_path: str) -> None:
    """Remove a git worktree."""
    try:
        git_root = get_git_root(repo_dir) or repo_dir
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=git_root,
            capture_output=True,
            check=True
        )
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=git_root,
            capture_output=True
        )
    except Exception:
        shutil.rmtree(worktree_path, ignore_errors=True)


def find_worktree_path(task_number: int) -> Optional[str]:
    """Find the worktree path for a task number.

    Uses trailing dash in prefix matching to avoid false matches
    (e.g., task-29 should not match task-290).
    """
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        return None

    worktrees_dir = Path(git_root) / ".worktrees"
    if not worktrees_dir.exists():
        return None

    # Find most recent worktree for this task
    # Use "task-{number}-" prefix to avoid matching wrong tasks (task-29 != task-290)
    matching_dirs = []
    prefix = f"task-{task_number}-"
    for d in worktrees_dir.iterdir():
        if d.is_dir() and d.name.startswith(prefix):
            matching_dirs.append(d)

    if not matching_dirs:
        return None

    # Return the most recently modified one
    matching_dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return str(matching_dirs[0])


def find_actual_branch(task_number: int) -> Optional[str]:
    """Find the actual branch for a task by checking the worktree or git branches.

    This handles cases where the stored branch name doesn't match the actual branch
    (e.g., branch was created with a different naming convention).
    """
    # First, try to get branch from worktree
    worktree_path = find_worktree_path(task_number)
    if worktree_path:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                branch_name = result.stdout.strip()
                if branch_name and branch_name != "HEAD":
                    return branch_name
        except Exception:
            pass

    # Fallback: search git branches by pattern
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if git_root:
        try:
            result = subprocess.run(
                ["git", "branch", "-a", "--list", f"*task/{task_number}-*"],
                cwd=git_root,
                capture_output=True,
                text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n")]
                local_branch = next((b for b in lines if not b.startswith("remotes/")), None)
                if local_branch:
                    return local_branch
        except Exception:
            pass

    return None
