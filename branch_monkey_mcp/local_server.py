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
import re
import select
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
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
if _default_working_dir:
    print(f"[Server] Working directory from env: {_default_working_dir}")


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
# Dev Proxy Server
# =============================================================================

# Default auth-allowed port for dev proxy
DEFAULT_PROXY_PORT = 5177

# Current proxy state
_proxy_state = {
    "target_port": None,
    "target_run_id": None,
    "server": None,
    "thread": None,
    "running": False,
    "proxy_port": DEFAULT_PROXY_PORT  # Configurable at runtime
}


class DevProxyHandler(BaseHTTPRequestHandler):
    """HTTP request handler that proxies to the target dev server."""

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

    def do_request(self, method: str):
        """Handle any HTTP method by proxying to target."""
        target_port = _proxy_state.get("target_port")
        if not target_port:
            self.send_error(503, "No dev server is currently active")
            return

        # Build target URL
        target_url = f"http://localhost:{target_port}{self.path}"

        # Read request body if present
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length) if content_length > 0 else None

        # Forward headers (except host)
        headers = {k: v for k, v in self.headers.items() if k.lower() != 'host'}

        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.request(
                    method=method,
                    url=target_url,
                    headers=headers,
                    content=body,
                    follow_redirects=False
                )

                # Send response
                self.send_response(response.status_code)
                for key, value in response.headers.items():
                    if key.lower() not in ('transfer-encoding', 'connection', 'keep-alive'):
                        self.send_header(key, value)
                self.end_headers()
                self.wfile.write(response.content)

        except httpx.ConnectError:
            self.send_error(502, f"Cannot connect to dev server on port {target_port}")
        except Exception as e:
            self.send_error(500, str(e))

    def do_GET(self):
        self.do_request("GET")

    def do_POST(self):
        self.do_request("POST")

    def do_PUT(self):
        self.do_request("PUT")

    def do_DELETE(self):
        self.do_request("DELETE")

    def do_PATCH(self):
        self.do_request("PATCH")

    def do_OPTIONS(self):
        self.do_request("OPTIONS")

    def do_HEAD(self):
        self.do_request("HEAD")


def start_dev_proxy(proxy_port: int = None) -> bool:
    """Start the dev proxy server on the configured port."""
    global _proxy_state

    if proxy_port is None:
        proxy_port = _proxy_state["proxy_port"]

    if _proxy_state["running"]:
        if _proxy_state["proxy_port"] == proxy_port:
            print(f"[DevProxy] Already running on port {proxy_port}")
            return True
        # Port changed, need to restart
        stop_dev_proxy()

    # Check if port is available
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex(('localhost', proxy_port)) == 0:
            print(f"[DevProxy] Port {proxy_port} is already in use")
            return False

    try:
        server = HTTPServer(('127.0.0.1', proxy_port), DevProxyHandler)

        def serve():
            print(f"[DevProxy] Started on http://localhost:{proxy_port}")
            server.serve_forever()

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()

        _proxy_state["server"] = server
        _proxy_state["thread"] = thread
        _proxy_state["running"] = True
        _proxy_state["proxy_port"] = proxy_port

        return True
    except Exception as e:
        print(f"[DevProxy] Failed to start: {e}")
        return False


def stop_dev_proxy():
    """Stop the dev proxy server."""
    global _proxy_state

    if _proxy_state["server"]:
        _proxy_state["server"].shutdown()
        _proxy_state["server"] = None
        _proxy_state["thread"] = None
        _proxy_state["running"] = False
        _proxy_state["target_port"] = None
        _proxy_state["target_run_id"] = None
        print("[DevProxy] Stopped")


def set_proxy_target(port: int, run_id: str = None):
    """Set the target port for the dev proxy."""
    global _proxy_state
    _proxy_state["target_port"] = port
    _proxy_state["target_run_id"] = run_id
    print(f"[DevProxy] Target set to port {port}" + (f" (run {run_id})" if run_id else ""))


