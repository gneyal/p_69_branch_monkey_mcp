"""
Advanced endpoints: Time machine, AI suggestions, and agent definitions.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_default_working_dir
from ..git_utils import get_git_root
from ..dev_server import start_ngrok_tunnel, stop_ngrok_tunnel

router = APIRouter()

# Track time machine previews
_time_machine_previews: Dict[str, dict] = {}
_time_machine_tunnels: Dict[str, object] = {}  # Track ngrok tunnels for time machine
TIME_MACHINE_BASE_PORT = 6100

# Track agent definitions
_agent_definitions: Dict[str, dict] = {}

# Default agent definitions
DEFAULT_AGENT_DEFINITIONS = [
    {
        "id": "default-planner",
        "slug": "planner",
        "name": "Planner Agent",
        "description": "Plans versions and decomposes features into tasks",
        "system_prompt": """You are a project planning assistant. Your job is to break down a version/milestone into actionable development tasks.

IMPORTANT: You MUST respond with ONLY valid JSON. No explanations, no markdown, no text before or after the JSON.

Rules:
1. Generate 5-12 concrete, actionable tasks for the requested feature/version
2. Each task should be completable in 1-4 hours of focused work
3. Order tasks by dependency (what needs to be done first)
4. Assign the most appropriate agent_slug based on task type:
   - "code": Implementation, features, bug fixes, API endpoints
   - "test": Writing tests, QA validation, test coverage
   - "docs": Documentation, README updates, comments
   - "refactor": Code cleanup, optimization, restructuring
5. Do NOT include tasks that already exist (check existing_tasks)
6. Do NOT suggest meta-tasks like "plan" or "review" - suggest concrete implementation tasks
7. Focus on the specific feature requested, not general project improvements

