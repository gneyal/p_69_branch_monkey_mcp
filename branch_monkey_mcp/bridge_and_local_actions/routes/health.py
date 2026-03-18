"""
Health and status endpoints for the local server.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks
from fastapi.responses import FileResponse

from ..config import get_default_working_dir
from ..agent_manager import agent_manager

router = APIRouter()

# Git repo URL for self-update
_PACKAGE_GIT_URL = "git+https://github.com/gneyal/p_69_branch_monkey_mcp.git"


@router.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "kompany-relay"}


def _get_dashboard_response():
    """Return the dashboard HTML file response."""
    dashboard_path = Path(__file__).parent.parent.parent / "static" / "dashboard.html"
    return FileResponse(dashboard_path, media_type="text/html")


@router.get("/")
def serve_root():
    """Serve the dashboard at root."""
    return _get_dashboard_response()


@router.get("/dashboard")
def serve_dashboard():
    """Serve the dashboard HTML."""
    return _get_dashboard_response()


@router.get("/api/status")
def api_status():
    """Status endpoint for frontend compatibility."""
    return {
        "status": "ok",
        "service": "kompany-relay",
        "agents": len(agent_manager._agents),
        "mode": "local",
        "working_directory": get_default_working_dir()
    }


def _do_upgrade_and_restart():
    """Background task: upgrade the package from git then exit so launchd restarts."""
    import shutil
    import time

    time.sleep(1)  # Let the HTTP response flush

    # Find uv or pip for upgrading
    uv = shutil.which("uv")
    pip = shutil.which("pip3") or shutil.which("pip")

    if uv:
        # uv pip install --upgrade pulls latest from git
        cmd = [uv, "pip", "install", "--upgrade", _PACKAGE_GIT_URL]
    elif pip:
        cmd = [pip, "install", "--upgrade", _PACKAGE_GIT_URL]
    else:
        print("[Restart] Neither uv nor pip found, exiting without upgrade")
        os._exit(0)

    print(f"[Restart] Upgrading: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            print("[Restart] Upgrade successful, exiting for launchd restart...")
        else:
            print(f"[Restart] Upgrade failed (exit {result.returncode}): {result.stderr[:500]}")
            print("[Restart] Exiting anyway for restart...")
    except Exception as e:
        print(f"[Restart] Upgrade error: {e}")
        print("[Restart] Exiting anyway for restart...")

    # Exit the process — launchd KeepAlive will restart it
    os._exit(0)


@router.post("/api/restart")
def restart_relay(background_tasks: BackgroundTasks):
    """Upgrade relay from git and restart via launchd.

    Pulls the latest code, then exits. Launchd (KeepAlive=true)
    automatically restarts the process with the new version.
    """
    background_tasks.add_task(_do_upgrade_and_restart)
    return {
        "status": "restarting",
        "message": "Upgrading from git and restarting. Back in ~15 seconds."
    }
