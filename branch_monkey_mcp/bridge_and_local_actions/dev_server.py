"""
Development server management for the local server.

This module handles the lifecycle of development servers, including
starting, stopping, and tracking running servers.
"""

import asyncio
import os
import signal
import socket
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

from pydantic import BaseModel

from .database import (
    init_dev_servers_db,
    save_dev_server_to_db,
    delete_dev_server_from_db,
    load_dev_servers_from_db,
    _is_port_in_use,
)
from .dev_proxy import start_dev_proxy, set_proxy_target, get_proxy_status, _proxy_state
from .worktree import find_worktree_path


# Optional ngrok support for remote dev server access
try:
    from pyngrok import ngrok
    NGROK_AVAILABLE = True
except ImportError:
    NGROK_AVAILABLE = False
    ngrok = None


# Track running dev servers by run_id (or task_number as fallback)
_running_dev_servers: Dict[str, dict] = {}
BASE_DEV_PORT = 6000

# Track ngrok tunnels by run_id
_ngrok_tunnels: Dict[str, object] = {}


class DevServerRequest(BaseModel):
    """Request to start a dev server."""
    task_id: Optional[str] = None
    task_number: int
    run_id: Optional[str] = None  # Run/session ID - a task can have multiple runs
    dev_script: Optional[str] = None  # Custom script, e.g. "cd frontend && npm run dev --port {port}"
    tunnel: Optional[bool] = False  # Create ngrok tunnel for remote access
    worktree_path: Optional[str] = None  # Explicit worktree path (for cross-repo scenarios)
    project_path: Optional[str] = None  # Project's local path (to find worktree in correct repo)


def start_ngrok_tunnel(port: int, run_id: str) -> Optional[str]:
    """Start an ngrok tunnel for the given port. Returns public URL or None."""
    if not NGROK_AVAILABLE:
        print("[Ngrok] pyngrok not installed - tunnel not available")
        return None

    try:
        # Check if ngrok authtoken is configured
        # Users should run: ngrok config add-authtoken <token>
        tunnel = ngrok.connect(port, "http")
        public_url = tunnel.public_url
        _ngrok_tunnels[run_id] = tunnel
        print(f"[Ngrok] Tunnel started for port {port}: {public_url}")
        return public_url
    except Exception as e:
        print(f"[Ngrok] Failed to start tunnel: {e}")
        return None


def stop_ngrok_tunnel(run_id: str):
    """Stop ngrok tunnel for the given run_id."""
    if run_id in _ngrok_tunnels:
        try:
            tunnel = _ngrok_tunnels[run_id]
            ngrok.disconnect(tunnel.public_url)
            del _ngrok_tunnels[run_id]
            print(f"[Ngrok] Tunnel stopped for run {run_id}")
        except Exception as e:
            print(f"[Ngrok] Failed to stop tunnel: {e}")


