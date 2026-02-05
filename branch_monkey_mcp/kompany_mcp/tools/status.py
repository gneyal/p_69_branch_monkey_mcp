"""
Status and authentication tools.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import requests

from .. import state
from ..auth import get_token_path, clear_token, device_code_flow, save_token
from ..api_client import api_get, reset_session
from ..mcp_app import mcp

LOCAL_SERVER_URL = "http://localhost:18081"
CONNECTION_LOG_FILE = Path.home() / ".kompany" / "connection_events.log"


@mcp.tool()
def monkey_status() -> str:
    """Get the current status of Branch Monkey."""
    try:
        token_path = get_token_path()
        auth_status = "Device Token" if token_path.exists() else "API Key"

        if not state.CURRENT_PROJECT_ID:
            # No project focused - show guidance
            return f"""# Branch Monkey Status

**Connected to:** {state.API_URL}
**Auth:** {auth_status}
**Project Focus:** ⚠️ None

## Getting Started

To use Branch Monkey, you must first select a project to work on:

1. Run `monkey_project_list` to see available projects
2. Run `monkey_project_focus <project_id>` to set the active project

All tasks, machines, versions, team members, and domains are scoped to the focused project.
"""

        # Get counts filtered by project
        task_endpoint = f"/api/tasks?project_id={state.CURRENT_PROJECT_ID}"
        tasks = api_get(task_endpoint)
        task_count = len(tasks.get("tasks", []))

        version_endpoint = f"/api/versions?project_id={state.CURRENT_PROJECT_ID}"
        versions = api_get(version_endpoint)
        version_count = len(versions.get("versions", []))

        machine_endpoint = f"/api/machines?project_id={state.CURRENT_PROJECT_ID}"
        machines = api_get(machine_endpoint)
        machine_count = len(machines.get("machines", []))

        return f"""# Branch Monkey Status

**Connected to:** {state.API_URL}
**Auth:** {auth_status}
**Project Focus:** 🎯 **{state.CURRENT_PROJECT_NAME}**

## Project Stats
- **Tasks:** {task_count}
- **Machines:** {machine_count}
- **Versions:** {version_count}

## Available Commands
- `monkey_task_list` - List tasks for this project
- `monkey_task_create` - Create a new task
- `monkey_machine_list` - List machines
- `monkey_version_list` - List versions
- `monkey_team_list` - List team members
- `monkey_project_clear` - Clear project focus
"""
    except Exception as e:
        return f"Error connecting to API: {str(e)}"


@mcp.tool()
def monkey_logout() -> str:
    """Log out and clear stored authentication token."""
    try:
        clear_token()
        return """# Logged Out

Your authentication token has been cleared.
On next use, you'll be prompted to approve the device again via your browser.

To re-authenticate now, use `monkey_login`."""
    except Exception as e:
        return f"Error logging out: {str(e)}"


@mcp.tool()
def monkey_login() -> str:
    """Force re-authentication via browser approval. Use this if you're having auth issues."""
    try:
        # Clear existing token
        clear_token()

        # Reset session to clear cached auth
        reset_session()

        # Run device code flow
        auth_result = device_code_flow(state.API_URL)

        if auth_result:
            state.API_KEY = auth_result.get("access_token")
            state.ORG_ID = auth_result.get("org_id")
            save_token(state.API_KEY, state.API_URL, state.ORG_ID)
            return """# Login Successful

You are now authenticated with Branch Monkey Cloud.
Your token has been saved for future sessions.

Use `monkey_status` to verify your connection."""
        else:
            return """# Login Failed

Authentication was not completed. This could be because:
- The browser approval was denied
- The code expired (15 minute timeout)
- Network connectivity issues

Please try again with `monkey_login`."""
    except Exception as e:
        return f"""# Login Error

Failed to authenticate: {str(e)}

If this persists, check:
1. Network connectivity to {state.API_URL}
2. That you can access {state.API_URL}/approve in your browser
3. Try logging out with `monkey_logout` and restart Claude Code"""


@mcp.tool()
def monkey_diagnose(hours: float = 24.0) -> str:
    """
    Diagnose relay connection issues. Shows live diagnostics and analyzes
    the connection event log for patterns like frequent disconnects,
    heartbeat failures, and stability trends.

    Args:
        hours: How many hours of log history to analyze (default: 24)
    """
    sections = []

    # --- Section 1: Live diagnostics from the local relay server ---
    live_diag = _fetch_live_diagnostics()
    if live_diag:
        sections.append(_format_live_diagnostics(live_diag))
    else:
        sections.append("## Live Relay\n\nLocal relay server not reachable at localhost:18081. Is `branch-monkey-relay` running?")

    # --- Section 2: Historical log analysis ---
    events = _read_log_events(hours)
    if events:
        sections.append(_analyze_events(events, hours))
    else:
        sections.append(f"## History\n\nNo events found in the last {hours:.0f}h. Log file: `{CONNECTION_LOG_FILE}`")

    return "# Connection Diagnostics\n\n" + "\n\n---\n\n".join(sections)