Your response must be EXACTLY this JSON structure (no other text):
{
  "tasks": [
    {
      "title": "Implement user login endpoint",
      "description": "Create POST /api/auth/login endpoint with email/password validation",
      "priority": 1,
      "estimated_complexity": "medium",
      "agent_slug": "code"
    }
  ]
}""",
        "color": "#ec4899",
        "icon": "sparkles",
        "is_default": True,
        "sort_order": 0
    },
    {
        "id": "default-code",
        "slug": "code",
        "name": "Code Agent",
        "description": "General-purpose coding agent",
        "system_prompt": "You are a skilled software engineer. Focus on writing clean, efficient, and well-documented code.",
        "color": "#3b82f6",
        "icon": "code",
        "is_default": True,
        "sort_order": 1
    },
    {
        "id": "default-test",
        "slug": "test",
        "name": "Test Agent",
        "description": "Test writing and QA specialist",
        "system_prompt": "You are a QA engineer specializing in writing comprehensive tests. Focus on edge cases, error handling, and test coverage.",
        "color": "#22c55e",
        "icon": "check",
        "is_default": True,
        "sort_order": 2
    },
    {
        "id": "default-docs",
        "slug": "docs",
        "name": "Docs Agent",
        "description": "Documentation specialist",
        "system_prompt": "You are a technical writer. Focus on clear, comprehensive documentation that helps developers understand the codebase.",
        "color": "#f97316",
        "icon": "book",
        "is_default": True,
        "sort_order": 3
    },
    {
        "id": "default-refactor",
        "slug": "refactor",
        "name": "Refactor Agent",
        "description": "Code refactoring specialist",
        "system_prompt": "You are a code refactoring specialist. Focus on improving code structure, reducing complexity, and enhancing maintainability without changing functionality.",
        "color": "#a855f7",
        "icon": "refresh",
        "is_default": True,
        "sort_order": 4
    }
]


def _generate_agent_slug(name: str) -> str:
    """Generate a slug from a name."""
    slug = name.lower()
    slug = re.sub(r'[^a-z0-9\s-]', '', slug)
    slug = re.sub(r'\s+', '-', slug)
    slug = re.sub(r'-+', '-', slug)
    return slug[:50].rstrip('-')


def _init_default_agent_definitions():
    """Initialize default agent definitions if not already present."""
    for agent in DEFAULT_AGENT_DEFINITIONS:
        if agent["id"] not in _agent_definitions:
            _agent_definitions[agent["id"]] = {
                **agent,
                "created_at": datetime.utcnow().isoformat(),
                "updated_at": datetime.utcnow().isoformat()
            }


# Initialize defaults on import
_init_default_agent_definitions()


# =============================================================================
# Time Machine
# =============================================================================

class TimeMachinePreviewRequest(BaseModel):
    commit_sha: str
    tunnel: Optional[bool] = False  # Request ngrok tunnel for remote access
    dev_script: Optional[str] = None  # Custom dev script (e.g., "npx serve -l {port}")
    project_path: Optional[str] = None  # Project directory path (to find git root)


@router.post("/time-machine/preview")
async def create_time_machine_preview(request: TimeMachinePreviewRequest):
    """Create a temporary worktree at a commit and start dev server."""
    commit_sha = request.commit_sha
    short_sha = commit_sha[:7]

    # Check if already running
    if short_sha in _time_machine_previews:
        info = _time_machine_previews[short_sha]
        tunnel_url = info.get("tunnel_url")

        # Create tunnel if requested and not already created
        if request.tunnel and not tunnel_url:
            tunnel_url = start_ngrok_tunnel(info["port"], f"timemachine-{short_sha}")
            if tunnel_url:
                info["tunnel_url"] = tunnel_url

        return {
            "status": "already_running",
            "port": info["port"],
            "url": f"http://localhost:{info['port']}",
            "tunnelUrl": tunnel_url,
            "worktree_path": info["worktree_path"]
        }

    try:
        work_dir = request.project_path or get_default_working_dir()
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

        # Find available port
        port = TIME_MACHINE_BASE_PORT + len(_time_machine_previews)

        # Determine working directory and command
        if request.dev_script:
            # Custom dev script provided - run from worktree root
            work_path = worktree_path
            command = request.dev_script.replace("{port}", str(port))
            print(f"[TimeMachine] Running custom script for {short_sha}: {command}")
            process = subprocess.Popen(
                command,
                shell=True,
                cwd=str(work_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )
        else:
            # Default: look for frontend directory or use root
            frontend_path = worktree_path / "frontend"
            if frontend_path.exists():
                work_path = frontend_path
            elif (worktree_path / "package.json").exists():
                work_path = worktree_path
            else:
                # Cleanup and error
                subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=git_root, capture_output=True)
                raise HTTPException(status_code=404, detail="No frontend directory or package.json found. Configure a custom dev_script.")

            # Install dependencies if needed
            node_modules = work_path / "node_modules"
            if not node_modules.exists():
                print(f"[TimeMachine] Installing dependencies for {short_sha}...")
                install_result = subprocess.run(
                    ["npm", "install"],
                    cwd=str(work_path),
                    capture_output=True,
                    timeout=180
                )
                if install_result.returncode != 0:
                    subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=git_root, capture_output=True)
                    raise HTTPException(status_code=500, detail="npm install failed")

            # Start dev server
            print(f"[TimeMachine] Starting dev server for {short_sha} on port {port}...")
            process = subprocess.Popen(
                ["npm", "run", "dev", "--", "--port", str(port)],
                cwd=str(work_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True
            )

        # Track it
        _time_machine_previews[short_sha] = {
            "process": process,
            "port": port,
            "worktree_path": str(worktree_path),
            "git_root": git_root,
            "commit_sha": commit_sha,
            "started_at": datetime.now().isoformat(),
            "tunnel_url": None
        }

        # Wait for server to start
        await asyncio.sleep(3)

        # Create ngrok tunnel if requested
        tunnel_url = None
        if request.tunnel:
            tunnel_url = start_ngrok_tunnel(port, f"timemachine-{short_sha}")
            if tunnel_url:
                _time_machine_previews[short_sha]["tunnel_url"] = tunnel_url

        return {
            "status": "started",
            "port": port,
            "url": f"http://localhost:{port}",
            "tunnelUrl": tunnel_url,
            "worktree_path": str(worktree_path),
            "commit_sha": commit_sha
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/time-machine/preview/{sha}")
def delete_time_machine_preview(sha: str):
    """Stop dev server and cleanup worktree."""
    short_sha = sha[:7]

    if short_sha not in _time_machine_previews:
        raise HTTPException(status_code=404, detail="Preview not found")

    info = _time_machine_previews[short_sha]

    # Stop ngrok tunnel if exists
    stop_ngrok_tunnel(f"timemachine-{short_sha}")

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
        git_root = info.get("git_root") or get_git_root(get_default_working_dir())
        if git_root:
            subprocess.run(
                ["git", "worktree", "remove", "--force", info["worktree_path"]],
                cwd=git_root, capture_output=True
            )
    except Exception as e:
        print(f"[TimeMachine] Warning: Failed to remove worktree: {e}")

    del _time_machine_previews[short_sha]
    return {"status": "stopped", "message": "Preview stopped and worktree cleaned up"}


# =============================================================================
# Deploy
# =============================================================================

class DeployConfig(BaseModel):
    """Configuration for deploying a commit."""
    commit_sha: str
    project_path: Optional[str] = None
    cloudflare_project: Optional[str] = None
    build_command: str = "npm run build"
    build_output_dir: str = "build"


def _run(cmd: list, cwd: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command, raise with stderr on failure."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        Path("/tmp/deploy-debug.log").write_text(
            f"CMD: {' '.join(cmd)}\nCWD: {cwd}\nRETURN: {result.returncode}\n"
            f"STDOUT:\n{result.stdout[-1000:]}\nSTDERR:\n{result.stderr[-1000:]}\n"
        )
        raise RuntimeError(result.stderr[:500])
    return result


def _detect_wrangler_config(git_root: str) -> dict:
    """Read wrangler config from project. Returns {name, is_pages}."""
    wrangler_names = ["wrangler.toml", "wrangler.jsonc", "wrangler.json"]
    search_dirs = [Path(git_root), Path(git_root) / "frontend"]
    for search_dir in search_dirs:
        for wname in wrangler_names:
            candidate = search_dir / wname
            if not candidate.exists():
                continue
            try:
                content = candidate.read_text()
                name = None
                is_pages = False
                if wname.endswith(".toml"):
                    for line in content.split('\n'):
                        stripped = line.strip()
                        if not name and stripped.startswith('name'):
                            name = line.split('=')[1].strip().strip('"').strip("'")
                        if 'pages_build_output_dir' in stripped:
                            is_pages = True
                    if not any(l.strip().startswith('main') for l in content.split('\n')):
                        is_pages = True
                else:
                    cleaned = re.sub(r'//.*?$', '', content, flags=re.MULTILINE)
                    parsed = json.loads(cleaned)
                    name = parsed.get("name")
                    is_pages = 'pages_build_output_dir' in parsed or 'main' not in parsed
                if name:
                    return {"name": name, "is_pages": is_pages}
            except Exception:
                pass
    return {}


def _extract_url(output: str) -> Optional[str]:
    """Extract a .pages.dev or .workers.dev URL from wrangler output."""
    for line in output.split('\n'):
        line = line.strip()
        if 'https://' in line and ('.workers.dev' in line or '.pages.dev' in line):
            start = line.index('https://')
            return line[start:].split()[0].rstrip(')')
    return None


def deploy_commit_to_url(config: DeployConfig) -> str:
    """
    Deploy a specific commit to Cloudflare and return the preview URL.

    1. Validates the commit exists
    2. Detects project type (Pages vs Workers) from wrangler config
    3. Creates a worktree for the commit
    4. Builds if package.json exists
    5. Deploys via wrangler
    6. Returns the preview URL
    """
    short_sha = config.commit_sha[:7]
    git_root = get_git_root(config.project_path or get_default_working_dir())
    if not git_root:
        raise RuntimeError("Not in a git repository")

    # Validate commit
    _run(["git", "cat-file", "-t", config.commit_sha], cwd=git_root, timeout=10)

    # Detect project config
    detected = _detect_wrangler_config(git_root)
    cf_project = config.cloudflare_project or detected.get("name")
    is_pages = detected.get("is_pages", False)
    if not cf_project:
        raise RuntimeError("Could not determine Cloudflare project name. Provide cloudflare_project or add a wrangler config.")
    if not shutil.which("npx"):
        raise RuntimeError("npx not found (needed for wrangler)")

    # Create worktree
    worktrees_dir = Path(git_root) / ".worktrees"
    worktrees_dir.mkdir(exist_ok=True)
    worktree_path = worktrees_dir / f"deploy-{short_sha}"

    try:
        if worktree_path.exists():
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=git_root, capture_output=True)
        _run(["git", "worktree", "add", "--detach", str(worktree_path), config.commit_sha], cwd=git_root)

        # Find build directory
        build_dir = worktree_path
        frontend_path = worktree_path / "frontend"
        if frontend_path.exists() and (frontend_path / "package.json").exists():
            build_dir = frontend_path

        # Build if needed
        has_package_json = (build_dir / "package.json").exists()
        if has_package_json:
            print(f"[Deploy] Installing deps for {short_sha}...")
            _run(["npm", "install"], cwd=str(build_dir), timeout=180)
            print(f"[Deploy] Building {short_sha}...")
            _run(config.build_command.split(), cwd=str(build_dir), timeout=300)

        # Deploy
        print(f"[Deploy] Deploying {short_sha} to '{cf_project}' (pages={is_pages})...")
        if is_pages:
            output_path = str(build_dir / config.build_output_dir) if has_package_json else str(build_dir)
            result = _run(
                ["npx", "wrangler", "pages", "deploy", output_path, "--project-name", cf_project, "--branch", f"preview-{short_sha}", "--commit-dirty=true"],
                cwd=str(build_dir)
            )
        else:
            result = _run(
                ["npx", "wrangler", "versions", "upload", "--preview-alias", short_sha],
                cwd=str(build_dir)
            )

        url = _extract_url(result.stdout + result.stderr)
        print(f"[Deploy] Done: {url or 'no URL detected'}")
        return url

    finally:
        try:
            subprocess.run(["git", "worktree", "remove", "--force", str(worktree_path)], cwd=git_root, capture_output=True)
        except Exception:
            pass


@router.post("/deploy")
async def deploy_commit(request: DeployConfig):
    """Deploy a specific commit and return the preview URL."""
    try:
        Path("/tmp/deploy-debug.log").write_text(f"sha={request.commit_sha} path={request.project_path} cf={request.cloudflare_project}\n")
        url = deploy_commit_to_url(request)
        return {
            "success": True,
            "commit_sha": request.commit_sha,
            "project": request.cloudflare_project or _detect_wrangler_config(
                get_git_root(request.project_path or get_default_working_dir()) or ""
            ).get("name"),
            "url": url,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Deploy timed out")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/time-machine/previews")
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
# AI Suggestions
# =============================================================================

class AISuggestVersionRequest(BaseModel):
    """Request for AI version suggestion."""
    project_id: str
    versions: List[dict]
    tasks: List[dict]


AI_SUGGEST_SYSTEM_PROMPT = """You are a project planning assistant. Analyze the project's versions and tasks to suggest which version the user should focus on next.

