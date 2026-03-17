"""
Local Agent Manager for AI CLI execution.

This module manages the lifecycle of local AI agent instances (Claude Code, Codex, etc.),
including creation, execution, session resumption, and cleanup.
"""

import asyncio
import json
import os
import signal
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

from fastapi import HTTPException

from .cli_providers import get_provider, get_available_providers, get_default_cli, CliProvider
from .config import get_default_working_dir
from .git_utils import is_git_repo, get_current_branch, generate_branch_name
from .worktree import create_worktree, remove_worktree


@dataclass
class LocalAgent:
    """Represents a running local AI CLI agent."""
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
    status: str  # prepared, starting, running, paused, completed, failed, stopped
    cli_tool: str = ""  # Which CLI provider to use (resolved at creation time)
    pid: Optional[int] = None
    process: Optional[subprocess.Popen] = None
    output_buffer: List[str] = field(default_factory=list)
    output_listeners: List[asyncio.Queue] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: datetime = field(default_factory=datetime.now)
    exit_code: Optional[int] = None
    session_id: Optional[str] = None
    callback: Optional[Dict] = None  # Cron completion callback info


class LocalAgentManager:
    """Manages local Claude Code agent instances."""

    MAX_AGENTS = 10  # Maximum concurrent agents to prevent resource exhaustion
    STALE_TIMEOUT = 3600  # Agents idle for 1 hour are considered stale

    def __init__(self):
        self._agents: Dict[str, LocalAgent] = {}
        self._output_tasks: Dict[str, asyncio.Task] = {}

    def cleanup_stale_agents(self) -> int:
        """Remove agents that are completed, failed, or stale. Returns count removed."""
        now = datetime.now()
        stale_ids = []

        for agent_id, agent in self._agents.items():
            # Remove failed/stopped agents
            if agent.status in ("failed", "stopped"):
                stale_ids.append(agent_id)
                continue

            # Check if process is still running
            process_exited = False
            if agent.process:
                poll = agent.process.poll()
                if poll is not None:
                    process_exited = True

            # Remove completed/paused agents whose process has exited and are past stale timeout
            if agent.status in ("completed", "paused") or process_exited:
                if agent.created_at:
                    try:
                        age = (now - agent.created_at).total_seconds()
                        if age > self.STALE_TIMEOUT:
                            print(f"[LocalAgent] Agent {agent_id} is stale ({agent.status}, age={int(age)}s)")
                            stale_ids.append(agent_id)
                            continue
                    except Exception:
                        pass
                # No session_id = no resumption value, clean immediately
                if not agent.session_id:
                    stale_ids.append(agent_id)
                    continue

            # Check for stale agents (no activity for a while) regardless of status
            if agent.created_at:
                try:
                    if (now - agent.created_at).total_seconds() > self.STALE_TIMEOUT:
                        print(f"[LocalAgent] Agent {agent_id} is stale (created {agent.created_at})")
                        stale_ids.append(agent_id)
                except Exception:
                    pass

        for agent_id in stale_ids:
            print(f"[LocalAgent] Cleaning up agent {agent_id}")
            self.kill(agent_id)

        return len(stale_ids)

    async def create(
        self,
        task_id: Optional[str] = None,
        task_number: Optional[int] = None,
        task_title: str = "",
        task_description: Optional[str] = None,
        working_dir: Optional[str] = None,
        prompt: Optional[str] = None,
        system_prompt: Optional[str] = None,
        skip_branch: bool = False,
        branch: Optional[str] = None,
        defer_start: bool = False,
        callback: Optional[Dict] = None,
        cli_tool: Optional[str] = None
    ) -> dict:
        """Create and optionally start a new local AI agent.

        If defer_start=True, sets up worktree/branch/tracking but does NOT spawn
        the CLI process. The session enters "prepared" status and waits for the
        first message via send_input, which calls spawn_cli_process().

        Args:
            cli_tool: Which CLI to use ('claude', 'codex'). Defaults to 'claude'.
        """

        # Clean up stale agents first
        cleaned = self.cleanup_stale_agents()
        if cleaned > 0:
            print(f"[LocalAgent] Cleaned up {cleaned} stale agents")

        # Check max agent limit
        if len(self._agents) >= self.MAX_AGENTS:
            raise HTTPException(
                status_code=429,
                detail=f"Maximum number of agents ({self.MAX_AGENTS}) reached. Kill some agents first."
            )

        # Resolve CLI provider
        provider = get_provider(cli_tool)
        cli_path = provider.is_available()
        if not cli_path:
            raise HTTPException(
                status_code=400,
                detail=f"{provider.display_name} CLI not found. Install with: {provider.install_hint}"
            )

        agent_id = str(uuid.uuid4())[:8]
        repo_dir = working_dir or get_default_working_dir()
        work_dir = repo_dir
        target_branch = branch  # Explicit branch from caller (e.g. 'staging')
        branch_created = False
        worktree_path = None

        # Handle git worktree if in a git repo
        print(f"[LocalAgent] Worktree check: task_number={task_number}, branch={target_branch}, is_git={is_git_repo(repo_dir)}, skip_branch={skip_branch}")
        if is_git_repo(repo_dir):
            if task_number and not skip_branch:
                # Task mode: generate branch name from task number
                target_branch = generate_branch_name(task_number, task_title, agent_id)
                print(f"[LocalAgent] Creating worktree for task branch: {target_branch}")
                result = create_worktree(repo_dir, target_branch, task_number, agent_id)
                print(f"[LocalAgent] Worktree result: {result}")

                if result["success"]:
                    worktree_path = result["worktree_path"]
                    work_dir = worktree_path
                    branch_created = result["branch_created"]
                else:
                    target_branch = get_current_branch(repo_dir)
            elif target_branch:
                # Explicit branch mode (e.g. staging): create worktree for named branch
                print(f"[LocalAgent] Creating worktree for explicit branch: {target_branch}")
                result = create_worktree(repo_dir, target_branch, 0, f"{target_branch}-{agent_id}")
                print(f"[LocalAgent] Worktree result: {result}")

                if result["success"]:
                    worktree_path = result["worktree_path"]
                    work_dir = worktree_path
                    branch_created = result["branch_created"]
                else:
                    target_branch = get_current_branch(repo_dir)
            else:
                # No task, no explicit branch: work in current directory
                target_branch = get_current_branch(repo_dir)

        # If deferring start, create the agent record in "prepared" status and return
        if defer_start:
            agent = LocalAgent(
                id=agent_id,
                task_id=task_id,
                task_number=task_number,
                task_title=task_title,
                task_description=task_description,
                repo_dir=repo_dir,
                work_dir=work_dir,
                worktree_path=worktree_path,
                branch=target_branch,
                branch_created=branch_created,
                status="prepared",
                cli_tool=provider.name,
                callback=callback
            )
            self._agents[agent_id] = agent
            print(f"[LocalAgent] Session prepared (deferred start): {agent_id}")

            return {
                "id": agent_id,
                "task_id": task_id,
                "task_number": task_number,
                "task_title": task_title,
                "status": "prepared",
                "type": "local",
                "work_dir": work_dir,
                "worktree_path": worktree_path,
                "branch": target_branch,
                "branch_created": branch_created,
                "is_worktree": worktree_path is not None
            }

        # Build prompt and spawn CLI process immediately
        final_prompt = self._build_prompt(prompt, task_id, task_number, task_title, task_description, target_branch, worktree_path, work_dir)

        agent = LocalAgent(
            id=agent_id,
            task_id=task_id,
            task_number=task_number,
            task_title=task_title,
            task_description=task_description,
            repo_dir=repo_dir,
            work_dir=work_dir,
            worktree_path=worktree_path,
            branch=target_branch,
            branch_created=branch_created,
            status="starting",
            cli_tool=provider.name,
            callback=callback
        )

        self._agents[agent_id] = agent

        try:
            self._start_cli_process(agent, final_prompt, system_prompt=system_prompt)

            return {
                "id": agent_id,
                "task_id": task_id,
                "task_number": task_number,
                "task_title": task_title,
                "status": agent.status,
                "type": "local",
                "cli_tool": agent.cli_tool,
                "work_dir": work_dir,
                "worktree_path": worktree_path,
                "branch": target_branch,
                "branch_created": branch_created,
                "is_worktree": worktree_path is not None
            }

        except Exception as e:
            agent.status = "failed"
            raise HTTPException(status_code=500, detail=f"Failed to start {provider.display_name}: {str(e)}")

    def _build_prompt(
        self,
        prompt: Optional[str],
        task_id: Optional[str],
        task_number: Optional[int],
        task_title: str,
        task_description: Optional[str],
        target_branch: Optional[str],
        worktree_path: Optional[str],
        work_dir: Optional[str] = None
    ) -> str:
        """Build the final prompt, prepending worktree/workspace info if applicable."""
        if prompt:
            final_prompt = prompt
            if worktree_path:
                worktree_info = f"""## IMPORTANT: Worktree Already Created
You are working in an isolated git worktree at: `{worktree_path}`
Branch: `{target_branch}`

Do NOT create another worktree - you are already isolated. Skip any worktree creation steps.

---

"""
                final_prompt = worktree_info + final_prompt
            return final_prompt
        else:
            task_json = {
                "task_uuid": task_id,
                "task_number": task_number,
                "title": task_title or "Untitled task",
                "description": task_description or "",
                "branch": target_branch,
                "worktree_path": str(worktree_path) if worktree_path else None
            }
            return f"""Please start working on this task:

```json
{json.dumps(task_json, indent=2)}
```"""

    def _get_provider(self, agent: LocalAgent) -> CliProvider:
        """Get the CLI provider for an agent."""
        return get_provider(agent.cli_tool)

    def _start_cli_process(self, agent: LocalAgent, final_prompt: str, system_prompt: Optional[str] = None) -> None:
        """Spawn the CLI process and start reading output."""
        provider = self._get_provider(agent)
        cli_cmd = provider.build_run_command(final_prompt, system_prompt=system_prompt)

        env = os.environ.copy()
        for key in cli_cmd.env_overrides:
            env.pop(key, None)
        # Always remove CLAUDECODE to allow nested launches
        env.pop("CLAUDECODE", None)
        # Inject auth env vars (e.g. API keys from config)
        if cli_cmd.env_inject:
            env.update(cli_cmd.env_inject)

        process = subprocess.Popen(
            cli_cmd.args,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=agent.work_dir,
            env=env,
            bufsize=1,
            universal_newlines=False
        )

        agent.pid = process.pid
        agent.process = process
        agent.status = "running"

        print(f"[LocalAgent] Started {provider.display_name}, PID: {process.pid}")

        self._output_tasks[agent.id] = asyncio.create_task(
            self._read_json_output(agent)
        )

    async def spawn_cli_process(self, agent_id: str, message: str, image_paths: List[str] = None) -> None:
        """Spawn a CLI process for a prepared session (first message).

        This is called when send_input detects a "prepared" agent.
        Builds the prompt from the message and starts the CLI process.
        """
        agent = self._agents.get(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found")

        if agent.status != "prepared":
            raise HTTPException(status_code=400, detail=f"Agent is not in prepared state (status: {agent.status})")

        # Build the final prompt with worktree/workspace context + user message
        final_prompt = self._build_prompt(
            message, agent.task_id, agent.task_number,
            agent.task_title, agent.task_description,
            agent.branch, agent.worktree_path, agent.work_dir
        )

        print(f"[LocalAgent] Spawning CLI for prepared session {agent_id}")

        try:
            self._start_cli_process(agent, final_prompt)
        except Exception as e:
            agent.status = "failed"
            provider = self._get_provider(agent)
            raise HTTPException(status_code=500, detail=f"Failed to start {provider.display_name}: {str(e)}")

    async def _read_json_output(self, agent: LocalAgent) -> None:
        """Read JSON output from subprocess and broadcast to listeners."""
        loop = asyncio.get_event_loop()
        provider = self._get_provider(agent)

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

                    # Normalize event through CLI provider (handles format differences)
                    normalized = provider.normalize_event(parsed)
                    if normalized is None:
                        continue

                    # Extract session_id using provider-specific logic
                    session_id = provider.extract_session_id(parsed)
                    if session_id:
                        agent.session_id = session_id
                        print(f"[LocalAgent] Got session_id: {session_id}")

                    # Store normalized event for consistent downstream processing
                    normalized_text = json.dumps(normalized)
                    agent.output_buffer.append({"data": normalized_text, "parsed": normalized})
                    if len(agent.output_buffer) > 1000:
                        agent.output_buffer.pop(0)

                    for queue in agent.output_listeners:
                        try:
                            await queue.put({
                                "type": "output",
                                "data": normalized_text,
                                "raw": text
                            })
                        except Exception:
                            pass

                except json.JSONDecodeError:
                    # Filter out known noise using provider-specific rules
                    if provider.is_noise(text):
                        continue
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

        # Cron agents (with callback) should complete, not pause — they don't need
        # session resumption and would otherwise linger in the compute pool.
        if agent.callback:
            agent.status = "completed" if agent.exit_code == 0 else "failed"
            agent.session_id = None  # Don't keep session — allows cleanup
            print(f"[LocalAgent] Cron agent {agent.id} {agent.status} (exit={agent.exit_code})")

            for queue in agent.output_listeners:
                try:
                    await queue.put({
                        "type": "exit",
                        "exit_code": agent.exit_code
                    })
                except Exception:
                    pass

            await self._fire_callback(agent)
        elif agent.session_id:
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

    def _extract_result(self, agent: LocalAgent) -> str:
        """Extract the final result text from the agent's output buffer.

        Looks for the 'result' type message in the stream-json output.
        Falls back to collecting assistant message text content.
        """
        # Look for explicit result message (Claude CLI stream-json format)
        for item in reversed(agent.output_buffer):
            parsed = item.get("parsed") if isinstance(item, dict) else None
            if not parsed:
                continue
            if parsed.get("type") == "result":
                return parsed.get("result", "")

        # Fallback: collect all assistant text content
        text_parts = []
        for item in agent.output_buffer:
            parsed = item.get("parsed") if isinstance(item, dict) else None
            if not parsed:
                continue
            if parsed.get("type") == "assistant":
                message = parsed.get("message", {})
                for block in message.get("content", []):
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))

        return "\n\n".join(text_parts) if text_parts else ""

    async def _fire_callback(self, agent: LocalAgent) -> None:
        """Send completion callback to the cloud (for cron-triggered agents)."""
        import httpx

        callback = agent.callback
        if not callback or not callback.get("url"):
            return

        result_text = self._extract_result(agent)
        status = agent.status  # completed, failed, or paused

        payload = {
            "cron_id": callback.get("cron_id", ""),
            "cron_name": callback.get("cron_name", ""),
            "agent_name": callback.get("agent_name", ""),
            "project_id": callback.get("project_id", ""),
            "user_id": callback.get("user_id", ""),
            "status": "completed" if status in ("completed", "paused") else "failed",
            "output": result_text
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    callback["url"],
                    json=payload,
                    headers={"x-cron-secret": callback.get("secret", "")}
                )
            print(f"[LocalAgent] Callback sent for {agent.task_title}: status={resp.status_code}")
        except Exception as e:
            print(f"[LocalAgent] Callback failed for {agent.task_title}: {e}")

    async def _run_with_resume(self, agent: LocalAgent, message: str, image_paths: List[str] = None) -> None:
        """Run a follow-up message using session resume.

        Args:
            agent: The agent to resume
            message: The follow-up message
            image_paths: Optional list of image file paths (already included in message text)
        """
        if not agent.session_id:
            return

        provider = self._get_provider(agent)
        cli_cmd = provider.build_resume_command(message, agent.session_id)

        env = os.environ.copy()
        for key in cli_cmd.env_overrides:
            env.pop(key, None)
        env.pop("CLAUDECODE", None)
        if cli_cmd.env_inject:
            env.update(cli_cmd.env_inject)

        if image_paths:
            print(f"[LocalAgent] Message includes {len(image_paths)} image paths for CLI to read")

        print(f"[LocalAgent] Resuming session {agent.session_id} with {provider.display_name}")

        process = subprocess.Popen(
            cli_cmd.args,
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
            "cli_tool": agent.cli_tool,
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
                "cli_tool": a.cli_tool,
                "branch": a.branch,
                "worktree_path": a.worktree_path,
                "created_at": a.created_at.isoformat(),
                "last_activity": a.last_activity.isoformat(),
                "session_id": a.session_id,
                "can_resume": a.session_id is not None
            }
            for a in self._agents.values()
        ]

    async def resume_session(self, agent_id: str, message: str, image_paths: List[str] = None) -> bool:
        """Resume an agent session with a follow-up message.

        Args:
            agent_id: The agent to resume
            message: The follow-up message (may already contain image references)
            image_paths: Optional list of image file paths to include
        """
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

        if image_paths:
            print(f"[LocalAgent] Resuming with {len(image_paths)} images: {image_paths}")

        try:
            if agent_id in self._output_tasks:
                self._output_tasks[agent_id].cancel()

            self._output_tasks[agent_id] = asyncio.create_task(
                self._run_with_resume(agent, message, image_paths)
            )

            return True

        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to resume session: {str(e)}")

    def kill(self, agent_id: str, cleanup_worktree: bool = False) -> None:
        """Kill an agent and optionally cleanup worktree."""
        agent = self._agents.get(agent_id)
        if not agent:
            return

        print(f"[LocalAgent] Killing agent {agent_id}")

        # Cancel output reading task first
        if agent_id in self._output_tasks:
            self._output_tasks[agent_id].cancel()
            del self._output_tasks[agent_id]

        # Close stdout pipe to release file descriptor
        if agent.process and agent.process.stdout:
            try:
                agent.process.stdout.close()
            except Exception:
                pass

        # Terminate the process
        if agent.process:
            try:
                agent.process.terminate()
                try:
                    agent.process.wait(timeout=2)
                except Exception:
                    agent.process.kill()
                    agent.process.wait(timeout=1)
            except Exception:
                pass
        elif agent.pid:
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

        if cleanup_worktree and agent.worktree_path and agent.repo_dir:
            remove_worktree(agent.repo_dir, agent.worktree_path)

        del self._agents[agent_id]
        print(f"[LocalAgent] Agent {agent_id} killed, {len(self._agents)} agents remaining")

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
