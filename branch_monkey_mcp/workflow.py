"""
kompany-workflow — Deterministic workflow CLI for Kompany agents.

A standalone CLI that agents invoke via bash to run multi-step pipelines.
Reads workflow definitions from YAML files, executes steps sequentially,
and returns structured JSON results. Approval gates pause execution and
return context for the agent to create a Kompany Decision.

Usage:
    kompany-workflow run [--file workflow.yml] [--step step-name] [--from step-name]
    kompany-workflow validate [--file workflow.yml]
    kompany-workflow list [--file workflow.yml]

Workflow definition (YAML):
    name: my-pipeline
    working_directory: /path/to/project  # optional, defaults to cwd
    env:                                  # optional, global env vars
      API_KEY: $API_KEY
    steps:
      - name: fetch-data
        run: python fetch.py
        timeout: 60                       # optional, seconds
      - name: process
        run: python process.py
        env:
          MODE: production
      - name: deploy
        run: ./deploy.sh
        approval: required                # halts here, agent creates Decision
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml


DEFAULT_WORKFLOW_FILE = ".kompany/workflow.yml"
DEFAULT_STEP_TIMEOUT = 300  # 5 minutes


def find_workflow_file(file_path=None):
    """Find the workflow definition file."""
    if file_path:
        p = Path(file_path)
        if p.exists():
            return p
        raise FileNotFoundError(f"Workflow file not found: {file_path}")

    # Search up from cwd
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        candidate = parent / DEFAULT_WORKFLOW_FILE
        if candidate.exists():
            return candidate
        # Also check workflow.yml at root
        candidate = parent / "workflow.yml"
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        f"No workflow file found. Create {DEFAULT_WORKFLOW_FILE} or pass --file"
    )


def load_workflow(file_path=None):
    """Load and validate a workflow definition from YAML."""
    path = find_workflow_file(file_path)

    with open(path) as f:
        wf = yaml.safe_load(f)

    if not isinstance(wf, dict):
        raise ValueError(f"Invalid workflow file: expected a YAML mapping, got {type(wf).__name__}")

    if "steps" not in wf or not isinstance(wf["steps"], list):
        raise ValueError("Workflow must have a 'steps' list")

    # Validate steps
    for i, step in enumerate(wf["steps"]):
        if not isinstance(step, dict):
            raise ValueError(f"Step {i} must be a mapping")
        if "name" not in step:
            raise ValueError(f"Step {i} missing 'name'")
        if "run" not in step and step.get("approval") != "required":
            raise ValueError(f"Step '{step['name']}' missing 'run' command")

    wf.setdefault("name", path.stem)
    wf.setdefault("working_directory", str(path.parent.parent))  # parent of .kompany/
    wf["_file"] = str(path)

    return wf


def resolve_env(env_dict, parent_env=None):
    """Resolve environment variables, expanding $VAR references."""
    base = dict(os.environ)
    if parent_env:
        base.update(parent_env)

    resolved = {}
    for key, val in (env_dict or {}).items():
        val = str(val)
        if val.startswith("$"):
            var_name = val[1:]
            resolved[key] = base.get(var_name, "")
        else:
            resolved[key] = val

    return resolved


def run_step(step, global_env, working_directory, prev_results):
    """Execute a single workflow step. Returns step result dict."""
    name = step["name"]
    command = step.get("run", "")
    step_env = step.get("env", {})
    timeout = step.get("timeout", DEFAULT_STEP_TIMEOUT)

    # Build environment — inherits parent env (including $AGENT_PROMPT if set)
    env = dict(os.environ)
    env.update(resolve_env(global_env))
    env.update(resolve_env(step_env, global_env))

    # Inject previous step results as env vars
    for prev in prev_results:
        safe_name = prev["name"].upper().replace("-", "_").replace(" ", "_")
        env[f"STEP_{safe_name}_STATUS"] = prev["status"]
        env[f"STEP_{safe_name}_EXIT_CODE"] = str(prev.get("exit_code", ""))
        # Truncate stdout to avoid env var size limits
        stdout = prev.get("stdout", "")
        if len(stdout) > 4096:
            stdout = stdout[:4096] + "\n...(truncated)"
        env[f"STEP_{safe_name}_STDOUT"] = stdout

    # Resolve working directory
    cwd = step.get("working_directory", working_directory)
    cwd = os.path.expanduser(cwd)

    start = time.time()

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            env=env,
        )
        duration_ms = int((time.time() - start) * 1000)

        return {
            "name": name,
            "status": "success" if result.returncode == 0 else "failed",
            "exit_code": result.returncode,
            "stdout": result.stdout[-8192:] if len(result.stdout) > 8192 else result.stdout,
            "stderr": result.stderr[-4096:] if len(result.stderr) > 4096 else result.stderr,
            "duration_ms": duration_ms,
        }

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "name": name,
            "status": "timeout",
            "exit_code": -1,
            "stdout": "",
            "stderr": f"Step timed out after {timeout}s",
            "duration_ms": duration_ms,
        }

    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        return {
            "name": name,
            "status": "error",
            "exit_code": -1,
            "stdout": "",
            "stderr": str(e),
            "duration_ms": duration_ms,
        }


def run_workflow(wf, from_step=None, single_step=None):
    """Execute a workflow. Returns structured JSON result."""
    steps = wf["steps"]
    global_env = wf.get("env", {})
    working_directory = wf.get("working_directory", os.getcwd())
    results = []
    total_start = time.time()

    # Determine which steps to run
    start_index = 0
    if from_step:
        found = False
        for i, step in enumerate(steps):
            if step["name"] == from_step:
                start_index = i
                found = True
                break
        if not found:
            return {
                "workflow": wf.get("name", "unknown"),
                "status": "error",
                "error": f"Step '{from_step}' not found",
                "steps": [],
                "duration_ms": 0,
            }

    for i, step in enumerate(steps):
        name = step["name"]

        # Skip steps before start point
        if i < start_index:
            results.append({"name": name, "status": "skipped"})
            continue

        # Single step mode
        if single_step and name != single_step:
            results.append({"name": name, "status": "skipped"})
            continue

        # Approval gate — halt before this step
        if step.get("approval") == "required":
            # If we're resuming FROM this step, skip the gate
            if from_step != name:
                results.append({
                    "name": name,
                    "status": "pending_approval",
                    "approval": {
                        "step": name,
                        "description": step.get("description", f"Step '{name}' requires approval before execution"),
                        "resume_from": name,
                    },
                })
                total_duration = int((time.time() - total_start) * 1000)
                return {
                    "workflow": wf.get("name", "unknown"),
                    "file": wf.get("_file", ""),
                    "status": "needs_approval",
                    "steps": results,
                    "duration_ms": total_duration,
                    "resume_from": name,
                    "approval": {
                        "step": name,
                        "description": step.get("description", f"Approval needed for: {name}"),
                    },
                }

        # Check condition
        condition = step.get("condition")
        if condition:
            # Simple condition: check if previous step succeeded
            if condition.startswith("step.") and condition.endswith(".success"):
                ref_name = condition[5:-8]
                ref_result = next((r for r in results if r["name"] == ref_name), None)
                if not ref_result or ref_result["status"] != "success":
                    results.append({"name": name, "status": "skipped", "reason": f"Condition not met: {condition}"})
                    continue

        # Run the step
        if step.get("run"):
            step_result = run_step(step, global_env, working_directory, results)
            results.append(step_result)

            # Stop on failure unless continue_on_error
            if step_result["status"] != "success" and not step.get("continue_on_error"):
                total_duration = int((time.time() - total_start) * 1000)
                return {
                    "workflow": wf.get("name", "unknown"),
                    "file": wf.get("_file", ""),
                    "status": "failed",
                    "failed_step": name,
                    "steps": results,
                    "duration_ms": total_duration,
                }
        else:
            results.append({"name": name, "status": "skipped", "reason": "No command"})

        if single_step:
            break

    total_duration = int((time.time() - total_start) * 1000)
    return {
        "workflow": wf.get("name", "unknown"),
        "file": wf.get("_file", ""),
        "status": "completed",
        "steps": results,
        "duration_ms": total_duration,
    }


def cmd_run(args):
    """Run a workflow."""
    try:
        wf = load_workflow(args.file)
    except (FileNotFoundError, ValueError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)

    result = run_workflow(wf, from_step=args.resume_from, single_step=args.step)
    print(json.dumps(result, indent=2))

    if result["status"] == "failed":
        sys.exit(1)
    elif result["status"] == "error":
        sys.exit(2)


def cmd_validate(args):
    """Validate a workflow file."""
    try:
        wf = load_workflow(args.file)
        steps = wf["steps"]
        approval_gates = [s["name"] for s in steps if s.get("approval") == "required"]

        result = {
            "valid": True,
            "workflow": wf.get("name", "unknown"),
            "file": wf.get("_file", ""),
            "step_count": len(steps),
            "steps": [s["name"] for s in steps],
            "approval_gates": approval_gates,
        }
        print(json.dumps(result, indent=2))

    except (FileNotFoundError, ValueError) as e:
        print(json.dumps({"valid": False, "error": str(e)}))
        sys.exit(1)


def cmd_list(args):
    """List steps in a workflow."""
    try:
        wf = load_workflow(args.file)
    except (FileNotFoundError, ValueError) as e:
        print(json.dumps({"status": "error", "error": str(e)}))
        sys.exit(1)

    steps = []
    for s in wf["steps"]:
        info = {"name": s["name"]}
        if s.get("run"):
            info["command"] = s["run"]
        if s.get("approval"):
            info["approval"] = s["approval"]
        if s.get("description"):
            info["description"] = s["description"]
        if s.get("timeout"):
            info["timeout"] = s["timeout"]
        if s.get("condition"):
            info["condition"] = s["condition"]
        steps.append(info)

    print(json.dumps({
        "workflow": wf.get("name", "unknown"),
        "file": wf.get("_file", ""),
        "steps": steps,
    }, indent=2))


def cmd_llm(args):
    """Run a prompt through the configured LLM CLI and print the result."""
    from .bridge_and_local_actions.cli_providers import get_provider, get_default_cli

    # Read prompt from --prompt arg or stdin
    prompt = args.prompt
    if not prompt:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        if not prompt:
            print("Error: provide a prompt via --prompt or stdin", file=sys.stderr)
            sys.exit(1)

    # Resolve provider
    cli_name = args.cli or get_default_cli()

    try:
        provider = get_provider(cli_name)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if not provider.is_available():
        print(f"Error: {provider.display_name} is not installed. {provider.install_hint}", file=sys.stderr)
        sys.exit(1)

    system_prompt = args.system_prompt or None
    cli_cmd = provider.build_text_command(prompt, system_prompt=system_prompt)

    env = os.environ.copy()
    for key in cli_cmd.env_overrides:
        env.pop(key, None)
    env.pop("CLAUDECODE", None)
    if cli_cmd.env_inject:
        env.update(cli_cmd.env_inject)

    cwd = args.cwd or os.getcwd()

    try:
        result = subprocess.run(
            cli_cmd.args,
            capture_output=True,
            text=True,
            timeout=args.timeout or 300,
            cwd=cwd,
            env=env,
        )
        # Print stdout (the LLM response)
        if result.stdout:
            print(result.stdout.rstrip())
        if result.returncode != 0 and result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(result.returncode)
    except subprocess.TimeoutExpired:
        print(f"Error: LLM call timed out after {args.timeout or 300}s", file=sys.stderr)
        sys.exit(1)


def _get_api_client():
    """Load auth and return (api_url, headers) for Kompany API calls."""
    import requests as req
    token_path = Path.home() / ".branch-monkey" / "token.json"
    if not token_path.exists():
        print("Error: not authenticated. Run branch-monkey-mcp first.", file=sys.stderr)
        sys.exit(1)

    with open(token_path) as f:
        token_data = json.load(f)

    api_url = token_data.get("api_url", "https://kompany.dev")
    headers = {
        "Authorization": f"Bearer {token_data.get('access_token', '')}",
        "X-Org-Id": token_data.get("org_id", ""),
        "Content-Type": "application/json",
    }
    return api_url, headers, req


def cmd_agent_prompt(args):
    """Fetch the agent's system_prompt for a machine and print to stdout."""
    api_url, headers, req = _get_api_client()

    resp = req.get(f"{api_url}/api/machines/{args.machine_id}", headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"Error fetching machine: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    machine = resp.json().get("machine", {})
    agent_id = machine.get("agent_id")
    if not agent_id:
        print("Error: machine has no agent assigned", file=sys.stderr)
        sys.exit(1)

    resp = req.get(f"{api_url}/api/agent-definitions/{agent_id}", headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"Error fetching agent: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    system_prompt = resp.json().get("agent", {}).get("system_prompt", "")
    if not system_prompt:
        print("Error: agent has no system_prompt", file=sys.stderr)
        sys.exit(1)

    print(system_prompt)


def cmd_save_output(args):
    """Save workflow output as a Kompany context."""
    api_url, headers, req = _get_api_client()

    content = args.content
    if not content:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        if not content:
            print("Error: provide content via --content or stdin", file=sys.stderr)
            sys.exit(1)

    payload = {
        "name": args.name or "Workflow Output",
        "content": content,
        "context_type": args.type or "general",
    }
    if args.project_id:
        payload["project_id"] = args.project_id

    resp = req.post(f"{api_url}/api/contexts", headers=headers, json=payload, timeout=15)
    if resp.status_code not in (200, 201):
        print(f"Error saving context: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    ctx = resp.json().get("context", resp.json())
    print(json.dumps({"saved": True, "context_id": ctx.get("id"), "name": payload["name"]}))


def cmd_update_memory(args):
    """Update or append to a machine's memory context."""
    api_url, headers, req = _get_api_client()

    content = args.content
    if not content:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        if not content:
            print("Error: provide content via --content or stdin", file=sys.stderr)
            sys.exit(1)

    if args.context_id:
        # Direct update
        context_id = args.context_id
    elif args.machine_id:
        # Find memory context for this machine
        resp = req.get(f"{api_url}/api/machines/{args.machine_id}", headers=headers, timeout=15)
        if resp.status_code != 200:
            print(f"Error fetching machine: {resp.status_code}", file=sys.stderr)
            sys.exit(1)
        machine_name = resp.json().get("machine", {}).get("name", "")
        memory_name = f"Machine Memory: {machine_name}"

        # Search for existing memory context
        resp = req.get(f"{api_url}/api/contexts?search={memory_name}", headers=headers, timeout=15)
        contexts = resp.json().get("contexts", [])
        memory = next((c for c in contexts if c.get("context_type") == "memory" and memory_name.lower() in c.get("name", "").lower()), None)

        if not memory:
            print(f"Error: no memory context found for machine {args.machine_id}", file=sys.stderr)
            sys.exit(1)
        context_id = memory["id"]
    else:
        print("Error: provide --machine-id or --context-id", file=sys.stderr)
        sys.exit(1)

    # Append or replace
    if args.append:
        resp = req.get(f"{api_url}/api/contexts/{context_id}", headers=headers, timeout=15)
        existing = resp.json().get("context", {}).get("content", "")
        content = existing.rstrip() + "\n\n" + content

    resp = req.put(f"{api_url}/api/contexts/{context_id}", headers=headers, json={"content": content}, timeout=15)
    if resp.status_code != 200:
        print(f"Error updating memory: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"updated": True, "context_id": context_id}))


def cmd_update_metric(args):
    """Update a metric value on a machine."""
    api_url, headers, req = _get_api_client()

    payload = {"metric_name": args.metric_name}
    if args.value is not None:
        payload["value"] = args.value
    if args.increment is not None:
        # Fetch current value and add
        resp = req.get(f"{api_url}/api/machines/{args.machine_id}/metrics", headers=headers, timeout=15)
        metrics = resp.json().get("metrics", [])
        current = next((m for m in metrics if m.get("metric_name") == args.metric_name), None)
        current_val = current.get("value", 0) if current else 0
        payload["value"] = current_val + args.increment

    resp = req.put(f"{api_url}/api/machines/{args.machine_id}/metrics", headers=headers, json=payload, timeout=15)
    if resp.status_code != 200:
        print(f"Error updating metric: {resp.status_code}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"updated": True, "machine_id": args.machine_id, "metric": args.metric_name, "value": payload.get("value")}))


def cmd_log(args):
    """Log a workflow run to the machine's run history."""
    api_url, headers, req = _get_api_client()

    content = args.content
    if not content:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        if not content:
            print("Error: provide content via --content or stdin", file=sys.stderr)
            sys.exit(1)

    payload = {"content": content}
    if args.task_id:
        payload["task_id"] = args.task_id

    resp = req.post(f"{api_url}/api/task-logs", headers=headers, json=payload, timeout=15)
    if resp.status_code in (200, 201):
        print(json.dumps({"logged": True}))
    else:
        # Fallback: save as notification
        notif_payload = {
            "title": args.title or "Workflow Log",
            "body": content[:500],
            "type": "info",
        }
        if args.machine_id:
            notif_payload["machine_id"] = args.machine_id
        req.post(f"{api_url}/api/notifications", headers=headers, json=notif_payload, timeout=15)
        print(json.dumps({"logged": True, "method": "notification"}))


def main():
    parser = argparse.ArgumentParser(
        prog="kompany-workflow",
        description="Deterministic workflow runner for Kompany agents",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # run
    run_parser = subparsers.add_parser("run", help="Execute a workflow")
    run_parser.add_argument("--file", "-f", help="Path to workflow YAML file")
    run_parser.add_argument("--step", "-s", help="Run only this specific step")
    run_parser.add_argument("--from", dest="resume_from", help="Resume from this step (skip prior steps)")
    run_parser.set_defaults(func=cmd_run)

    # validate
    val_parser = subparsers.add_parser("validate", help="Validate a workflow file")
    val_parser.add_argument("--file", "-f", help="Path to workflow YAML file")
    val_parser.set_defaults(func=cmd_validate)

    # list
    list_parser = subparsers.add_parser("list", help="List workflow steps")
    list_parser.add_argument("--file", "-f", help="Path to workflow YAML file")
    list_parser.set_defaults(func=cmd_list)

    # agent-prompt
    ap_parser = subparsers.add_parser("agent-prompt", help="Fetch the agent prompt for a machine")
    ap_parser.add_argument("machine_id", help="Machine UUID")
    ap_parser.set_defaults(func=cmd_agent_prompt)

    # llm
    llm_parser = subparsers.add_parser("llm", help="Run a prompt through the configured LLM")
    llm_parser.add_argument("--prompt", "-p", help="The prompt (or pipe via stdin)")
    llm_parser.add_argument("--system-prompt", "-s", help="System prompt")
    llm_parser.add_argument("--cli", help="CLI provider: claude, codex, grok (default: from config)")
    llm_parser.add_argument("--cwd", help="Working directory")
    llm_parser.add_argument("--timeout", type=int, default=300, help="Timeout in seconds (default: 300)")
    llm_parser.set_defaults(func=cmd_llm)

    # save-output
    so_parser = subparsers.add_parser("save-output", help="Save output as a Kompany context")
    so_parser.add_argument("--content", "-c", help="Content to save (or pipe via stdin)")
    so_parser.add_argument("--name", "-n", help="Context name (default: Workflow Output)")
    so_parser.add_argument("--type", "-t", default="general", help="Context type (default: general)")
    so_parser.add_argument("--project-id", help="Project UUID (uses focused project if omitted)")
    so_parser.set_defaults(func=cmd_save_output)

    # update-memory
    mem_parser = subparsers.add_parser("update-memory", help="Update a machine's memory context")
    mem_parser.add_argument("--machine-id", "-m", help="Machine UUID (looks up its memory context)")
    mem_parser.add_argument("--context-id", help="Direct context UUID to update")
    mem_parser.add_argument("--content", "-c", help="Content (or pipe via stdin)")
    mem_parser.add_argument("--append", "-a", action="store_true", help="Append to existing content instead of replacing")
    mem_parser.set_defaults(func=cmd_update_memory)

    # update-metric
    met_parser = subparsers.add_parser("update-metric", help="Update a metric on a machine")
    met_parser.add_argument("machine_id", help="Machine UUID")
    met_parser.add_argument("metric_name", help="Metric name")
    met_parser.add_argument("--value", type=float, help="Set to this value")
    met_parser.add_argument("--increment", type=float, help="Increment by this amount")
    met_parser.set_defaults(func=cmd_update_metric)

    # log
    log_parser = subparsers.add_parser("log", help="Log workflow activity")
    log_parser.add_argument("--content", "-c", help="Log content (or pipe via stdin)")
    log_parser.add_argument("--machine-id", "-m", help="Machine UUID")
    log_parser.add_argument("--task-id", help="Task ID to log against")
    log_parser.add_argument("--title", help="Log title")
    log_parser.set_defaults(func=cmd_log)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == "__main__":
    main()
