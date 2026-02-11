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
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..config import get_default_working_dir
from ..git_utils import get_git_root
from ..dev_server_manager import manager as dev_manager

router = APIRouter()

# Track time machine previews
_time_machine_previews: Dict[str, dict] = {}
_time_machine_tunnels: Dict[str, object] = {}  # Track ngrok tunnels for time machine
TIME_MACHINE_BASE_PORT = 6100

# Agent definitions are stored in the cloud database (public.agents table).
# The local server does NOT maintain its own store — all CRUD goes through
# the cloud API via MCP tools. The local apply-agent endpoint receives the
# system_prompt and allowed_tools directly from the caller.


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
            tunnel_url = dev_manager.start_tunnel(info["port"], f"timemachine-{short_sha}")
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
            tunnel_url = dev_manager.start_tunnel(port, f"timemachine-{short_sha}")
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
    dev_manager.stop_tunnel(f"timemachine-{short_sha}")

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


def _run(cmd: list, cwd: str, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run a command, raise with stderr on failure."""
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[:500])
    return result


def _detect_wrangler_config(git_root: str) -> dict:
    """Auto-detect deploy config from wrangler files. Returns partial deploy config dict."""
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
                    # Determine build_dir from where wrangler config was found
                    build_dir = None
                    if search_dir != Path(git_root):
                        build_dir = str(search_dir.relative_to(git_root))
                    return {
                        "platform": "cloudflare-pages" if is_pages else "cloudflare-workers",
                        "project": name,
                        "build_command": "npm run build",
                        "build_output_dir": "build",
                        **({"build_dir": build_dir} if build_dir else {}),
                    }
            except Exception:
                pass
    return {}


def _read_deploy_config(git_root: str) -> dict:
    """Read .kompany/cerver_deploy.json from git root.

    If missing, fall back to auto-detecting from wrangler config and write
    .kompany/cerver_deploy.json for next time. Returns parsed config dict.
    """
    config_file = Path(git_root) / ".kompany" / "cerver_deploy.json"
    if config_file.exists():
        try:
            return json.loads(config_file.read_text())
        except Exception:
            pass

    # Fall back to auto-detection
    detected = _detect_wrangler_config(git_root)
    if not detected:
        raise RuntimeError(
            "No .kompany/cerver_deploy.json found and could not auto-detect from wrangler config. "
            "Create .kompany/cerver_deploy.json with platform, project, build_command, build_output_dir."
        )

    # Write the detected config for next time
    config_file.parent.mkdir(exist_ok=True)
    config_file.write_text(json.dumps(detected, indent=2))
    print(f"[Deploy] Auto-detected config and wrote {config_file}")
    return detected


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
    Deploy a specific commit using config from .kompany/cerver_deploy.json and return the URL.

    1. Reads deploy config from .kompany/cerver_deploy.json (auto-detects if missing)
    2. Creates a worktree for the commit
    3. Builds based on config
    4. Deploys based on platform
    5. Saves URL to .kompany/deploys.json
    6. Returns the preview URL
    """
    short_sha = config.commit_sha[:7]
    git_root = get_git_root(config.project_path or get_default_working_dir())
    if not git_root:
        raise RuntimeError("Not in a git repository")

    # Validate commit
    _run(["git", "cat-file", "-t", config.commit_sha], cwd=git_root, timeout=10)

    # Read deploy config
    deploy_cfg = _read_deploy_config(git_root)
    platform = deploy_cfg.get("platform", "cloudflare-pages")
    project_name = deploy_cfg.get("project")
    build_command = deploy_cfg.get("build_command", "npm run build")
    build_output_dir = deploy_cfg.get("build_output_dir", "build")
    cfg_build_dir = deploy_cfg.get("build_dir")  # e.g. "frontend"

    if not project_name:
        raise RuntimeError("No 'project' specified in .kompany/cerver_deploy.json")
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

        # Determine build directory from config
        if cfg_build_dir:
            build_dir = worktree_path / cfg_build_dir
        else:
            build_dir = worktree_path

        # Build
        has_package_json = (build_dir / "package.json").exists()
        if has_package_json:
            print(f"[Deploy] Installing deps for {short_sha}...")
            _run(["npm", "install"], cwd=str(build_dir), timeout=180)
            print(f"[Deploy] Building {short_sha}...")
            _run(build_command.split(), cwd=str(build_dir), timeout=300)

        # Deploy based on platform
        print(f"[Deploy] Deploying {short_sha} to '{project_name}' (platform={platform})...")
        if platform == "cloudflare-pages":
            output_path = str(build_dir / build_output_dir) if has_package_json else str(build_dir)
            result = _run(
                ["npx", "wrangler", "pages", "deploy", output_path, "--project-name", project_name, "--branch", f"preview-{short_sha}", "--commit-dirty=true"],
                cwd=str(build_dir)
            )
        elif platform == "cloudflare-workers":
            result = _run(
                ["npx", "wrangler", "versions", "upload", "--preview-alias", short_sha],
                cwd=str(build_dir)
            )
        else:
            raise RuntimeError(f"Unsupported platform: {platform}")

        url = _extract_url(result.stdout + result.stderr)
        print(f"[Deploy] Done: {url or 'no URL detected'}")

        # Save deploy info to .kompany/deploys.json
        if url:
            deploys_file = Path(git_root) / ".kompany" / "deploys.json"
            deploys_file.parent.mkdir(exist_ok=True)
            deploys = {}
            if deploys_file.exists():
                try:
                    deploys = json.loads(deploys_file.read_text())
                except Exception:
                    pass
            deploys[short_sha] = {
                "url": url,
                "commit_sha": config.commit_sha,
                "project": project_name,
                "platform": platform,
                "deployed_at": datetime.utcnow().isoformat() + "Z",
            }
            deploys_file.write_text(json.dumps(deploys, indent=2))

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
        url = deploy_commit_to_url(request)
        git_root = get_git_root(request.project_path or get_default_working_dir())
        deploy_cfg = _read_deploy_config(git_root) if git_root else {}
        return {
            "success": True,
            "commit_sha": request.commit_sha,
            "project": deploy_cfg.get("project"),
            "platform": deploy_cfg.get("platform"),
            "url": url,
        }
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Deploy timed out")
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/deploys")
def get_deploys(path: Optional[str] = None):
    """Get saved deploy URLs from .kompany/deploys.json."""
    git_root = get_git_root(path or get_default_working_dir())
    if not git_root:
        return {}
    deploys_file = Path(git_root) / ".kompany" / "deploys.json"
    if not deploys_file.exists():
        return {}
    try:
        return json.loads(deploys_file.read_text())
    except Exception:
        return {}


@router.post("/deploy/init")
def deploy_init(path: Optional[str] = None):
    """Auto-detect deploy platform and write .kompany/cerver_deploy.json."""
    git_root = get_git_root(path or get_default_working_dir())
    if not git_root:
        raise HTTPException(status_code=404, detail="Not in a git repository")

    config_file = Path(git_root) / ".kompany" / "cerver_deploy.json"
    if config_file.exists():
        try:
            existing = json.loads(config_file.read_text())
            return {"success": True, "config": existing, "created": False}
        except Exception:
            pass

    detected = _detect_wrangler_config(git_root)
    if not detected:
        raise HTTPException(
            status_code=404,
            detail="Could not auto-detect deploy config. Create .kompany/cerver_deploy.json manually."
        )

    config_file.parent.mkdir(exist_ok=True)
    config_file.write_text(json.dumps(detected, indent=2))
    return {"success": True, "config": detected, "created": True}


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

# =============================================================================
# Apply Agent (run Claude with agent system prompt)
# =============================================================================

class ApplyAgentRequest(BaseModel):
    """Request to apply an agent to execute instructions.

    The caller (MCP tool or relay) fetches the agent from the cloud database
    and passes system_prompt + allowed_tools directly — no local lookup needed.
    """
    instructions: str
    system_prompt: str = ""
    allowed_tools: Optional[List[str]] = None
    working_dir: Optional[str] = None
    agent_slug: Optional[str] = None  # For logging only


@router.post("/apply-agent")
async def apply_agent(request: ApplyAgentRequest):
    """Run Claude CLI with an agent's system prompt and user instructions.

    The system_prompt and allowed_tools are provided by the caller, who
    already fetched them from the cloud database.
    """
    claude_path = shutil.which("claude")
    if not claude_path:
        raise HTTPException(
            status_code=400,
            detail="Claude Code CLI not found. Install with: npm install -g @anthropic-ai/claude-code"
        )

    # Build prompt: system prompt + instructions
    full_prompt = request.instructions
    if request.system_prompt:
        full_prompt = f"""{request.system_prompt}

---

{request.instructions}"""

    work_dir = request.working_dir or get_default_working_dir()

    try:
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)

        cmd = [
            "claude",
            "-p", full_prompt,
            "--output-format", "json",
            "--dangerously-skip-permissions"
        ]

        # Restrict tools if agent has allowed_tools configured
        if request.allowed_tools is not None and len(request.allowed_tools) > 0:
            cmd.extend(["--allowedTools", ",".join(request.allowed_tools)])

        result = subprocess.run(
            cmd,
            cwd=work_dir,
            env=env,
            capture_output=True,
            text=True,
            timeout=120
        )

        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Claude CLI error: {result.stderr[:500]}"
            )

        output = result.stdout.strip()

        # Parse JSON output from Claude
        try:
            response_data = json.loads(output)
            if "result" in response_data:
                output_text = response_data["result"]
            else:
                output_text = output
        except json.JSONDecodeError:
            output_text = output

        return {
            "success": True,
            "agent_slug": request.agent_slug,
            "output": output_text
        }

    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Agent execution timed out (120s)")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
