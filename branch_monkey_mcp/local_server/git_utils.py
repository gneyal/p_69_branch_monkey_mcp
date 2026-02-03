"""
Git utility functions for the local server.

This module provides basic git operations used throughout the local server.
"""

import re
import subprocess
from typing import Optional


def is_git_repo(directory: str) -> bool:
    """Check if directory is a git repository."""
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=directory,
            capture_output=True,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def get_git_root(directory: str) -> Optional[str]:
    """Get the root directory of a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def get_current_branch(directory: str) -> Optional[str]:
    """Get the current git branch."""
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=directory,
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def branch_exists(directory: str, branch: str) -> bool:
    """Check if a branch exists."""
    try:
        subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=directory,
            capture_output=True,
            check=True
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def generate_branch_name(task_number: int, title: str, run_id: str = "") -> str:
    """Generate a git branch name from task number, title, and run ID."""
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    slug = slug[:30].rstrip('-')

    if run_id:
        return f"task/{task_number}-{slug}-{run_id[:6]}"
    return f"task/{task_number}-{slug}"