def _fetch_live_diagnostics() -> dict | None:
    """Fetch diagnostics from the local relay server."""
    try:
        resp = requests.get(f"{LOCAL_SERVER_URL}/api/relay/diagnostics", timeout=3)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def _format_live_diagnostics(d: dict) -> str:
    """Format the live diagnostics response into readable markdown."""
    conn = d.get("connection", {})
    stats = d.get("stats", {})
    hb = d.get("heartbeat", {})
    relay = d.get("relay_status", {})
    last_fail = d.get("last_failure")

    connected = relay.get("connected", False)
    status_icon = "ON" if connected else "OFF"

    uptime = conn.get("uptime_seconds")
    uptime_str = _format_duration(uptime) if uptime is not None else "N/A"

    hb_rate = hb.get("success_rate_pct")
    hb_str = f"{hb_rate}%" if hb_rate is not None else "N/A"

    lines = [
        f"## Live Relay — {status_icon}",
        "",
        f"| Metric | Value |",
        f"|--------|-------|",
        f"| Status | {'Connected' if connected else 'Disconnected'} |",
        f"| Machine | {relay.get('machine_name', 'N/A')} |",
        f"| Uptime | {uptime_str} |",
        f"| Connects | {stats.get('total_connects', 0)} |",
        f"| Disconnects | {stats.get('total_disconnects', 0)} |",
        f"| Reconnects | {stats.get('total_reconnects', 0)} |",
        f"| Heartbeat OK/Fail | {hb.get('total_ok', 0)}/{hb.get('total_failed', 0)} ({hb_str}) |",
        f"| Session start | {_short_ts(conn.get('session_start'))} |",
    ]

    if last_fail:
        lines.append("")
        lines.append(f"**Last failure:** `{last_fail.get('event')}` at {_short_ts(last_fail.get('ts'))}")
        if last_fail.get("error"):
            lines.append(f"  Error: `{last_fail['error']}`")
        if last_fail.get("reason"):
            lines.append(f"  Reason: `{last_fail['reason']}`")

    return "\n".join(lines)