def is_port_in_use(port: int) -> bool:
    """Check if a port is in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0


def find_available_port(base_port: int) -> int:
    """Find an available port starting from base."""
    port = base_port
    while is_port_in_use(port):
        port += 1
        if port > base_port + 100:
            raise Exception("No available ports found")
    return port


async def start_dev_server_process(
    task_number: int,
    run_id: str,
    task_id: Optional[str] = None,
    dev_script: Optional[str] = None,
    tunnel: bool = False,
    worktree_path: Optional[str] = None,
    project_path: Optional[str] = None
) -> dict:
    """Start a dev server for a worktree.

    Returns:
        Dict with port, url, proxyUrl, tunnelUrl, runId, status
    """
    from fastapi import HTTPException

    # Ensure proxy is running
    if not _proxy_state["running"]:
        start_dev_proxy()

    # Check if already running for this run
    if run_id in _running_dev_servers:
        info = _running_dev_servers[run_id]

        # Verify the process is actually still running by checking the port
        if _is_port_in_use(info["port"]):
            # Update proxy to point to this server
            set_proxy_target(info["port"], run_id)
            proxy_status = get_proxy_status()

            # Create tunnel if requested and not already created
            tunnel_url = info.get("tunnel_url")
            if tunnel and not tunnel_url:
                tunnel_url = start_ngrok_tunnel(info["port"], run_id)
                if tunnel_url:
                    info["tunnel_url"] = tunnel_url

            return {
                "port": info["port"],
                "url": f"http://localhost:{info['port']}",
                "proxyUrl": proxy_status["proxyUrl"],
                "tunnelUrl": tunnel_url,
                "runId": run_id,
                "status": "already_running"
            }
        else:
            # Process died, clean up stale entry and start fresh
            print(f"[DevServer] Cleaning up stale entry for {run_id} (port {info['port']} not in use)")
            delete_dev_server_from_db(run_id)
            del _running_dev_servers[run_id]

    # Find worktree - use provided path if available, otherwise search in project
    if worktree_path:
        # Validate the provided path exists
        if not Path(worktree_path).exists():
            raise HTTPException(status_code=404, detail=f"Provided worktree path does not exist: {worktree_path}")
        print(f"[DevServer] Using provided worktree path: {worktree_path}")
    else:
        worktree_path = find_worktree_path(task_number, project_path)
        if not worktree_path:
            detail = f"No worktree found for task {task_number}"
            if project_path:
                detail += f" in {project_path}"
            raise HTTPException(status_code=404, detail=detail)

    # Find available port
    port = find_available_port(BASE_DEV_PORT + task_number)

    # Use custom dev_script if provided, otherwise use default
    if dev_script:
        # Replace {port} placeholder
        command = dev_script.replace("{port}", str(port))
        print(f"[DevServer] Running custom script for run {run_id} (task {task_number}): {command}")

        process = subprocess.Popen(
            command,
            shell=True,
            cwd=str(worktree_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True
        )
    else:
        # Default behavior: run npm run dev in frontend directory
        frontend_path = Path(worktree_path) / "frontend"
        if not frontend_path.exists():
            raise HTTPException(status_code=404, detail="No frontend directory in worktree. Configure a custom dev script in project settings.")

        # Check if node_modules exists, if not install
        node_modules = frontend_path / "node_modules"
        if not node_modules.exists():
            print(f"[DevServer] Installing dependencies for task {task_number}...")
            try:
                subprocess.run(
                    ["npm", "install"],
                    cwd=str(frontend_path),
                    capture_output=True,
                    timeout=180,
                    check=True
                )
            except subprocess.TimeoutExpired:
                raise HTTPException(status_code=500, detail="npm install timed out")
            except subprocess.CalledProcessError as e:
                raise HTTPException(status_code=500, detail=f"npm install failed: {e.stderr}")

        print(f"[DevServer] Starting dev server for run {run_id} (task {task_number}) on port {port}")
        process = subprocess.Popen(
            ["npm", "run", "dev", "--", "--port", str(port)],
            cwd=str(frontend_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True
        )

    # Track it by run_id
    _running_dev_servers[run_id] = {
        "process": process,
        "port": port,
        "task_id": task_id,
        "task_number": task_number,
        "run_id": run_id,
        "worktree_path": str(worktree_path),
        "started_at": datetime.now().isoformat(),
        "tunnel_url": None
    }

    # Persist to database for recovery after restart
    save_dev_server_to_db(run_id, _running_dev_servers[run_id])

    # Wait for server to start
    await asyncio.sleep(3)

    # Create ngrok tunnel if requested
    tunnel_url = None
    if tunnel:
        tunnel_url = start_ngrok_tunnel(port, run_id)
        if tunnel_url:
            _running_dev_servers[run_id]["tunnel_url"] = tunnel_url

    # Set proxy target to this server
    set_proxy_target(port, run_id)
    proxy_status = get_proxy_status()

    return {
        "port": port,
        "url": f"http://localhost:{port}",
        "proxyUrl": proxy_status["proxyUrl"],
        "tunnelUrl": tunnel_url,
        "runId": run_id,
        "status": "started"
    }


def list_running_dev_servers() -> dict:
    """List running dev servers."""
    servers = []
    proxy_status = get_proxy_status()
    for run_id, info in _running_dev_servers.items():
        is_active = proxy_status["targetRunId"] == run_id
        servers.append({
            "runId": run_id,
            "taskNumber": info.get("task_number"),
            "port": info["port"],
            "url": f"http://localhost:{info['port']}",
            "proxyUrl": proxy_status["proxyUrl"] if is_active else None,
            "tunnelUrl": info.get("tunnel_url"),
            "isActive": is_active,
            "startedAt": info["started_at"],
            "worktreePath": info.get("worktree_path")
        })
    return {"servers": servers, "proxy": proxy_status}


def stop_dev_server(run_id: str) -> dict:
    """Stop a dev server by run_id."""
    from fastapi import HTTPException

    if run_id not in _running_dev_servers:
        raise HTTPException(status_code=404, detail="Server not found")

    info = _running_dev_servers[run_id]

    # Stop ngrok tunnel if exists
    stop_ngrok_tunnel(run_id)

    try:
        os.killpg(os.getpgid(info["process"].pid), signal.SIGTERM)
    except Exception:
        try:
            info["process"].kill()
        except Exception:
            pass

    # If this was the active proxy target, clear it
    if _proxy_state["target_run_id"] == run_id:
        _proxy_state["target_port"] = None
        _proxy_state["target_run_id"] = None

    # Remove from database
    delete_dev_server_from_db(run_id)

    del _running_dev_servers[run_id]
    return {"status": "stopped", "runId": run_id}


def get_running_dev_servers() -> Dict[str, dict]:
    """Get the running dev servers dictionary."""
    return _running_dev_servers


# Initialize database and load existing servers on module import
init_dev_servers_db()
load_dev_servers_from_db(_running_dev_servers)
