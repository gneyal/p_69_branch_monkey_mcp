"""
Agent management endpoints for the local server.
"""

import asyncio
import base64
import json
import os
import shutil
import tempfile
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from ..config import get_default_working_dir
from ..agent_manager import agent_manager

router = APIRouter()


class CreateAgentRequest(BaseModel):
    task_id: Optional[str] = None
    task_number: Optional[int] = None
    title: str = "Local Task"
    description: Optional[str] = None
    working_dir: Optional[str] = None
    prompt: Optional[str] = None
    skip_branch: bool = False


class TaskExecuteRequest(BaseModel):
    """Request from relay to execute a task in a specific local_path."""
    task_id: str
    task_number: int
    title: str
    description: Optional[str] = None
    local_path: Optional[str] = None
    repository_url: Optional[str] = None


class ImageData(BaseModel):
    data: str  # base64 data URL (data:image/png;base64,...)
    name: str = "image"
    type: str = "image/png"


class InputRequest(BaseModel):
    input: str
    images: Optional[List[ImageData]] = None


def save_images_to_temp(images: List[ImageData]) -> List[str]:
    """Save base64 images to temporary files and return file paths."""
    temp_paths = []
    for i, img in enumerate(images):
        try:
            # Parse data URL: data:image/png;base64,xxxxx
            if img.data.startswith('data:'):
                # Extract the base64 part after the comma
                header, b64_data = img.data.split(',', 1)
                # Get extension from content type
                content_type = header.split(';')[0].split(':')[1]
                ext = content_type.split('/')[-1]
                if ext == 'jpeg':
                    ext = 'jpg'
            else:
                # Assume raw base64
                b64_data = img.data
                ext = 'png'

            # Decode and save to temp file
            image_bytes = base64.b64decode(b64_data)
            fd, temp_path = tempfile.mkstemp(suffix=f'.{ext}', prefix='claude_img_')
            os.write(fd, image_bytes)
            os.close(fd)
            temp_paths.append(temp_path)
            print(f"[LocalServer] Saved image {i+1} to {temp_path}")
        except Exception as e:
            print(f"[LocalServer] Failed to save image {i+1}: {e}")
    return temp_paths


@router.post("/task-execute")
async def execute_task(request: TaskExecuteRequest):
    """
    Execute a task dispatched from the cloud.
    This endpoint is called by the relay when a user triggers a task run.
    The task is executed in the specified local_path.
    """
    # Use local_path if provided, otherwise fall back to default working dir
    print(f"[TaskExecute] Received local_path from request: {request.local_path}")
    working_dir = request.local_path or get_default_working_dir()
    print(f"[TaskExecute] Resolved working_dir: {working_dir}")

    # Verify the directory exists
    if working_dir and not os.path.isdir(working_dir):
        raise HTTPException(
            status_code=400,
            detail=f"Working directory does not exist: {working_dir}"
        )

    print(f"[TaskExecute] Starting task #{request.task_number}: {request.title}")
    print(f"[TaskExecute] Working directory: {working_dir}")

    # Build the prompt with task info
    prompt = f"Work on task #{request.task_number}: {request.title}"
    if request.description:
        prompt += f"\n\n{request.description}"

    # Create and start the agent
    result = await agent_manager.create(
        task_id=request.task_id,
        task_number=request.task_number,
        task_title=request.title,
        task_description=request.description,
        working_dir=working_dir,
        prompt=prompt
    )

    return {
        "success": True,
        "agent_id": result.get("id"),
        "task_number": request.task_number,
        "working_dir": working_dir,
        "branch": result.get("branch"),
        "worktree_path": result.get("worktree_path"),
        "is_worktree": result.get("is_worktree", False),
        "message": f"Task #{request.task_number} started in {working_dir}"
    }


@router.post("/agents")
async def create_agent(request: CreateAgentRequest):
    """Create and start a new local Claude Code agent."""
    return await agent_manager.create(
        task_id=request.task_id,
        task_number=request.task_number,
        task_title=request.title,
        task_description=request.description,
        working_dir=request.working_dir,
        prompt=request.prompt,
        skip_branch=request.skip_branch
    )


@router.get("/agents")
def list_agents():
    """List all local agents."""
    return agent_manager.list()