def get_proxy_status() -> dict:
    """Get current proxy status."""
    proxy_port = _proxy_state["proxy_port"]
    return {
        "running": _proxy_state["running"],
        "proxyPort": proxy_port,
        "targetPort": _proxy_state["target_port"],
        "targetRunId": _proxy_state["target_run_id"],
        "proxyUrl": f"http://localhost:{proxy_port}" if _proxy_state["running"] else None
    }


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

        # Copy .env files from main repo to worktree (they're gitignored)
        copied_envs = []
        for env_pattern in [".env", ".env.local", ".env.development", ".env.development.local"]:
            # Check common locations: root, frontend/, src/
            for subdir in ["", "frontend", "src"]:
                src_env = Path(git_root) / subdir / env_pattern if subdir else Path(git_root) / env_pattern
                if src_env.exists():
                    dst_env = worktree_path / subdir / env_pattern if subdir else worktree_path / env_pattern
                    dst_env.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_env, dst_env)
                    copied_envs.append(str(dst_env.relative_to(worktree_path)))

        if copied_envs:
            print(f"[Worktree] Copied env files: {copied_envs}")

        return {
            "success": True,
            "worktree_path": str(worktree_path),
            "message": f"Created worktree with {'new' if branch_created else 'existing'} branch: {branch}",
            "branch_created": branch_created,
            "copied_env_files": copied_envs
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
                "worktree_path": a.worktree_path,
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


def _get_dashboard_response():
    """Return the dashboard HTML file response."""
    dashboard_path = Path(__file__).parent / "static" / "dashboard.html"
    return FileResponse(dashboard_path, media_type="text/html")


@app.get("/")
def serve_root():
    """Serve the dashboard at root."""
    return _get_dashboard_response()


@app.get("/dashboard")
def serve_dashboard():
    """Serve the dashboard HTML."""
    return _get_dashboard_response()


@app.get("/api/status")
def api_status():
    """Status endpoint for frontend compatibility."""
    return {
        "status": "ok",
        "service": "branch-monkey-relay",
        "agents": len(agent_manager._agents),
        "mode": "local",
        "working_directory": get_default_working_dir()
    }


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


@app.get("/api/config")
async def get_app_config():
    """Get app configuration, proxied from cloud with caching."""
    global _cached_app_config, _app_config_fetched_at

    # Use cache if fresh (within 5 minutes)
    if _cached_app_config and _app_config_fetched_at:
        age_seconds = (datetime.utcnow() - _app_config_fetched_at).total_seconds()
        if age_seconds < 300:
            return _cached_app_config

    # Try to fetch from cloud
    relay_status = get_relay_status()
    cloud_url = relay_status.get("cloud_url") or "https://p-63-branch-monkey.pages.dev"

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


# =============================================================================
# Relay Status Endpoints
# =============================================================================


class RelayHeartbeat(BaseModel):
    """Relay heartbeat request."""
    machine_id: str
    machine_name: str
    cloud_url: str


@app.get("/api/relay/status")
def relay_status():
    """Get current relay connection status."""
    status = get_relay_status()
    # Check if heartbeat is recent (within 60 seconds)
    if status["last_heartbeat"]:
        from datetime import datetime
        last_hb = datetime.fromisoformat(status["last_heartbeat"])
        age_seconds = (datetime.utcnow() - last_hb).total_seconds()
        status["connected"] = age_seconds < 60
        status["heartbeat_age_seconds"] = int(age_seconds)
    return status


@app.post("/api/relay/heartbeat")
def relay_heartbeat(heartbeat: RelayHeartbeat):
    """Receive heartbeat from relay client to indicate it's connected."""
    update_relay_status(
        connected=True,
        machine_id=heartbeat.machine_id,
        machine_name=heartbeat.machine_name,
        cloud_url=heartbeat.cloud_url
    )
    return {"status": "ok", "received": datetime.utcnow().isoformat()}


@app.post("/api/relay/disconnect")
def relay_disconnect():
    """Mark relay as disconnected."""
    update_relay_status(connected=False)
    return {"status": "ok", "disconnected": True}


# =============================================================================
# Working Directory Configuration
# =============================================================================


class WorkingDirectoryRequest(BaseModel):
    """Request to set working directory."""
    directory: str


@app.get("/api/config/working-directory")
def get_working_directory():
    """Get the current working directory for agent execution."""
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)

    # Check if it's a valid git repo
    is_git_repo = git_root is not None

    # Count worktrees if it's a git repo
    worktree_count = 0
    if git_root:
        worktrees_dir = Path(git_root) / ".worktrees"
        if worktrees_dir.exists():
            worktree_count = len([d for d in worktrees_dir.iterdir() if d.is_dir()])

    return {
        "working_directory": work_dir,
        "git_root": git_root,
        "is_git_repo": is_git_repo,
        "worktree_count": worktree_count
    }