Consider:
1. Tasks that are almost complete (prioritize finishing what's started)
2. Task dependencies and logical order
3. Business impact and value delivery
4. Current workload distribution

Respond with JSON only (no markdown code fences):
{
  "versionKey": "the_version_key",
  "versionLabel": "Human Readable Name",
  "reason": "Brief explanation (1-2 sentences) of why this version should be the focus",
  "confidence": 0.0-1.0
}"""


@router.post("/ai/suggest-version")
async def ai_suggest_version(request: AISuggestVersionRequest):
    """Get AI suggestion for which version to work on next."""
    claude_path = shutil.which("claude")
    if not claude_path:
        raise HTTPException(
            status_code=400,
            detail="Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
        )

    if not request.versions:
        raise HTTPException(status_code=400, detail="No versions provided")

    # Build context for AI
    versions_context = []
    for v in request.versions:
        version_tasks = [t for t in request.tasks if t.get('version') == v.get('key')]
        todo_count = len([t for t in version_tasks if t.get('status') == 'todo'])
        in_progress_count = len([t for t in version_tasks if t.get('status') == 'in_progress'])
        done_count = len([t for t in version_tasks if t.get('status') == 'done'])
        total = len(version_tasks)

        versions_context.append({
            "key": v.get('key'),
            "label": v.get('label'),
            "tasks": {
                "total": total,
                "todo": todo_count,
                "inProgress": in_progress_count,
                "done": done_count,
            },
            "percentComplete": round((done_count / total) * 100) if total > 0 else 0,
        })

    user_message = f"""Project versions and their status:
{json.dumps(versions_context, indent=2)}

Which version should be the focus for today? Prioritize versions that have work in progress or are close to completion."""

    full_prompt = f"""{AI_SUGGEST_SYSTEM_PROMPT}

---

{user_message}"""

    try:
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)

        cmd = [
            "claude",
            "-p", full_prompt,
            "--output-format", "json",
            "--dangerously-skip-permissions"
        ]

        result = subprocess.run(
            cmd,
            cwd=get_default_working_dir(),
            env=env,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Claude CLI error: {result.stderr[:200]}"
            )

        output = result.stdout.strip()

        try:
            response_data = json.loads(output)
            if "result" in response_data:
                suggestion_text = response_data["result"]
            else:
                suggestion_text = output
        except json.JSONDecodeError:
            suggestion_text = output

        try:
            if isinstance(suggestion_text, str):
                suggestion = json.loads(suggestion_text)
            else:
                suggestion = suggestion_text
        except json.JSONDecodeError:
            json_match = re.search(r'\{[\s\S]*\}', suggestion_text if isinstance(suggestion_text, str) else output)
            if json_match:
                suggestion = json.loads(json_match.group())
            else:
                raise HTTPException(
                    status_code=500,
                    detail="Could not parse AI response as JSON"
                )

        return {"success": True, "suggestion": suggestion}

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Claude CLI timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# =============================================================================
# Agent Definitions
# =============================================================================

class AgentDefinitionCreate(BaseModel):
    """Request to create an agent definition."""
    name: str
    slug: Optional[str] = None
    description: Optional[str] = ""
    system_prompt: Optional[str] = ""
    color: Optional[str] = "#6366f1"
    icon: Optional[str] = "bot"
    is_default: Optional[bool] = False
    sort_order: Optional[int] = 0
    project_id: Optional[str] = None


class AgentDefinitionUpdate(BaseModel):
    """Request to update an agent definition."""
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    color: Optional[str] = None
    icon: Optional[str] = None
    is_default: Optional[bool] = None
    sort_order: Optional[int] = None


@router.get("/agent-definitions")
def list_agent_definitions(project_id: Optional[str] = None):
    """List all agent definitions."""
    agents = list(_agent_definitions.values())

    if project_id:
        agents = [a for a in agents if a.get("project_id") == project_id or a.get("is_default")]

    agents.sort(key=lambda x: (x.get("sort_order", 0), x.get("created_at", "")))

    return {"success": True, "agents": agents}


@router.post("/agent-definitions")
def create_agent_definition(request: AgentDefinitionCreate):
    """Create a new agent definition."""
    agent_id = str(uuid.uuid4())
    slug = request.slug or _generate_agent_slug(request.name)
    now = datetime.utcnow().isoformat()

    agent = {
        "id": agent_id,
        "name": request.name,
        "slug": slug,
        "description": request.description or "",
        "system_prompt": request.system_prompt or "",
        "color": request.color or "#6366f1",
        "icon": request.icon or "bot",
        "is_default": request.is_default or False,
        "sort_order": request.sort_order or 0,
        "project_id": request.project_id,
        "created_at": now,
        "updated_at": now
    }

    _agent_definitions[agent_id] = agent
    return {"success": True, "agent": agent}


@router.get("/agent-definitions/{agent_id}")
def get_agent_definition(agent_id: str):
    """Get a specific agent definition."""
    agent = _agent_definitions.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent definition not found")
    return {"success": True, "agent": agent}


@router.put("/agent-definitions/{agent_id}")
def update_agent_definition(agent_id: str, request: AgentDefinitionUpdate):
    """Update an agent definition."""
    agent = _agent_definitions.get(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent definition not found")

    update_data = request.dict(exclude_unset=True)
    if "name" in update_data and "slug" not in update_data:
        update_data["slug"] = _generate_agent_slug(update_data["name"])

    for key, value in update_data.items():
        agent[key] = value

    agent["updated_at"] = datetime.utcnow().isoformat()

    return {"success": True, "agent": agent}


@router.delete("/agent-definitions/{agent_id}")
def delete_agent_definition(agent_id: str):
    """Delete an agent definition."""
    if agent_id not in _agent_definitions:
        raise HTTPException(status_code=404, detail="Agent definition not found")

    agent = _agent_definitions[agent_id]
    if agent.get("is_default") and agent["id"].startswith("default-"):
        raise HTTPException(status_code=400, detail="Cannot delete built-in default agents")

    del _agent_definitions[agent_id]
    return {"success": True, "deleted": agent_id}