@router.get("/agents/{agent_id}")
def get_agent(agent_id: str):
    """Get agent info by ID."""
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@router.post("/agents/{agent_id}/input")
async def send_input(agent_id: str, request: InputRequest):
    """Send input to agent (resumes session if paused)."""
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    message = request.input.rstrip('\n')

    # Handle images: save to temp files
    image_paths = []
    if request.images:
        image_paths = save_images_to_temp(request.images)
        if image_paths:
            # Prepend image paths to message - Claude can read image files directly
            image_refs = "\n".join([f"Please read and analyze this image file: {path}" for path in image_paths])
            message = f"{image_refs}\n\n{message}" if message else image_refs
            print(f"[LocalServer] Added {len(image_paths)} image references to message")

    if agent["status"] in ("paused", "completed", "failed") and agent.get("session_id"):
        await agent_manager.resume_session(agent_id, message, image_paths)
        return {"success": True, "action": "resumed", "images": len(image_paths)}

    if agent["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is running. Wait for it to complete before sending another message."
        )

    raise HTTPException(
        status_code=400,
        detail="No active session. Start a new task."
    )


@router.delete("/agents/{agent_id}")
def kill_agent(agent_id: str, cleanup_worktree: bool = False):
    """Kill an agent."""
    agent_manager.kill(agent_id, cleanup_worktree)
    return {"success": True}


@router.post("/agents/cleanup")
def cleanup_agents():
    """Clean up all stale/completed agents."""
    cleaned = agent_manager.cleanup_stale_agents()
    return {"success": True, "cleaned": cleaned, "remaining": len(agent_manager._agents)}


@router.delete("/agents")
def kill_all_agents(cleanup_worktrees: bool = False):
    """Kill all agents."""
    agent_ids = list(agent_manager._agents.keys())
    for agent_id in agent_ids:
        agent_manager.kill(agent_id, cleanup_worktrees)
    return {"success": True, "killed": len(agent_ids)}


@router.get("/agents/{agent_id}/output")
def get_output(agent_id: str):
    """Get full output buffer."""
    output = agent_manager.get_output(agent_id)
    return {"output": output}


@router.get("/agents/{agent_id}/stream")
async def stream_output(agent_id: str, request: Request):
    """Stream agent output via Server-Sent Events."""
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    queue = agent_manager.add_listener(agent_id)

    async def event_generator():
        try:
            init_event = {"type": "connected", "agentId": agent_id, "status": agent['status']}
            yield f"data: {json.dumps(init_event)}\n\n"

            # Send worktree/branch info so frontend can update the UI
            worktree_event = {
                "type": "worktree_info",
                "branch": agent.get('branch'),
                "worktree_path": agent.get('worktree_path'),
                "is_worktree": agent.get('worktree_path') is not None,
                "work_dir": agent.get('work_dir')
            }
            yield f"data: {json.dumps(worktree_event)}\n\n"

            # If agent is already paused/completed, send that status immediately
            if agent['status'] in ('paused', 'completed', 'failed'):
                paused_event = {
                    "type": "paused",
                    "exit_code": agent.get('exit_code'),
                    "session_id": agent.get('session_id'),
                    "can_resume": agent.get('can_resume', True)
                }
                yield f"data: {json.dumps(paused_event)}\n\n"

            while True:
                if await request.is_disconnected():
                    break

                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(event)}\n\n"

                    if event.get("type") == "exit":
                        break

                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"

        finally:
            agent_manager.remove_listener(agent_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "Access-Control-Allow-Origin": "*",
        }
    )


@router.get("/check")
def check_claude_installed():
    """Check if Claude Code CLI is installed locally."""
    claude_path = shutil.which("claude")
    return {
        "installed": claude_path is not None,
        "path": claude_path
    }


@router.get("/stats")
def get_machine_stats():
    """
    Get combined machine stats in a single request.
    Returns agents, worktrees count, and working directory info.
    This avoids multiple relay round-trips.
    """
    from .worktrees import list_worktrees
    from .config_routes import get_working_directory

    # Get agents (fast - in memory)
    agents = agent_manager.list()

    # Get worktrees (may be slower due to git commands)
    try:
        wt_result = list_worktrees()
        worktrees = wt_result.get("worktrees", [])
    except Exception as e:
        print(f"[Stats] Failed to get worktrees: {e}")
        worktrees = []

    # Get working directory config
    try:
        config = get_working_directory()
        home_dir = config.get("home_directory")
    except Exception as e:
        print(f"[Stats] Failed to get working dir: {e}")
        home_dir = None

    return {
        "agents": agents,
        "worktrees": worktrees,
        "home_directory": home_dir
    }