@app.post("/api/config/working-directory")
def set_working_directory(request: WorkingDirectoryRequest):
    """Set the working directory for agent execution."""
    directory = request.directory

    # Validate directory exists
    if not os.path.isdir(directory):
        raise HTTPException(status_code=400, detail=f"Directory does not exist: {directory}")

    # Resolve to absolute path
    abs_path = os.path.abspath(directory)

    # Check if it's a git repo
    git_root = get_git_root(abs_path)
    if not git_root:
        raise HTTPException(status_code=400, detail=f"Not a git repository: {abs_path}")

    # Set the new working directory
    set_default_working_dir(abs_path)

    # Count worktrees
    worktree_count = 0
    worktrees_dir = Path(git_root) / ".worktrees"
    if worktrees_dir.exists():
        worktree_count = len([d for d in worktrees_dir.iterdir() if d.is_dir()])

    return {
        "status": "ok",
        "working_directory": abs_path,
        "git_root": git_root,
        "worktree_count": worktree_count
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
    run_id: Optional[str] = None  # Run/session ID - a task can have multiple runs
    dev_script: Optional[str] = None  # Custom script, e.g. "cd frontend && npm run dev --port {port}"


class OpenInEditorRequest(BaseModel):
    task_number: Optional[int] = None
    path: Optional[str] = None


# Track running dev servers by run_id (or task_number as fallback)
_running_dev_servers: Dict[str, dict] = {}
BASE_DEV_PORT = 6000

# Database path for persisting dev server state
_DB_PATH = Path(__file__).parent.parent / ".branch_monkey" / "data.db"


def _init_dev_servers_db():
    """Initialize the dev_servers table if it doesn't exist."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dev_servers (
            run_id TEXT PRIMARY KEY,
            task_id TEXT,
            task_number INTEGER,
            port INTEGER NOT NULL,
            worktree_path TEXT,
            started_at TEXT NOT NULL,
            pid INTEGER
        )
    """)
    conn.commit()
    conn.close()


def _save_dev_server_to_db(run_id: str, info: dict):
    """Save dev server info to database."""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO dev_servers
        (run_id, task_id, task_number, port, worktree_path, started_at, pid)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        info.get("task_id"),
        info.get("task_number"),
        info["port"],
        info.get("worktree_path"),
        info.get("started_at"),
        info.get("process").pid if info.get("process") else None
    ))
    conn.commit()
    conn.close()


def _delete_dev_server_from_db(run_id: str):
    """Delete dev server from database."""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("DELETE FROM dev_servers WHERE run_id = ?", (run_id,))
    conn.commit()
    conn.close()


def _load_dev_servers_from_db():
    """Load dev servers from database and validate they're still running."""
    global _running_dev_servers

    if not _DB_PATH.exists():
        return

    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT * FROM dev_servers")
    rows = cursor.fetchall()
    conn.close()

    for row in rows:
        run_id = row["run_id"]
        port = row["port"]
        pid = row["pid"]

        # Check if the port is still in use (server still running)
        if _is_port_in_use(port):
            _running_dev_servers[run_id] = {
                "process": None,  # Can't restore process object
                "port": port,
                "task_id": row["task_id"],
                "task_number": row["task_number"],
                "run_id": run_id,
                "worktree_path": row["worktree_path"],
                "started_at": row["started_at"],
                "pid": pid
            }
            print(f"[DevServer] Restored dev server {run_id} on port {port}")
        else:
            # Server no longer running, clean up DB
            _delete_dev_server_from_db(run_id)
            print(f"[DevServer] Cleaned up stale dev server {run_id}")


