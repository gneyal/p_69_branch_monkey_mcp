"""
Agent management endpoints for the local server.
"""

import asyncio
import base64
import json
import os
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
    workflow: str = "execute"
    skip_branch: bool = False  # Legacy: prefer workflow field
    branch: Optional[str] = None
    defer_start: bool = False
    cli_tool: Optional[str] = None  # 'claude' or 'codex'; defaults to 'claude'


class CronCallback(BaseModel):
    """Callback info for notifying the cloud when a cron agent finishes."""
    url: str
    secret: str = ""
    cron_id: str = ""
    cron_name: str = ""
    agent_name: str = ""
    project_id: str = ""
    user_id: str = ""


class RunAgentRequest(BaseModel):
    """Request to run an agent with its system prompt (e.g. from a cron)."""
    agent_name: str = "Agent"
    system_prompt: str
    instructions: str
    working_dir: Optional[str] = None
    callback: Optional[CronCallback] = None
    cli_tool: Optional[str] = None  # 'claude' or 'codex'; defaults to 'claude'


class TaskExecuteRequest(BaseModel):
    """Request from relay to execute a task in a specific local_path."""
    task_id: str
    task_number: int
    title: str
    description: Optional[str] = None
    local_path: Optional[str] = None
    repository_url: Optional[str] = None
    cli_tool: Optional[str] = None  # 'claude' or 'codex'; defaults to 'claude'


class ImageData(BaseModel):
    data: str  # base64 data URL (data:image/png;base64,...)
    name: str = "image"
    type: str = "image/png"


class InputRequest(BaseModel):
    input: str
    images: Optional[List[ImageData]] = None
    cli_tool: Optional[str] = None  # Override CLI provider before first message (prepared sessions only)


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
        prompt=prompt,
        cli_tool=request.cli_tool
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
    # Derive skip_branch from workflow — non-code workflows skip git worktree
    skip_branch = request.skip_branch or request.workflow in ("ask", "plan", "workspace")

    return await agent_manager.create(
        task_id=request.task_id,
        task_number=request.task_number,
        task_title=request.title,
        task_description=request.description,
        working_dir=request.working_dir,
        prompt=request.prompt,
        skip_branch=skip_branch,
        branch=request.branch,
        defer_start=request.defer_start,
        cli_tool=request.cli_tool
    )


@router.post("/run-agent")
async def run_agent(request: RunAgentRequest):
    """Run an agent with a system prompt and instructions.

    Used by cron jobs and other automated triggers.
    The agent's system_prompt is passed via --append-system-prompt,
    and the instructions are the user message via -p.
    """
    working_dir = request.working_dir or get_default_working_dir()

    if working_dir and not os.path.isdir(working_dir):
        raise HTTPException(
            status_code=400,
            detail=f"Working directory does not exist: {working_dir}"
        )

    print(f"[RunAgent] Starting agent: {request.agent_name}")
    print(f"[RunAgent] Working directory: {working_dir}")

    callback_dict = None
    if request.callback:
        callback_dict = request.callback.model_dump()

    result = await agent_manager.create(
        task_title=request.agent_name,
        working_dir=working_dir,
        prompt=request.instructions,
        system_prompt=request.system_prompt,
        skip_branch=True,
        callback=callback_dict,
        cli_tool=request.cli_tool
    )

    return {
        "success": True,
        "agent_id": result.get("id"),
        "agent_name": request.agent_name,
        "status": result.get("status"),
        "work_dir": result.get("work_dir")
    }


class RunWorkflowRequest(BaseModel):
    """Request to run a workflow."""
    workflow_yaml: Optional[str] = None  # the workflow YAML content (from machine.command)
    working_dir: Optional[str] = None
    from_step: Optional[str] = None  # resume from this step
    step: Optional[str] = None  # run only this step
    callback: Optional[CronCallback] = None
    # For machines without a workflow — auto-generate a default
    machine_id: Optional[str] = None
    system_prompt: Optional[str] = None
    instructions: Optional[str] = None
    agent_name: Optional[str] = None


def _build_default_yaml(machine_id: Optional[str], instructions: str, agent_name: str) -> str:
    """Build a default workflow YAML for machines without one."""
    steps = []
    if machine_id:
        steps.append(f'  - name: load-context\n    description: Load machine context (agent, memory, metrics, tasks)\n    run: "kompany-workflow load-context {machine_id}"')
        escaped = instructions.replace('"', '\\"')
        steps.append(f'  - name: run\n    description: "{agent_name}"\n    run: \'kompany-workflow llm -s "$STEP_LOAD_CONTEXT_STDOUT" -p "{escaped}"\'\n    timeout: 300')
    else:
        escaped = instructions.replace('"', '\\"')
        steps.append(f'  - name: run\n    description: "{agent_name}"\n    run: \'kompany-workflow llm -p "{escaped}"\'\n    timeout: 300')

    return f"name: {agent_name}\ndescription: Auto-generated workflow\n\nsteps:\n" + "\n\n".join(steps) + "\n"


