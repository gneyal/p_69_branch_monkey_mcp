"""
Local Agent Server for Branch Monkey Relay

This module provides a FastAPI server that handles local Claude Code agent operations.
When combined with the relay client, it allows cloud users to run agents on local machines.

Routes:
- POST /api/local-claude/agents - Create and start a new agent
- GET /api/local-claude/agents - List all agents
- GET /api/local-claude/agents/{id} - Get agent info
- DELETE /api/local-claude/agents/{id} - Kill an agent
- POST /api/local-claude/agents/{id}/input - Send input to agent
- GET /api/local-claude/agents/{id}/stream - SSE stream of agent output
- GET /api/local-claude/check - Check if Claude CLI is installed
"""

import asyncio
import json
import os
import pty
import select
import shutil
import signal
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel


# =============================================================================
# FastAPI App
# =============================================================================

app = FastAPI(
    title="Branch Monkey Local Agent Server",
    description="Local server for running Claude Code agents",
    version="0.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# Git Utilities
# =============================================================================

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
    import re
    slug = title.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    slug = slug[:30].rstrip('-')

    if run_id:
        return f"task/{task_number}-{slug}-{run_id[:6]}"
    return f"task/{task_number}-{slug}"


def create_worktree(repo_dir: str, branch: str, task_number: int, run_id: str) -> dict:
    """Create a git worktree for a task run."""
    try:
        git_root = get_git_root(repo_dir) or repo_dir
        worktrees_dir = Path(git_root) / ".worktrees"
        worktree_path = worktrees_dir / f"task-{task_number}-{run_id}"

        print(f"[Worktree] Creating: {worktree_path} -> {branch}")

        worktrees_dir.mkdir(parents=True, exist_ok=True)

        branch_created = False
        if not branch_exists(git_root, branch):
            print(f"[Worktree] Creating branch: {branch}")
            result = subprocess.run(
                ["git", "branch", branch],
                cwd=git_root,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                raise Exception(f"Failed to create branch: {result.stderr}")
            branch_created = True

        result = subprocess.run(
            ["git", "worktree", "add", str(worktree_path), branch],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            raise Exception(f"Failed to create worktree: {result.stderr}")

        # Create .claude/settings.local.json for auto-accept permissions
        claude_dir = worktree_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings_file = claude_dir / "settings.local.json"
        settings = {
            "permissions": {
                "allow": [
                    "Edit", "Write",
                    "Bash(git:*)", "Bash(npm:*)", "Bash(npx:*)", "Bash(node:*)",
                    "Bash(mkdir:*)", "Bash(rm:*)", "Bash(cp:*)", "Bash(mv:*)",
                    "Bash(cat:*)", "Bash(ls:*)", "Bash(pwd:*)", "Bash(cd:*)",
                    "Bash(echo:*)", "Bash(grep:*)", "Bash(find:*)", "Bash(head:*)",
                    "Bash(tail:*)", "Bash(wc:*)", "Bash(sort:*)", "Bash(sed:*)",
                    "Bash(awk:*)", "Bash(curl:*)", "Bash(python:*)", "Bash(python3:*)",
                    "Bash(pip:*)", "Bash(pip3:*)", "Bash(uv:*)", "Bash(cargo:*)",
                    "Bash(go:*)", "Bash(make:*)", "Bash(gh:*)"
                ],
                "deny": []
            },
            "trust": [str(worktree_path), str(git_root)]
        }
        with open(settings_file, "w") as f:
            json.dump(settings, f, indent=2)

        return {
            "success": True,
            "worktree_path": str(worktree_path),
            "message": f"Created worktree with {'new' if branch_created else 'existing'} branch: {branch}",
            "branch_created": branch_created
        }

    except Exception as e:
        print(f"[Worktree] Error: {e}")
        return {
            "success": False,
            "worktree_path": "",
            "message": str(e),
            "branch_created": False
        }


def remove_worktree(repo_dir: str, worktree_path: str) -> None:
    """Remove a git worktree."""
    try:
        git_root = get_git_root(repo_dir) or repo_dir
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree_path],
            cwd=git_root,
            capture_output=True,
            check=True
        )
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=git_root,
            capture_output=True
        )
    except Exception:
        shutil.rmtree(worktree_path, ignore_errors=True)


# =============================================================================
# Local Agent Manager
# =============================================================================

