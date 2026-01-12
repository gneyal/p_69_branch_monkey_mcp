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
# Default Working Directory
# =============================================================================

_default_working_dir: Optional[str] = None


def set_default_working_dir(directory: str) -> None:
    """Set the default working directory for agent execution."""
    global _default_working_dir
    _default_working_dir = directory
    print(f"[Server] Default working directory: {directory}")


def get_default_working_dir() -> str:
    """Get the default working directory (falls back to cwd)."""
    return _default_working_dir or os.getcwd()


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
        repo_dir = working_dir or get_default_working_dir()
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


@app.get("/api/status")
def api_status():
    """Status endpoint for frontend compatibility."""
    return {
        "status": "ok",
        "service": "branch-monkey-relay",
        "agents": len(agent_manager._agents),
        "mode": "local"
    }


# =============================================================================
# Merge and Dev Server Endpoints
# =============================================================================

class MergeRequest(BaseModel):
    task_number: int
    branch: str
    target_branch: Optional[str] = None


class DevServerRequest(BaseModel):
    task_id: Optional[str] = None
    task_number: int


class OpenInEditorRequest(BaseModel):
    task_number: Optional[int] = None
    path: Optional[str] = None


# Track running dev servers
_running_dev_servers: Dict[int, dict] = {}
BASE_DEV_PORT = 6000


