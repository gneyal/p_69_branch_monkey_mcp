"""
Configuration and state management for the local server.

This module manages:
- Home directory (base directory passed to relay, never changes)
- Default working directory (current project directory)
- Relay connection status
- App configuration caching
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

import httpx


# =============================================================================
# Home Directory (base directory passed to relay, never changes)
# =============================================================================

_home_directory: Optional[str] = os.environ.get("BRANCH_MONKEY_WORKING_DIR")


def set_home_directory(directory: str) -> None:
    """Set the home directory (only called once at startup)."""
    global _home_directory
    _home_directory = directory


def get_home_directory() -> str:
    """Get the home directory (the base directory passed to the relay)."""
    return _home_directory or os.getcwd()


# =============================================================================
# Current Project Directory (can be changed to a specific project within home)
# =============================================================================

_default_working_dir: Optional[str] = os.environ.get("BRANCH_MONKEY_WORKING_DIR")


def set_default_working_dir(directory: str) -> None:
    """Set the default working directory for agent execution."""
    global _default_working_dir
    _default_working_dir = directory
    print(f"[Server] Default working directory: {directory}")


def get_default_working_dir() -> str:
    """Get the default working directory (falls back to cwd)."""
    return _default_working_dir or os.getcwd()


# Initialize from environment on startup
if _home_directory:
    print(f"[Server] Default working directory: {_home_directory}")


# =============================================================================
# Project Detection
# =============================================================================

# Subdirectories to check for package.json (in priority order)
_APP_SUBDIRS = ["", "frontend", "app", "client", "web", "packages/web", "packages/app"]


def find_dev_dir(project_path: str) -> Tuple[str, Optional[dict]]:
    """Find the subdirectory containing dev scripts in a project.

    Scans project_path and common subdirectories for a package.json with
    a "dev" script. Prioritises "dev" over "start"-only matches so that
    `npm run dev` works in the returned directory.

    Returns:
        (run_dir, package_json) — run_dir is always a valid absolute path.
    """
    dev_match = None       # Has scripts.dev — strongest
    start_match = None     # Has scripts.start but not scripts.dev
    any_match = None       # Has package.json but no dev/start

    for subdir in _APP_SUBDIRS:
        candidate = Path(project_path) / subdir / "package.json" if subdir else Path(project_path) / "package.json"
        if not candidate.exists():
            continue
        try:
            pj = json.loads(candidate.read_text())
        except Exception:
            continue

        scripts = pj.get("scripts", {})
        d = str(candidate.parent)

        if scripts.get("dev"):
            dev_match = (d, pj)
            break  # Best possible match
        elif scripts.get("start") and start_match is None:
            start_match = (d, pj)
        elif any_match is None:
            any_match = (d, pj)

    return dev_match or start_match or any_match or (project_path, None)


# =============================================================================
# Relay Status Tracking
# =============================================================================

_relay_status = {
    "connected": False,
    "machine_id": None,
    "machine_name": None,
    "cloud_url": None,
    "last_heartbeat": None,
    "connected_at": None,
}


def update_relay_status(
    connected: bool,
    machine_id: str = None,
    machine_name: str = None,
    cloud_url: str = None
) -> None:
    """Update relay connection status."""
    global _relay_status
    _relay_status["connected"] = connected
    if connected:
        _relay_status["machine_id"] = machine_id
        _relay_status["machine_name"] = machine_name
        _relay_status["cloud_url"] = cloud_url
        _relay_status["last_heartbeat"] = datetime.utcnow().isoformat()
        if not _relay_status["connected_at"]:
            _relay_status["connected_at"] = datetime.utcnow().isoformat()
    else:
        _relay_status["connected_at"] = None


def get_relay_status() -> dict:
    """Get current relay status."""
    return _relay_status.copy()


# =============================================================================
# App Configuration Caching
# =============================================================================

# Default app config values
_DEFAULT_APP_CONFIG = {
    "appName": "branch/main",
    "appNameDisplay": "Branch Monkey",
    "appNameTitle": "Branch Monkey",
    "appMcpNameTitle": "Branch Monkey",
    "appDomain": None
}

# Cached app config
_cached_app_config = None
_app_config_fetched_at = None


async def get_app_config() -> dict:
    """Get app configuration, proxied from cloud with caching."""
    global _cached_app_config, _app_config_fetched_at

    # Use cache if fresh (within 5 minutes)
    if _cached_app_config and _app_config_fetched_at:
        age_seconds = (datetime.utcnow() - _app_config_fetched_at).total_seconds()
        if age_seconds < 300:
            return _cached_app_config

    # Try to fetch from cloud
    relay_status = get_relay_status()
    cloud_url = relay_status.get("cloud_url") or "https://kompany.dev"

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(f"{cloud_url}/api/config")
            if res.status_code == 200:
                data = res.json()
                _cached_app_config = {
                    "appName": data.get("appName", _DEFAULT_APP_CONFIG["appName"]),
                    "appNameDisplay": data.get("appNameDisplay", _DEFAULT_APP_CONFIG["appNameDisplay"]),
                    "appNameTitle": data.get("appNameTitle", _DEFAULT_APP_CONFIG["appNameTitle"]),
                    "appMcpNameTitle": data.get("appMcpNameTitle", _DEFAULT_APP_CONFIG["appMcpNameTitle"]),
                    "appDomain": data.get("appDomain", _DEFAULT_APP_CONFIG["appDomain"])
                }
                _app_config_fetched_at = datetime.utcnow()
                return _cached_app_config
    except Exception as e:
        print(f"[Config] Failed to fetch from cloud: {e}")

    # Return cached or default
    return _cached_app_config or _DEFAULT_APP_CONFIG
