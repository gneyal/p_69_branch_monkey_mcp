"""
Dev proxy management endpoints for the local server.
"""

from fastapi import APIRouter, HTTPException

from ..dev_proxy import (
    start_dev_proxy,
    stop_dev_proxy,
    set_proxy_target,
    get_proxy_status,
    set_proxy_port,
    _proxy_state,
)
from ..dev_server_manager import manager

router = APIRouter()


@router.get("/dev-proxy")
def get_dev_proxy():
    """Get current dev proxy status."""
    return get_proxy_status()


@router.post("/dev-proxy")
def set_dev_proxy_target(run_id: str):
    """Set proxy target to a specific running dev server by run_id."""
    running_dev_servers = manager.get_servers()
    if run_id not in running_dev_servers:
        raise HTTPException(status_code=404, detail=f"No dev server running for run {run_id}")

    # Ensure proxy is running
    if not _proxy_state["running"]:
        start_dev_proxy()

    info = running_dev_servers[run_id]
    set_proxy_target(info["port"], run_id)
    return get_proxy_status()


@router.put("/dev-proxy/port")
def set_dev_proxy_port_endpoint(port: int):
    """Set the proxy port. Restarts proxy if already running."""
    if port < 1024 or port > 65535:
        raise HTTPException(status_code=400, detail="Port must be between 1024 and 65535")

    old_port = _proxy_state["proxy_port"]

    success = set_proxy_port(port)
    if not success:
        raise HTTPException(status_code=500, detail=f"Port {port} is not available, reverted to {old_port}")

    return {
        "success": True,
        "oldPort": old_port,
        "newPort": port,
        "status": get_proxy_status()
    }


@router.delete("/dev-proxy")
def stop_dev_proxy_endpoint():
    """Stop the dev proxy server."""
    stop_dev_proxy()
    return {"success": True, "status": get_proxy_status()}