def _read_log_events(hours: float) -> list[dict]:
    """Read events from the log file within the given time window."""
    if not CONNECTION_LOG_FILE.exists():
        return []

    cutoff = datetime.now(timezone.utc).timestamp() - (hours * 3600)
    events = []

    try:
        # Also check rotated file for longer time windows
        for path in [CONNECTION_LOG_FILE.with_suffix(".log.1"), CONNECTION_LOG_FILE]:
            if not path.exists():
                continue
            with open(path, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        ts_str = entry.get("ts", "")
                        ts = datetime.fromisoformat(ts_str).timestamp()
                        if ts >= cutoff:
                            events.append(entry)
                    except (json.JSONDecodeError, ValueError):
                        continue
    except Exception:
        pass

    return events


def _analyze_events(events: list[dict], hours: float) -> str:
    """Analyze log events and produce a pattern report."""
    lines = [f"## History — last {hours:.0f}h ({len(events)} events)"]

    # Count by event type
    counts: dict[str, int] = {}
    for e in events:
        ev = e.get("event", "unknown")
        counts[ev] = counts.get(ev, 0) + 1

    # Event summary table
    if counts:
        lines.append("")
        lines.append("| Event | Count |")
        lines.append("|-------|-------|")
        for ev, c in sorted(counts.items(), key=lambda x: -x[1]):
            lines.append(f"| {ev} | {c} |")

    # Disconnection analysis
    disconnects = [e for e in events if e.get("event") in ("disconnected", "connection_failed")]
    if disconnects:
        lines.append("")
        lines.append(f"### Disconnections: {len(disconnects)}")

        # Time between disconnections
        if len(disconnects) >= 2:
            gaps = []
            for i in range(1, len(disconnects)):
                try:
                    t1 = datetime.fromisoformat(disconnects[i - 1]["ts"]).timestamp()
                    t2 = datetime.fromisoformat(disconnects[i]["ts"]).timestamp()
                    gaps.append(t2 - t1)
                except (ValueError, KeyError):
                    continue

            if gaps:
                avg_gap = sum(gaps) / len(gaps)
                min_gap = min(gaps)
                max_gap = max(gaps)
                lines.append(f"- Average interval: **{_format_duration(avg_gap)}**")
                lines.append(f"- Shortest: {_format_duration(min_gap)} / Longest: {_format_duration(max_gap)}")

                # Detect if there's a regular pattern
                if max_gap < avg_gap * 2 and min_gap > avg_gap * 0.5 and len(gaps) >= 3:
                    lines.append(f"- Pattern: disconnections occur roughly every **{_format_duration(avg_gap)}**")

        # Common error reasons
        reasons: dict[str, int] = {}
        errors: dict[str, int] = {}
        for d in disconnects:
            r = d.get("reason")
            if r:
                reasons[r] = reasons.get(r, 0) + 1
            err = d.get("error")
            if err:
                # Truncate long errors
                short_err = err[:80] + ("..." if len(err) > 80 else "")
                errors[short_err] = errors.get(short_err, 0) + 1

        if reasons:
            lines.append("")
            lines.append("**Disconnect reasons:**")
            for r, c in sorted(reasons.items(), key=lambda x: -x[1]):
                lines.append(f"- `{r}` ({c}x)")

        if errors:
            lines.append("")
            lines.append("**Errors seen:**")
            for err, c in sorted(errors.items(), key=lambda x: -x[1]):
                lines.append(f"- `{err}` ({c}x)")

        # Show last 5 disconnections
        lines.append("")
        lines.append("**Recent disconnections:**")
        for d in disconnects[-5:]:
            ts = _short_ts(d.get("ts"))
            reason = d.get("reason", "")
            error = d.get("error", "")
            detail = d.get("detail", "")
            info = reason or error or detail or "unknown"
            lines.append(f"- {ts} — `{info}`")

    # Reconnection performance
    reconnects = [e for e in events if e.get("event") == "reconnecting"]
    if reconnects:
        attempts = [e.get("attempt", 0) for e in reconnects if e.get("attempt") is not None]
        delays = [e.get("delay", 0) for e in reconnects if e.get("delay") is not None]
        lines.append("")
        lines.append(f"### Reconnection Attempts: {len(reconnects)}")
        if attempts:
            lines.append(f"- Max consecutive attempts: {max(attempts)}")
        if delays:
            lines.append(f"- Average backoff delay: {sum(delays)/len(delays):.1f}s")

    # Heartbeat analysis
    hb_fail = [e for e in events if e.get("event") == "heartbeat_failed"]
    hb_ok = counts.get("heartbeat_ok", 0)
    if hb_fail:
        hb_total = hb_ok + len(hb_fail)
        fail_rate = len(hb_fail) / hb_total * 100 if hb_total > 0 else 0
        lines.append("")
        lines.append(f"### Heartbeat Failures: {len(hb_fail)}/{hb_total} ({fail_rate:.1f}% failure rate)")
        if fail_rate > 20:
            lines.append("- High failure rate suggests network instability or server-side issues")

    # Stability score
    lines.append("")
    lines.append(_compute_stability(counts, events, hours))

    return "\n".join(lines)


def _compute_stability(counts: dict, events: list, hours: float) -> str:
    """Compute a simple stability score with recommendations."""
    disconnects = counts.get("disconnected", 0) + counts.get("connection_failed", 0)
    reconnects = counts.get("reconnected", 0)
    hb_ok = counts.get("heartbeat_ok", 0)
    hb_fail = counts.get("heartbeat_failed", 0)

    # Score out of 100
    score = 100

    # Penalize disconnections (heavy penalty)
    if disconnects > 0:
        score -= min(50, disconnects * 8)

    # Penalize heartbeat failures
    hb_total = hb_ok + hb_fail
    if hb_total > 0:
        hb_fail_rate = hb_fail / hb_total
        score -= int(hb_fail_rate * 30)

    # Bonus: successful reconnections show resilience
    if disconnects > 0 and reconnects > 0:
        recovery_rate = min(1.0, reconnects / disconnects)
        score += int(recovery_rate * 10)

    score = max(0, min(100, score))

    if score >= 90:
        label = "Excellent"
    elif score >= 70:
        label = "Good"
    elif score >= 50:
        label = "Unstable"
    elif score >= 30:
        label = "Poor"
    else:
        label = "Critical"

    result = f"### Stability: {score}/100 ({label})"

    # Actionable recommendations
    tips = []
    if disconnects > 5:
        tips.append("Frequent disconnections — check network stability or firewall rules")
    if hb_total > 0 and hb_fail / hb_total > 0.1:
        tips.append("Heartbeat failures — local server may be overloaded or unresponsive")
    if counts.get("auth_error", 0) > 0:
        tips.append("Auth errors seen — try `monkey_login` to re-authenticate")
    if counts.get("channel_error", 0) > 0:
        tips.append("Channel errors — Supabase Realtime may be having issues")
    if disconnects > 0 and reconnects == 0:
        tips.append("No successful reconnections — relay may not be running or auto-reconnect is broken")

    if tips:
        result += "\n\n**Recommendations:**"
        for tip in tips:
            result += f"\n- {tip}"

    return result


def _format_duration(seconds: float | int | None) -> str:
    """Format seconds into a human-readable duration."""
    if seconds is None:
        return "N/A"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m}m"


def _short_ts(ts_str: str | None) -> str:
    """Convert an ISO timestamp to a short readable format."""
    if not ts_str:
        return "N/A"
    try:
        dt = datetime.fromisoformat(ts_str)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts_str