@router.post("/run-workflow")
async def run_workflow(request: RunWorkflowRequest):
    """Run a workflow. The YAML comes from the request body (stored in machine.command).
    If no YAML provided, auto-generates a default LLM workflow.
    """
    import subprocess as sp
    import tempfile

    working_dir = request.working_dir or get_default_working_dir()

    if working_dir and not os.path.isdir(working_dir):
        raise HTTPException(status_code=400, detail=f"Working directory does not exist: {working_dir}")

    # Get the workflow YAML
    yaml_content = request.workflow_yaml
    if not yaml_content:
        yaml_content = _build_default_yaml(
            request.machine_id,
            request.instructions or "Run your default behavior.",
            request.agent_name or "default",
        )
        print(f"[RunWorkflow] Auto-generated default workflow")

    # Write YAML to temp file
    tmp = tempfile.NamedTemporaryFile(mode='w', suffix='.yml', prefix='wf-', delete=False)
    tmp.write(yaml_content)
    tmp.close()
    workflow_file = tmp.name

    # Build command
    cmd = ["kompany-workflow", "run", "-f", workflow_file]
    if request.from_step:
        cmd.extend(["--from", request.from_step])
    if request.step:
        cmd.extend(["--step", request.step])

    print(f"[RunWorkflow] Running: {' '.join(cmd)}")
    print(f"[RunWorkflow] Working dir: {working_dir}")

    try:
        result = sp.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,  # 10 min max
            cwd=working_dir,
        )

        print(f"[RunWorkflow] Exit code: {result.returncode}")

        # Parse JSON output
        try:
            workflow_result = json.loads(result.stdout)
        except json.JSONDecodeError:
            workflow_result = {
                "status": "error",
                "error": "Failed to parse workflow output",
                "stdout": result.stdout[-2000:] if result.stdout else "",
                "stderr": result.stderr[-2000:] if result.stderr else "",
            }

        # Save run history to cloud API
        callback_dict = request.callback.model_dump() if request.callback else {}
        try:
            import httpx
            callback_url = callback_dict.get("url", "")
            api_base = callback_url.rsplit("/api/", 1)[0] if callback_url else "https://kompany.dev"

            run_payload = {
                "machine_id": request.machine_id or None,
                "project_id": callback_dict.get("project_id"),
                "user_id": callback_dict.get("user_id"),
                "status": workflow_result.get("status", "error"),
                "duration_ms": workflow_result.get("duration_ms", 0),
                "triggered_by": "cron" if request.callback else "manual",
                "cron_id": callback_dict.get("cron_id"),
                "error": workflow_result.get("error"),
                "resume_from": workflow_result.get("resume_from"),
                "steps": workflow_result.get("steps", []),
            }

            async with httpx.AsyncClient() as client:
                # Save run history
                await client.post(f"{api_base}/api/workflow-runs", json=run_payload, timeout=10)
                print(f"[RunWorkflow] Saved run history")

                # Handle cron callback
                if callback_url:
                    await client.post(callback_url, json={
                        "cron_id": callback_dict.get("cron_id"),
                        "cron_name": callback_dict.get("cron_name"),
                        "agent_name": callback_dict.get("agent_name"),
                        "project_id": callback_dict.get("project_id"),
                        "user_id": callback_dict.get("user_id"),
                        "status": "completed" if workflow_result.get("status") == "completed" else "failed",
                        "output": workflow_result.get("steps", [{}])[-1].get("stdout", "")[:2000] if workflow_result.get("steps") else "",
                    }, headers={"x-cron-secret": callback_dict.get("secret", "")}, timeout=10)
        except Exception as e:
            print(f"[RunWorkflow] Post-run error: {e}")

        # Clean up temp file
        if os.path.exists(workflow_file):
            os.unlink(workflow_file)

        return {
            "success": result.returncode == 0,
            "workflow": workflow_result,
        }

    except sp.TimeoutExpired:
        if os.path.exists(workflow_file):
            os.unlink(workflow_file)
        return {"success": False, "workflow": {"status": "error", "error": "Workflow timed out after 600s"}}
    except Exception as e:
        if os.path.exists(workflow_file):
            os.unlink(workflow_file)
        raise HTTPException(status_code=500, detail=str(e))


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

    # Handle prepared sessions: first message spawns the CLI process
    if agent["status"] == "prepared":
        # Allow overriding CLI tool before spawning (user may have changed selection)
        if request.cli_tool:
            agent_obj = agent_manager._agents.get(agent_id)
            if agent_obj:
                agent_obj.cli_tool = request.cli_tool
        await agent_manager.spawn_cli_process(agent_id, message, image_paths)
        return {"success": True, "action": "started", "cli_tool": agent.get("cli_tool"), "images": len(image_paths)}

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

            # If agent is prepared (deferred start), send status so frontend knows
            if agent['status'] == 'prepared':
                prepared_event = {
                    "type": "prepared",
                    "message": "Session ready. Send a message to start."
                }
                yield f"data: {json.dumps(prepared_event)}\n\n"

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
def check_cli_installed():
    """Check which AI CLI tools are installed locally."""
    from ..cli_providers import get_available_providers

    providers = get_available_providers()

    # Backwards compatible: 'installed' is True if any provider is available
    any_installed = any(p["installed"] for p in providers.values())
    # Claude path for backwards compat
    claude_info = providers.get("claude", {})

    return {
        "installed": any_installed,
        "path": claude_info.get("path"),
        "providers": providers
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