@dataclass
class LocalAgent:
    """Represents a running local Claude Code agent."""
    id: str
    task_id: Optional[str]
    task_number: Optional[int]
    task_title: str
    task_description: Optional[str]
    repo_dir: str
    work_dir: str
    worktree_path: Optional[str]
    branch: Optional[str]
    branch_created: bool
    status: str  # starting, running, paused, completed, failed, stopped
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    output_buffer: List[str] = field(default_factory=list)
    output_listeners: List[asyncio.Queue] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    exit_code: Optional[int] = None
    session_id: Optional[str] = None


class LocalAgentManager:
    """Manages local Claude Code agent instances."""

    def __init__(self):
        self._agents: Dict[str, LocalAgent] = {}
        self._output_tasks: Dict[str, asyncio.Task] = {}

    async def create(
        self,
        task_id: Optional[str] = None,
        task_number: Optional[int] = None,
        task_title: str = "",
        task_description: Optional[str] = None,
        working_dir: Optional[str] = None,
        prompt: Optional[str] = None
    ) -> dict:
        """Create and start a new local Claude Code agent."""

        claude_path = shutil.which("claude")
        if not claude_path:
            raise HTTPException(
                status_code=400,
                detail="Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
            )

        agent_id = str(uuid.uuid4())[:8]
        repo_dir = working_dir or os.getcwd()
        work_dir = repo_dir
        branch = None
        branch_created = False
        worktree_path = None

        # Handle git worktree if in a git repo with task number
        if task_number and is_git_repo(repo_dir):
            branch = generate_branch_name(task_number, task_title, agent_id)
            result = create_worktree(repo_dir, branch, task_number, agent_id)

            if result["success"]:
                worktree_path = result["worktree_path"]
                work_dir = worktree_path
                branch_created = result["branch_created"]
            else:
                branch = get_current_branch(repo_dir)
        elif is_git_repo(repo_dir):
            branch = get_current_branch(repo_dir)

        # Build prompt
        if prompt:
            final_prompt = prompt
        else:
            task_json = {
                "task_uuid": task_id,
                "task_number": task_number,
                "title": task_title or "Untitled task",
                "description": task_description or "",
                "branch": branch,
                "worktree_path": str(worktree_path) if worktree_path else None
            }
            final_prompt = f"""Please start working on this task:

```json
{json.dumps(task_json, indent=2)}
```"""

        agent = LocalAgent(
            id=agent_id,
            task_id=task_id,
            task_number=task_number,
            task_title=task_title,
            task_description=task_description,
            repo_dir=repo_dir,
            work_dir=work_dir,
            worktree_path=worktree_path,
            branch=branch,
            branch_created=branch_created,
            status="starting"
        )

        self._agents[agent_id] = agent

        try:
            env = os.environ.copy()
            env.pop("ANTHROPIC_API_KEY", None)  # Use user's subscription

            # Use print mode with JSON output
            cmd = [
                "claude",
                "-p", final_prompt,
                "--output-format", "stream-json",
                "--verbose",
                "--dangerously-skip-permissions"
            ]

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=work_dir,
                env=env,
                bufsize=1,
                universal_newlines=False
            )

            agent.pid = process.pid
            agent.process = process
            agent.status = "running"

            print(f"[LocalAgent] Started Claude, PID: {process.pid}")

            self._output_tasks[agent_id] = asyncio.create_task(
                self._read_json_output(agent)
            )

            return {
                "id": agent_id,
                "task_id": task_id,
                "task_number": task_number,
                "task_title": task_title,
                "status": agent.status,
                "type": "local",
                "work_dir": work_dir,
                "worktree_path": worktree_path,
                "branch": branch,
                "branch_created": branch_created,
                "is_worktree": worktree_path is not None
            }

        except Exception as e:
            agent.status = "failed"
            raise HTTPException(status_code=500, detail=f"Failed to start Claude: {str(e)}")

    async def _read_json_output(self, agent: LocalAgent) -> None:
        """Read JSON output from subprocess and broadcast to listeners."""
        loop = asyncio.get_event_loop()

        def read_line():
            try:
                if agent.process and agent.process.stdout:
                    line = agent.process.stdout.readline()
                    return line
                return b''
            except Exception:
                return b''

        while agent.status == "running":
            try:
                line = await loop.run_in_executor(None, read_line)

                if not line:
                    break

                text = line.decode('utf-8', errors='replace').strip()
                if not text:
                    continue

                agent.last_activity = datetime.now()

                try:
                    parsed = json.loads(text)

                    # Extract session_id from system init message
                    if parsed.get("type") == "system" and parsed.get("subtype") == "init":
                        session_id = parsed.get("session_id")
                        if session_id:
                            agent.session_id = session_id
                            print(f"[LocalAgent] Got session_id: {session_id}")

                    agent.output_buffer.append({"data": text, "parsed": parsed})
                    if len(agent.output_buffer) > 1000:
                        agent.output_buffer.pop(0)

                    for queue in agent.output_listeners:
                        try:
                            await queue.put({
                                "type": "output",
                                "data": text,
                                "raw": text
                            })
                        except Exception:
                            pass

                except json.JSONDecodeError:
                    for queue in agent.output_listeners:
                        try:
                            await queue.put({
                                "type": "output",
                                "data": text
                            })
                        except Exception:
                            pass

            except Exception as e:
                print(f"[LocalAgent] Read error: {e}")
                break

        if agent.process:
            agent.exit_code = agent.process.wait()

        if agent.session_id:
            agent.status = "paused"
            print(f"[LocalAgent] Agent {agent.id} paused, session can be resumed")

            for queue in agent.output_listeners:
                try:
                    await queue.put({
                        "type": "paused",
                        "exit_code": agent.exit_code,
                        "session_id": agent.session_id,
                        "can_resume": True
                    })
                except Exception:
                    pass
        else:
            agent.status = "completed" if agent.exit_code == 0 else "failed"

            for queue in agent.output_listeners:
                try:
                    await queue.put({
                        "type": "exit",
                        "exit_code": agent.exit_code
                    })
                except Exception:
                    pass

    async def _run_with_resume(self, agent: LocalAgent, message: str) -> None:
        """Run a follow-up message using session resume."""
        if not agent.session_id:
            return

        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)

        cmd = [
            "claude",
            "-p", message,
            "--output-format", "stream-json",
            "--verbose",
            "--resume", agent.session_id,
            "--dangerously-skip-permissions"
        ]

        print(f"[LocalAgent] Resuming session {agent.session_id}")

        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=agent.work_dir,
            env=env,
            bufsize=1,
            universal_newlines=False
        )

        agent.process = process
        agent.pid = process.pid
        agent.status = "running"

        await self._read_json_output(agent)

    def get(self, agent_id: str) -> Optional[dict]:
        """Get agent info by ID."""
        agent = self._agents.get(agent_id)
        if not agent:
            return None

        return {
            "id": agent.id,
            "task_id": agent.task_id,
            "task_number": agent.task_number,
            "task_title": agent.task_title,
            "status": agent.status,
            "type": "local",
            "work_dir": agent.work_dir,
            "worktree_path": agent.worktree_path,
            "branch": agent.branch,
            "branch_created": agent.branch_created,
            "is_worktree": agent.worktree_path is not None,
            "created_at": agent.created_at.isoformat(),
            "last_activity": agent.last_activity.isoformat(),
            "exit_code": agent.exit_code,
            "session_id": agent.session_id,
            "can_resume": agent.session_id is not None
        }

    def list(self) -> List[dict]:
        """List all agents."""
        return [
            {
                "id": a.id,
                "task_id": a.task_id,
                "task_number": a.task_number,
                "task_title": a.task_title,
                "status": a.status,
                "type": "local",
                "branch": a.branch,
                "created_at": a.created_at.isoformat(),
                "last_activity": a.last_activity.isoformat(),
                "session_id": a.session_id,
                "can_resume": a.session_id is not None
            }
            for a in self._agents.values()
        ]

    async def resume_session(self, agent_id: str, message: str) -> bool:
        """Resume an agent session with a follow-up message."""
        agent = self._agents.get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        if not agent.session_id:
            raise HTTPException(
                status_code=400,
                detail="No session ID available. Cannot resume session."
            )

        if agent.status == "running":
            raise HTTPException(
                status_code=400,
                detail="Agent is already running. Wait for it to complete."
            )

        try:
            if agent_id in self._output_tasks:
                self._output_tasks[agent_id].cancel()

            self._output_tasks[agent_id] = asyncio.create_task(
                self._run_with_resume(agent, message)
            )

            return True

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to resume session: {str(e)}")

    def kill(self, agent_id: str, cleanup_worktree: bool = False) -> None:
        """Kill an agent and optionally cleanup worktree."""
        agent = self._agents.get(agent_id)
        if not agent:
            return

        if agent.pid:
            try:
                os.kill(agent.pid, signal.SIGTERM)
                try:
                    os.waitpid(agent.pid, os.WNOHANG)
                except Exception:
                    pass
            except ProcessLookupError:
                pass
            except Exception:
                try:
                    os.kill(agent.pid, signal.SIGKILL)
                except Exception:
                    pass
            agent.status = "stopped"

        if agent_id in self._output_tasks:
            self._output_tasks[agent_id].cancel()
            del self._output_tasks[agent_id]

        if cleanup_worktree and agent.worktree_path and agent.repo_dir:
            remove_worktree(agent.repo_dir, agent.worktree_path)

        del self._agents[agent_id]

    def add_listener(self, agent_id: str) -> asyncio.Queue:
        """Add an output listener for streaming."""
        agent = self._agents.get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        queue = asyncio.Queue()

        for item in agent.output_buffer:
            if isinstance(item, dict):
                queue.put_nowait({"type": "output", **item})
            else:
                queue.put_nowait({"type": "output", "data": item})

        agent.output_listeners.append(queue)
        return queue

    def remove_listener(self, agent_id: str, queue: asyncio.Queue) -> None:
        """Remove an output listener."""
        agent = self._agents.get(agent_id)
        if agent and queue in agent.output_listeners:
            agent.output_listeners.remove(queue)

    def get_output(self, agent_id: str) -> str:
        """Get full output buffer."""
        agent = self._agents.get(agent_id)
        if not agent:
            return ""
        parts = []
        for item in agent.output_buffer:
            if isinstance(item, dict):
                parts.append(item.get("data", ""))
            else:
                parts.append(item)
        return "".join(parts)


