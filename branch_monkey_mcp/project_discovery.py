"""
Project Discovery Module

Scans local filesystem for git repositories and provides
project discovery endpoints for the relay.
"""

import os
import subprocess
import socket
from pathlib import Path
from typing import List, Optional
from fastapi import APIRouter

router = APIRouter(prefix="/api/local-claude", tags=["project-discovery"])

# Cache for discovered projects
_discovered_projects_cache: List[dict] = []
_cache_timestamp: float = 0
CACHE_TTL_SECONDS = 300  # 5 minutes


def get_git_remote_url(repo_path: str) -> Optional[str]:
    """Get the remote origin URL for a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def get_git_branch(repo_path: str) -> Optional[str]:
    """Get the current branch name for a git repository."""
    try:
        result = subprocess.run(
            ["git", "-C", repo_path, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None


def discover_git_projects(search_paths: List[str] = None, max_depth: int = 3) -> List[dict]:
    """
    Discover git repositories in common development directories.

    Returns a list of dicts with:
    - name: Repository/folder name
    - path: Full path to the repository
    - remote_url: Git remote origin URL (if any)
    - branch: Current git branch
    - parent: Parent directory name (for grouping)
    """
    import time
    global _discovered_projects_cache, _cache_timestamp

    # Return cache if still valid
    if _discovered_projects_cache and (time.time() - _cache_timestamp) < CACHE_TTL_SECONDS:
        return _discovered_projects_cache

    if search_paths is None:
        home = Path.home()
        # Use realpath to resolve symlinks and case differences
        search_paths = []
        for p in [
            home / "Code",
            home / "code",
            home / "Projects",
            home / "projects",
            home / "Developer",
            home / "dev",
            home / "src",
            home / "repos",
            home / "workspace",
            home / "work",
        ]:
            if p.exists():
                real_path = str(p.resolve())
                if real_path not in search_paths:
                    search_paths.append(real_path)

    projects = []
    seen_paths = set()  # Use lowercase paths for case-insensitive deduplication (macOS)

    for search_path in search_paths:
        if not os.path.isdir(search_path):
            continue

        # Walk the directory tree up to max_depth
        for root, dirs, files in os.walk(search_path):
            # Calculate current depth
            depth = root.replace(search_path, "").count(os.sep)
            if depth >= max_depth:
                dirs[:] = []  # Don't recurse deeper
                continue

            # Skip hidden directories and common non-project dirs
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in [
                'node_modules', 'venv', '.venv', 'env', '__pycache__',
                'dist', 'build', 'target', '.git', 'vendor'
            ]]

            # Check if this is a git repository
            git_dir = os.path.join(root, '.git')
            if os.path.isdir(git_dir):
                # Don't recurse into git repos
                dirs[:] = []

                # Use lowercase path for deduplication (macOS is case-insensitive)
                real_root = os.path.realpath(root)
                path_key = real_root.lower()
                if path_key in seen_paths:
                    continue
                seen_paths.add(path_key)

                projects.append({
                    "name": os.path.basename(root),
                    "path": real_root,
                    "remote_url": get_git_remote_url(root),
                    "branch": get_git_branch(root),
                    "parent": os.path.basename(os.path.dirname(real_root))
                })

    # Sort by name
    projects.sort(key=lambda x: x["name"].lower())

    # Update cache
    _discovered_projects_cache = projects
    _cache_timestamp = time.time()

    return projects


def clear_cache():
    """Clear the discovered projects cache."""
    global _discovered_projects_cache, _cache_timestamp
    _discovered_projects_cache = []
    _cache_timestamp = 0


@router.get("/projects")
def list_local_projects(refresh: bool = False):
    """
    List git projects discovered on the local machine.

    Query params:
    - refresh: Force refresh the cache
    """
    if refresh:
        clear_cache()

    projects = discover_git_projects()
    return {
        "projects": projects,
        "count": len(projects),
        "machine_name": socket.gethostname()
    }


@router.get("/projects/search")
def search_local_projects(q: str, limit: int = 10):
    """
    Search discovered projects by name.

    Query params:
    - q: Search query (matches against project name)
    - limit: Max results to return (default 10)
    """
    projects = discover_git_projects()
    query = q.lower()

    # Filter and score matches
    matches = []
    for project in projects:
        name = project["name"].lower()
        if query in name:
            # Score: exact match > starts with > contains
            if name == query:
                score = 3
            elif name.startswith(query):
                score = 2
            else:
                score = 1
            matches.append((score, project))

    # Sort by score (descending) then name
    matches.sort(key=lambda x: (-x[0], x[1]["name"].lower()))

    return {
        "projects": [m[1] for m in matches[:limit]],
        "count": len(matches),
        "query": q
    }
