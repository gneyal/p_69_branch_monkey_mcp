"""
Dev server management endpoints for the local server.
"""

from fastapi import APIRouter

from ..dev_server_manager import DevServerRequest, manager

router = APIRouter()


@router.post("/dev-server")
async def start_dev_server(request: DevServerRequest):
    """Start a dev server for a worktree."""
    run_id = request.run_id or str(request.task_number)
    return await manager.start(
        task_number=request.task_number,
        run_id=run_id,
        task_id=request.task_id,
        dev_script=request.dev_script,
        tunnel=request.tunnel or False,
        worktree_path=request.worktree_path,
        project_path=request.project_path,
    )


@router.get("/dev-server")
def list_dev_servers():
    """List running dev servers."""
    return manager.list()


@router.delete("/dev-server")
def stop_dev_server_endpoint(run_id: str):
    """Stop a dev server by run_id."""
    return manager.stop(run_id)