def find_worktree_path(task_number: int) -> Optional[str]:
    """Find the worktree path for a task number."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        return None

    worktrees_dir = Path(git_root) / ".worktrees"
    if not worktrees_dir.exists():
        return None

    # Find most recent worktree for this task
    matching_dirs = []
    for d in worktrees_dir.iterdir():
        if d.is_dir() and d.name.startswith(f"task-{task_number}"):
            matching_dirs.append(d)

    if not matching_dirs:
        return None

    # Return the most recently modified one
    matching_dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return str(matching_dirs[0])


@app.get("/api/local-claude/merge-preview")
def merge_preview(task_number: int, branch: str):
    """Get commit info for merge preview visualization."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    current_branch = get_current_branch(git_root)

    def get_commits(ref: str, limit: int = 5) -> List[dict]:
        """Get commits from a ref with details."""
        try:
            result = subprocess.run(
                ["git", "log", ref, f"-{limit}", "--pretty=format:%H|%s|%an|%ar"],
                cwd=git_root,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                return []

            commits = []
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('|', 3)
                if len(parts) >= 4:
                    commits.append({
                        "hash": parts[0][:7],
                        "message": parts[1][:50] + ('...' if len(parts[1]) > 50 else ''),
                        "author": parts[2],
                        "date": parts[3]
                    })
            return commits
        except Exception:
            return []

    def get_unique_commits(feature_branch: str, base_branch: str) -> List[dict]:
        """Get commits that are in feature but not in base."""
        try:
            result = subprocess.run(
                ["git", "log", f"{base_branch}..{feature_branch}", "--pretty=format:%H|%s|%an|%ar"],
                cwd=git_root,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                return []

            commits = []
            for line in result.stdout.strip().split('\n'):
                if not line:
                    continue
                parts = line.split('|', 3)
                if len(parts) >= 4:
                    commits.append({
                        "hash": parts[0][:7],
                        "message": parts[1][:50] + ('...' if len(parts[1]) > 50 else ''),
                        "author": parts[2],
                        "date": parts[3]
                    })
            return commits
        except Exception:
            return []

    feature_commits = get_unique_commits(branch, current_branch)
    main_commits = get_commits(current_branch, 3)

    return {
        "target_branch": current_branch,
        "source_branch": branch,
        "feature_commits": feature_commits,
        "main_commits": main_commits
    }


@app.get("/api/local-claude/diff")
def get_branch_diff(branch: str):
    """Get diff between a branch and main."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    try:
        # Get diff between main and the branch
        result = subprocess.run(
            ["git", "diff", "main..." + branch],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # Try without the ... syntax
            result = subprocess.run(
                ["git", "diff", "main", branch],
                cwd=git_root,
                capture_output=True,
                text=True
            )

        return {"diff": result.stdout or "No changes", "branch": branch}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/local-claude/open-in-editor")
def open_in_editor(request: OpenInEditorRequest):
    """Open a worktree or path in VS Code."""
    target_path = None

    if request.path:
        target_path = Path(request.path)
    elif request.task_number:
        target_path = find_worktree_path(request.task_number)
        if not target_path:
            raise HTTPException(status_code=404, detail=f"No worktree found for task {request.task_number}")
    else:
        raise HTTPException(status_code=400, detail="Either task_number or path required")

    if not target_path.exists():
        raise HTTPException(status_code=404, detail=f"Path does not exist: {target_path}")

    try:
        # Try to open in VS Code
        result = subprocess.run(
            ["code", str(target_path)],
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            # VS Code not found, try 'open' on macOS
            import platform
            if platform.system() == "Darwin":
                subprocess.run(["open", str(target_path)])
            else:
                raise HTTPException(status_code=500, detail="Could not open editor. VS Code not found.")

        return {"success": True, "path": str(target_path)}
    except FileNotFoundError:
        # 'code' command not found
        import platform
        if platform.system() == "Darwin":
            subprocess.run(["open", str(target_path)])
            return {"success": True, "path": str(target_path), "editor": "finder"}
        raise HTTPException(status_code=500, detail="VS Code CLI not found. Install 'code' command from VS Code.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/local-claude/merge")
def merge_worktree_branch(request: MergeRequest):
    """Merge a worktree branch into the target branch."""
    if not request.branch:
        raise HTTPException(status_code=400, detail="Branch name required")

    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        raise HTTPException(status_code=400, detail="Not in a git repository")

    original_branch = get_current_branch(git_root)
    target = request.target_branch or original_branch

    if request.target_branch and request.target_branch != original_branch:
        checkout_result = subprocess.run(
            ["git", "checkout", request.target_branch],
            cwd=git_root,
            capture_output=True,
            text=True
        )
        if checkout_result.returncode != 0:
            raise HTTPException(
                status_code=400,
                detail=f"Failed to checkout {request.target_branch}: {checkout_result.stderr}"
            )

    # Find worktree and commit any uncommitted changes
    worktree_path = find_worktree_path(request.task_number)
    if worktree_path:
        try:
            status_result = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=worktree_path,
                capture_output=True,
                text=True
            )

            if status_result.stdout.strip():
                subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
                subprocess.run(
                    ["git", "commit", "-m", f"Task #{request.task_number}: Agent changes"],
                    cwd=worktree_path,
                    capture_output=True
                )
        except Exception:
            pass

    # Merge the branch
    try:
        result = subprocess.run(
            ["git", "merge", request.branch, "--no-edit"],
            cwd=git_root,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            error_msg = result.stderr
            if "CONFLICT" in error_msg or "Automatic merge failed" in error_msg:
                subprocess.run(["git", "merge", "--abort"], cwd=git_root, capture_output=True)
                raise HTTPException(
                    status_code=409,
                    detail="Merge conflict detected. Please resolve conflicts manually."
                )
            raise HTTPException(status_code=500, detail=error_msg)

        return {
            "success": True,
            "message": f"Successfully merged {request.branch} into {target}",
            "output": result.stdout,
            "target_branch": target
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def is_port_in_use(port: int) -> bool:
    """Check if a port is in use."""
    import socket
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


@app.post("/api/local-claude/dev-server")
async def start_dev_server(request: DevServerRequest):
    """Start a dev server for a worktree."""
    task_number = request.task_number

    # Check if already running
    if task_number in _running_dev_servers:
        info = _running_dev_servers[task_number]
        return {
            "port": info["port"],
            "url": f"http://localhost:{info['port']}",
            "status": "already_running"
        }

    # Find worktree
    worktree_path = find_worktree_path(task_number)
    if not worktree_path:
        raise HTTPException(status_code=404, detail=f"No worktree found for task {task_number}")

    # Check for frontend directory
    frontend_path = Path(worktree_path) / "frontend"
    if not frontend_path.exists():
        raise HTTPException(status_code=404, detail="No frontend directory in worktree")

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

    # Find available port
    port = find_available_port(BASE_DEV_PORT + task_number)

    # Start the dev server
    process = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port)],
        cwd=str(frontend_path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True
    )

    # Track it
    _running_dev_servers[task_number] = {
        "process": process,
        "port": port,
        "task_id": request.task_id,
        "started_at": datetime.now().isoformat()
    }

    # Wait for server to start
    await asyncio.sleep(3)

    return {
        "port": port,
        "url": f"http://localhost:{port}",
        "status": "started"
    }


@app.get("/api/local-claude/dev-server")
def list_dev_servers():
    """List running dev servers."""
    servers = []
    for task_number, info in _running_dev_servers.items():
        servers.append({
            "taskNumber": task_number,
            "port": info["port"],
            "url": f"http://localhost:{info['port']}",
            "startedAt": info["started_at"]
        })
    return servers


@app.delete("/api/local-claude/dev-server")
def stop_dev_server(task_number: int):
    """Stop a dev server."""
    if task_number not in _running_dev_servers:
        raise HTTPException(status_code=404, detail="Server not found")

    info = _running_dev_servers[task_number]
    try:
        os.killpg(os.getpgid(info["process"].pid), signal.SIGTERM)
    except Exception:
        try:
            info["process"].kill()
        except Exception:
            pass

    del _running_dev_servers[task_number]
    return {"status": "stopped"}


@app.delete("/api/local-claude/worktree")
def delete_worktree_endpoint(task_number: int, worktree_path: str):
    """Delete a git worktree."""
    try:
        work_dir = get_default_working_dir()
        git_root = get_git_root(work_dir) or work_dir
        remove_worktree(git_root, worktree_path)
        return {"success": True, "message": f"Worktree deleted: {worktree_path}"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete worktree: {str(e)}")


# =============================================================================
# Server Runner
# =============================================================================

def run_server(port: int = 8081, host: str = "127.0.0.1"):
    """Run the FastAPI server."""
    import uvicorn
    print(f"\n[Server] Starting local agent server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