def _is_port_in_use(port: int) -> bool:
    """Check if a port is currently in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0


# Initialize database on module load
_init_dev_servers_db()
_load_dev_servers_from_db()


def find_worktree_path(task_number: int) -> Optional[str]:
    """Find the worktree path for a task number.

    Uses trailing dash in prefix matching to avoid false matches
    (e.g., task-29 should not match task-290).
    """
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if not git_root:
        return None

    worktrees_dir = Path(git_root) / ".worktrees"
    if not worktrees_dir.exists():
        return None

    # Find most recent worktree for this task
    # Use "task-{number}-" prefix to avoid matching wrong tasks (task-29 != task-290)
    matching_dirs = []
    prefix = f"task-{task_number}-"
    for d in worktrees_dir.iterdir():
        if d.is_dir() and d.name.startswith(prefix):
            matching_dirs.append(d)

    if not matching_dirs:
        return None

    # Return the most recently modified one
    matching_dirs.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    return str(matching_dirs[0])


def find_actual_branch(task_number: int) -> Optional[str]:
    """Find the actual branch for a task by checking the worktree or git branches.

    This handles cases where the stored branch name doesn't match the actual branch
    (e.g., branch was created with a different naming convention).
    """
    # First, try to get branch from worktree
    worktree_path = find_worktree_path(task_number)
    if worktree_path:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=worktree_path,
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                branch_name = result.stdout.strip()
                if branch_name and branch_name != "HEAD":
                    return branch_name
        except Exception:
            pass

    # Fallback: search git branches by pattern
    work_dir = get_default_working_dir()
    git_root = get_git_root(work_dir)
    if git_root:
        try:
            result = subprocess.run(
                ["git", "branch", "-a", "--list", f"*task/{task_number}-*"],
                cwd=git_root,
                capture_output=True,
                text=True
            )
            if result.returncode == 0 and result.stdout.strip():
                lines = [b.strip().lstrip("* ") for b in result.stdout.strip().split("\n")]
                local_branch = next((b for b in lines if not b.startswith("remotes/")), None)
                if local_branch:
                    return local_branch
        except Exception:
            pass

    return None


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

    # Determine the actual branch to merge
    branch_to_merge = request.branch

    # Check if the provided branch exists
    check_result = subprocess.run(
        ["git", "rev-parse", "--verify", request.branch],
        cwd=git_root,
        capture_output=True,
        text=True
    )
    if check_result.returncode != 0:
        # Branch doesn't exist - try to find the actual branch by task number
        print(f"[Merge] Branch {request.branch} not found, searching for actual branch...")
        actual_branch = find_actual_branch(request.task_number)
        if actual_branch:
            print(f"[Merge] Found actual branch: {actual_branch}")
            branch_to_merge = actual_branch
        else:
            raise HTTPException(
                status_code=404,
                detail=f"Branch {request.branch} not found and could not find an alternative branch for task {request.task_number}"
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
            ["git", "merge", branch_to_merge, "--no-edit"],
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
            "message": f"Successfully merged {branch_to_merge} into {target}",
            "output": result.stdout,
            "target_branch": target,
            "actual_branch": branch_to_merge if branch_to_merge != request.branch else None
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


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


@app.post("/api/local-claude/dev-server")
async def start_dev_server(request: DevServerRequest):
    """Start a dev server for a worktree."""
    task_number = request.task_number
    run_id = request.run_id or str(task_number)  # Fallback to task_number if no run_id
    dev_script = request.dev_script

    # Ensure proxy is running
    if not _proxy_state["running"]:
        start_dev_proxy()

    # Check if already running for this run
    if run_id in _running_dev_servers:
        info = _running_dev_servers[run_id]
        # Update proxy to point to this server
        set_proxy_target(info["port"], run_id)
        proxy_status = get_proxy_status()
        return {
            "port": info["port"],
            "url": f"http://localhost:{info['port']}",
            "proxyUrl": proxy_status["proxyUrl"],
            "runId": run_id,
            "status": "already_running"
        }

    # Find worktree
    worktree_path = find_worktree_path(task_number)
    if not worktree_path:
        raise HTTPException(status_code=404, detail=f"No worktree found for task {task_number}")

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
        "task_id": request.task_id,
        "task_number": task_number,
        "run_id": run_id,
        "worktree_path": str(worktree_path),
        "started_at": datetime.now().isoformat()
    }

    # Persist to database for recovery after restart
    _save_dev_server_to_db(run_id, _running_dev_servers[run_id])

    # Wait for server to start
    await asyncio.sleep(3)

    # Set proxy target to this server
    set_proxy_target(port, run_id)
    proxy_status = get_proxy_status()

    return {
        "port": port,
        "url": f"http://localhost:{port}",
        "proxyUrl": proxy_status["proxyUrl"],
        "runId": run_id,
        "status": "started"
    }


@app.get("/api/local-claude/dev-server")
def list_dev_servers():
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
            "isActive": is_active,
            "startedAt": info["started_at"],
            "worktreePath": info.get("worktree_path")
        })
    return {"servers": servers, "proxy": proxy_status}


@app.get("/api/local-claude/dev-proxy")
def get_dev_proxy():
    """Get current dev proxy status."""
    return get_proxy_status()


@app.post("/api/local-claude/dev-proxy")
def set_dev_proxy_target(run_id: str):
    """Set proxy target to a specific running dev server by run_id."""
    if run_id not in _running_dev_servers:
        raise HTTPException(status_code=404, detail=f"No dev server running for run {run_id}")

    # Ensure proxy is running
    if not _proxy_state["running"]:
        start_dev_proxy()

    info = _running_dev_servers[run_id]
    set_proxy_target(info["port"], run_id)
    return get_proxy_status()


@app.put("/api/local-claude/dev-proxy/port")
def set_dev_proxy_port(port: int):
    """Set the proxy port. Restarts proxy if already running."""
    if port < 1024 or port > 65535:
        raise HTTPException(status_code=400, detail="Port must be between 1024 and 65535")

    old_port = _proxy_state["proxy_port"]
    was_running = _proxy_state["running"]
    target_port = _proxy_state["target_port"]
    target_run_id = _proxy_state["target_run_id"]

    # Update the port in state
    _proxy_state["proxy_port"] = port

    # If proxy was running, restart on new port
    if was_running:
        stop_dev_proxy()
        success = start_dev_proxy(port)
        if not success:
            # Revert to old port
            _proxy_state["proxy_port"] = old_port
            start_dev_proxy(old_port)
            raise HTTPException(status_code=500, detail=f"Port {port} is not available, reverted to {old_port}")
        # Restore target if there was one
        if target_port:
            set_proxy_target(target_port, target_run_id)

    return {
        "success": True,
        "oldPort": old_port,
        "newPort": port,
        "status": get_proxy_status()
    }


@app.delete("/api/local-claude/dev-proxy")
def stop_dev_proxy_endpoint():
    """Stop the dev proxy server."""
    stop_dev_proxy()
    return {"success": True, "status": get_proxy_status()}


@app.delete("/api/local-claude/dev-server")
def stop_dev_server_endpoint(run_id: str):
    """Stop a dev server by run_id."""
    if run_id not in _running_dev_servers:
        raise HTTPException(status_code=404, detail="Server not found")

    info = _running_dev_servers[run_id]
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
    _delete_dev_server_from_db(run_id)

    del _running_dev_servers[run_id]
    return {"status": "stopped", "runId": run_id}


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


@app.get("/api/local-claude/worktrees")
def list_worktrees():
    """List all git worktrees in the repository with detailed info."""
    try:
        work_dir = get_default_working_dir()
        git_root = get_git_root(work_dir)
        if not git_root:
            return {"worktrees": [], "error": "Not in a git repository"}

        # Get worktree list
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=git_root, capture_output=True, text=True
        )

        if result.returncode != 0:
            return {"worktrees": [], "error": "Failed to list worktrees"}

        worktrees = []
        current_wt = {}

        for line in result.stdout.strip().split('\n'):
            if not line:
                if current_wt:
                    worktrees.append(current_wt)
                    current_wt = {}
                continue

            if line.startswith('worktree '):
                current_wt['path'] = line[9:]
            elif line.startswith('HEAD '):
                current_wt['head'] = line[5:]
            elif line.startswith('branch '):
                branch = line[7:]
                if branch.startswith('refs/heads/'):
                    branch = branch[11:]
                current_wt['branch'] = branch
            elif line == 'bare':
                current_wt['bare'] = True
            elif line == 'detached':
                current_wt['detached'] = True

        if current_wt:
            worktrees.append(current_wt)

        # Filter to only task worktrees
        task_worktrees = [
            wt for wt in worktrees
            if '.worktrees' in wt.get('path', '') or wt.get('branch', '').startswith('task/')
        ]

        # Enrich each worktree with additional info
        for wt in task_worktrees:
            path = wt.get('path', '')
            branch = wt.get('branch', '')

            # Extract task number
            task_number = None
            match = re.search(r'task-(\d+)', path)
            if match:
                task_number = int(match.group(1))
            elif branch:
                match = re.search(r'task/(\d+)', branch)
                if match:
                    task_number = int(match.group(1))
            wt['task_number'] = task_number
            wt['is_main_repo'] = '.worktrees' not in path

            # Get last commit info
            if os.path.isdir(path):
                try:
                    commit_result = subprocess.run(
                        ["git", "log", "-1", "--pretty=format:%h|%s|%ar"],
                        cwd=path, capture_output=True, text=True
                    )
                    if commit_result.returncode == 0 and commit_result.stdout:
                        parts = commit_result.stdout.split('|', 2)
                        if len(parts) >= 3:
                            wt['last_commit'] = {
                                'hash': parts[0],
                                'message': parts[1][:50] + ('...' if len(parts[1]) > 50 else ''),
                                'date': parts[2]
                            }

                    # Check for uncommitted changes
                    status_result = subprocess.run(
                        ["git", "status", "--porcelain"],
                        cwd=path, capture_output=True, text=True
                    )
                    wt['has_changes'] = bool(status_result.stdout.strip())

                    # Check if merged to main
                    if branch and not wt.get('is_main_repo'):
                        merge_check = subprocess.run(
                            ["git", "branch", "--merged", "main"],
                            cwd=git_root, capture_output=True, text=True
                        )
                        merged_branches = [b.strip().lstrip('* ') for b in merge_check.stdout.strip().split('\n') if b.strip()]
                        wt['merged_to_main'] = branch in merged_branches

                        # If merged, get merge commit details
                        if wt['merged_to_main']:
                            # Find the merge commit on main that brought in this branch
                            # Look for merge commits that reference this branch
                            merge_commit_result = subprocess.run(
                                ["git", "log", "main", "--merges", "--grep", branch, "-1", "--pretty=format:%h|%s|%an|%ar"],
                                cwd=git_root, capture_output=True, text=True
                            )
                            if merge_commit_result.returncode == 0 and merge_commit_result.stdout.strip():
                                mc_parts = merge_commit_result.stdout.strip().split('|', 3)
                                if len(mc_parts) >= 4:
                                    wt['merge_commit'] = {
                                        'hash': mc_parts[0],
                                        'message': mc_parts[1][:50] + ('...' if len(mc_parts[1]) > 50 else ''),
                                        'author': mc_parts[2],
                                        'date': mc_parts[3]
                                    }
                            else:
                                # If no merge commit found (fast-forward merge), find where branch tip landed on main
                                # Get the commit where branch was merged (the branch's HEAD commit on main)
                                branch_tip = subprocess.run(
                                    ["git", "rev-parse", "--short", branch],
                                    cwd=git_root, capture_output=True, text=True
                                )
                                if branch_tip.returncode == 0:
                                    tip_sha = branch_tip.stdout.strip()
                                    # Get details of that commit
                                    tip_info = subprocess.run(
                                        ["git", "log", "-1", tip_sha, "--pretty=format:%h|%s|%an|%ar"],
                                        cwd=git_root, capture_output=True, text=True
                                    )
                                    if tip_info.returncode == 0 and tip_info.stdout.strip():
                                        tc_parts = tip_info.stdout.strip().split('|', 3)
                                        if len(tc_parts) >= 4:
                                            wt['merge_commit'] = {
                                                'hash': tc_parts[0],
                                                'message': tc_parts[1][:50] + ('...' if len(tc_parts[1]) > 50 else ''),
                                                'author': tc_parts[2],
                                                'date': tc_parts[3],
                                                'fast_forward': True
                                            }
                except Exception:
                    pass

        return {
            "worktrees": task_worktrees,
            "total": len(task_worktrees),
            "git_root": git_root
        }
    except Exception as e:
        return {"worktrees": [], "error": str(e)}


@app.get("/api/local-claude/commits")
def list_commits(limit: int = 10, branch: Optional[str] = None, all_branches: bool = False):
    """List recent commits for the current branch, specified branch, or all branches."""
    try:
        work_dir = get_default_working_dir()
        git_root = get_git_root(work_dir)
        if not git_root:
            return {"commits": [], "error": "Not in a git repository"}

        current_branch = branch or get_current_branch(git_root)

        # Use git log --graph to get accurate branch visualization
        # Format: GRAPH_CHARS COMMIT_MARKER hash|short_hash|parents|subject|author|email|relative_date|date|refs
        if all_branches or (not branch):
            cmd = ["git", "log", "--all", "--graph", f"-{limit}", "--pretty=format:COMMIT_MARKER%H|%h|%P|%s|%an|%ae|%ar|%ai|%D"]
        else:
            cmd = ["git", "log", current_branch, "--graph", f"-{limit}", "--pretty=format:COMMIT_MARKER%H|%h|%P|%s|%an|%ae|%ar|%ai|%D"]

        result = subprocess.run(
            cmd,
            cwd=git_root, capture_output=True, text=True
        )

        if result.returncode != 0:
            return {"commits": [], "branch": current_branch, "error": result.stderr}

        commits = []
        for line in result.stdout.split('\n'):
            if 'COMMIT_MARKER' not in line:
                continue

            # Split into graph part and commit part
            marker_idx = line.index('COMMIT_MARKER')
            graph_part = line[:marker_idx]
            commit_part = line[marker_idx + len('COMMIT_MARKER'):]

            # Parse graph characters to determine lane
            # Count position of * (commit marker) in graph
            # Each | or space before * represents a lane
            lane = 0
            for i, char in enumerate(graph_part):
                if char == '*':
                    # Lane is roughly i/2 since graph uses "| " pattern
                    lane = i // 2
                    break

            parts = commit_part.split('|', 8)
            if len(parts) >= 8:
                refs = parts[8] if len(parts) > 8 else ""
                parent_hashes = parts[2].split() if parts[2] else []
                commits.append({
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "parents": parent_hashes,
                    "message": parts[3],
                    "author": parts[4],
                    "author_email": parts[5],
                    "relative_date": parts[6],
                    "date": parts[7],
                    "refs": refs,
                    "lane": lane,  # Lane from git's own graph
                    "graph": graph_part.rstrip()  # Raw graph characters for reference
                })

        return {
            "commits": commits,
            "branch": current_branch,
            "total": len(commits)
        }
    except Exception as e:
        return {"commits": [], "error": str(e)}


# =============================================================================
# Time Machine - Visual Git History with Preview
# =============================================================================

# Track running time machine previews
_time_machine_previews: Dict[str, dict] = {}
TIME_MACHINE_BASE_PORT = 6100


@app.get("/api/local-claude/commit-diff/{sha}")
def get_commit_diff(sha: str):
    """Get detailed diff for a specific commit."""
    try:
        work_dir = get_default_working_dir()
        git_root = get_git_root(work_dir)
        if not git_root:
            raise HTTPException(status_code=404, detail="Not in a git repository")

        # Get commit info
        info_result = subprocess.run(
            ["git", "show", "--no-patch", "--format=%H|%s|%an|%ae|%ai|%P", sha],
            cwd=git_root, capture_output=True, text=True
        )
        if info_result.returncode != 0:
            raise HTTPException(status_code=404, detail=f"Commit not found: {sha}")

        parts = info_result.stdout.strip().split('|', 5)
        if len(parts) < 5:
            raise HTTPException(status_code=500, detail="Failed to parse commit info")

        full_sha = parts[0]
        message = parts[1]
        author = parts[2]
        author_email = parts[3]
        date = parts[4]
        parent_sha = parts[5] if len(parts) > 5 else None

        # Get file stats
        stat_result = subprocess.run(
            ["git", "show", "--stat", "--format=", sha],
            cwd=git_root, capture_output=True, text=True
        )

        files = []
        if stat_result.returncode == 0:
            for line in stat_result.stdout.strip().split('\n'):
                if '|' in line and ('+' in line or '-' in line):
                    # Parse: " filename | 10 +++---"
                    file_part = line.split('|')[0].strip()
                    stats_part = line.split('|')[1] if '|' in line else ""
                    insertions = stats_part.count('+')
                    deletions = stats_part.count('-')
                    files.append({
                        "path": file_part,
                        "insertions": insertions,
                        "deletions": deletions
                    })

        # Get full diff
        diff_result = subprocess.run(
            ["git", "show", "--format=", sha],
            cwd=git_root, capture_output=True, text=True
        )
        diff = diff_result.stdout if diff_result.returncode == 0 else ""

        return {
            "sha": sha,
            "full_sha": full_sha,
            "message": message,
            "author": author,
            "author_email": author_email,
            "date": date,
            "parent_sha": parent_sha,
            "files": files,
            "diff": diff
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class TimeMachinePreviewRequest(BaseModel):
    commit_sha: str


@app.post("/api/local-claude/time-machine/preview")
async def create_time_machine_preview(request: TimeMachinePreviewRequest):
    """Create a temporary worktree at a commit and start dev server."""
    commit_sha = request.commit_sha
    short_sha = commit_sha[:7]

    # Check if already running
    if short_sha in _time_machine_previews:
        info = _time_machine_previews[short_sha]
        return {
            "status": "already_running",
            "port": info["port"],
            "url": f"http://localhost:{info['port']}",
            "worktree_path": info["worktree_path"]
        }

    try:
        work_dir = get_default_working_dir()
        git_root = get_git_root(work_dir)
        if not git_root:
            raise HTTPException(status_code=404, detail="Not in a git repository")

        # Verify commit exists
        verify_result = subprocess.run(
            ["git", "cat-file", "-t", commit_sha],
            cwd=git_root, capture_output=True, text=True
        )
        if verify_result.returncode != 0:
            raise HTTPException(status_code=404, detail=f"Commit not found: {commit_sha}")

        # Create worktree directory
        worktrees_dir = Path(git_root) / ".worktrees"
        worktrees_dir.mkdir(exist_ok=True)

        worktree_name = f"timemachine-{short_sha}"
        worktree_path = worktrees_dir / worktree_name

        # Remove existing if present
        if worktree_path.exists():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=git_root, capture_output=True
            )

        # Create worktree at specific commit (detached HEAD)
        create_result = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree_path), commit_sha],
            cwd=git_root, capture_output=True, text=True
        )
        if create_result.returncode != 0:
            raise HTTPException(status_code=500, detail=f"Failed to create worktree: {create_result.stderr}")

        # Check for frontend directory
        frontend_path = worktree_path / "frontend"
        if not frontend_path.exists():
            # Cleanup and error
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=git_root, capture_output=True)
            raise HTTPException(status_code=404, detail="No frontend directory in this commit")

        # Install dependencies if needed
        node_modules = frontend_path / "node_modules"
        if not node_modules.exists():
            print(f"[TimeMachine] Installing dependencies for {short_sha}...")
            install_result = subprocess.run(
                ["npm", "install"],
                cwd=str(frontend_path),
                capture_output=True,
                timeout=180
            )
            if install_result.returncode != 0:
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=git_root, capture_output=True)
                raise HTTPException(status_code=500, detail="npm install failed")

        # Find available port
        port = TIME_MACHINE_BASE_PORT + len(_time_machine_previews)

        # Start dev server
        print(f"[TimeMachine] Starting dev server for {short_sha} on port {port}...")
        process = subprocess.Popen(
            ["npm", "run", "dev", "--", "--port", str(port)],
            cwd=str(frontend_path),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True
        )

        # Track it
        _time_machine_previews[short_sha] = {
            "process": process,
            "port": port,
            "worktree_path": str(worktree_path),
            "commit_sha": commit_sha,
            "started_at": datetime.now().isoformat()
        }

        # Wait for server to start
        await asyncio.sleep(3)

        return {
            "status": "started",
            "port": port,
            "url": f"http://localhost:{port}",
            "worktree_path": str(worktree_path),
            "commit_sha": commit_sha
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/local-claude/time-machine/preview/{sha}")
def delete_time_machine_preview(sha: str):
    """Stop dev server and cleanup worktree."""
    short_sha = sha[:7]

    if short_sha not in _time_machine_previews:
        raise HTTPException(status_code=404, detail="Preview not found")

    info = _time_machine_previews[short_sha]

    # Stop dev server
    try:
        os.killpg(os.getpgid(info["process"].pid), signal.SIGTERM)
    except Exception:
        try:
            info["process"].kill()
        except Exception:
            pass

    # Remove worktree
    try:
        work_dir = get_default_working_dir()
        git_root = get_git_root(work_dir)
        if git_root:
            subprocess.run(
                ["git", "worktree", "remove", "--force", info["worktree_path"]],
                cwd=git_root, capture_output=True
            )
    except Exception as e:
        print(f"[TimeMachine] Warning: Failed to remove worktree: {e}")

    del _time_machine_previews[short_sha]
    return {"status": "stopped", "message": "Preview stopped and worktree cleaned up"}


@app.get("/api/local-claude/time-machine/previews")
def list_time_machine_previews():
    """List active time machine previews."""
    previews = []
    for sha, info in _time_machine_previews.items():
        previews.append({
            "sha": sha,
            "commit_sha": info["commit_sha"],
            "port": info["port"],
            "url": f"http://localhost:{info['port']}",
            "worktree_path": info["worktree_path"],
            "started_at": info["started_at"]
        })
    return {"previews": previews}


# =============================================================================
# Server Runner
# =============================================================================

def run_server(port: int = 18081, host: str = "127.0.0.1"):
    """Run the FastAPI server."""
    import uvicorn
    print(f"\n[Server] Starting local agent server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