# Singleton instance
agent_manager = LocalAgentManager()


# =============================================================================
# API Routes
# =============================================================================

class CreateAgentRequest(BaseModel):
    task_id: Optional[str] = None
    task_number: Optional[int] = None
    title: str = "Local Task"
    description: Optional[str] = None
    working_dir: Optional[str] = None
    prompt: Optional[str] = None


class InputRequest(BaseModel):
    input: str


@app.post("/api/local-claude/agents")
async def create_agent(request: CreateAgentRequest):
    """Create and start a new local Claude Code agent."""
    return await agent_manager.create(
        task_id=request.task_id,
        task_number=request.task_number,
        task_title=request.title,
        task_description=request.description,
        working_dir=request.working_dir,
        prompt=request.prompt
    )


@app.get("/api/local-claude/agents")
def list_agents():
    """List all local agents."""
    return agent_manager.list()


@app.get("/api/local-claude/agents/{agent_id}")
def get_agent(agent_id: str):
    """Get agent info by ID."""
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    return agent


@app.post("/api/local-claude/agents/{agent_id}/input")
async def send_input(agent_id: str, request: InputRequest):
    """Send input to agent (resumes session if paused)."""
    agent = agent_manager.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found")

    message = request.input.rstrip('\n')

    if agent["status"] in ("paused", "completed", "failed") and agent.get("session_id"):
        await agent_manager.resume_session(agent_id, message)
        return {"success": True, "action": "resumed"}

    if agent["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail="Agent is running. Wait for it to complete before sending another message."
        )

    raise HTTPException(
        status_code=400,
        detail="No active session. Start a new task."
    )


@app.delete("/api/local-claude/agents/{agent_id}")
def kill_agent(agent_id: str, cleanup_worktree: bool = False):
    """Kill an agent."""
    agent_manager.kill(agent_id, cleanup_worktree)
    return {"success": True}


@app.get("/api/local-claude/agents/{agent_id}/output")
def get_output(agent_id: str):
    """Get full output buffer."""
    output = agent_manager.get_output(agent_id)
    return {"output": output}


@app.get("/api/local-claude/agents/{agent_id}/stream")
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


@app.get("/api/local-claude/check")
def check_claude_installed():
    """Check if Claude Code CLI is installed locally."""
    claude_path = shutil.which("claude")
    return {
        "installed": claude_path is not None,
        "path": claude_path
    }


@app.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "branch-monkey-relay"}


# =============================================================================
# Server Runner
# =============================================================================

def run_server(port: int = 8081, host: str = "127.0.0.1"):
    """Run the FastAPI server."""
    import uvicorn
    print(f"\n[Server] Starting local agent server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
