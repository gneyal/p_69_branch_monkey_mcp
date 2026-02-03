"""
Dev server management endpoints for the local server.
"""

from fastapi import APIRouter

from ..dev_server import (
    DevServerRequest,
    start_dev_server_process,
    list_running_dev_servers,
    stop_dev_server,
)

router = APIRouter()


@router.post("/dev-server")
async def start_dev_server(request: DevServerRequest):
    """Start a dev server for a worktree."""
    run_id = request.run_id or str(request.task_number)
    return await start_dev_server_process(
        task_number=request.task_number,
        run_id=run_id,
        task_id=request.task_id,
        dev_script=request.dev_script,
        tunnel=request.tunnel or False,
        worktree_path=request.worktree_path
    )


@router.get("/dev-server")
def list_dev_servers():
    """List running dev servers."""
    return list_running_dev_servers()


@router.delete("/dev-server")
def stop_dev_server_endpoint(run_id: str):
    """Stop a dev server by run_id."""
    return stop_dev_server(run_id)
