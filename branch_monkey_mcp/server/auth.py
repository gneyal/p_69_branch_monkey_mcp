"""
Authentication for Branch Monkey MCP Server.

Handles token storage and device code flow authentication.
"""

import os
import sys
import json
import time
import webbrowser
from pathlib import Path
from typing import Optional

import requests


def get_token_path() -> Path:
    """Get the path to the stored token file."""
    config_dir = Path.home() / ".branch-monkey"
    config_dir.mkdir(exist_ok=True)
    return config_dir / "token.json"


def load_stored_token(api_url: str) -> Optional[dict]:
    """Load stored token from disk."""
    token_path = get_token_path()
    if token_path.exists():
        try:
            with open(token_path) as f:
                data = json.load(f)
                if data.get("api_url") == api_url:
                    return data
        except Exception:
            pass
    return None


def save_token(token: str, api_url: str, org_id: str = None):
    """Save token to disk."""
    token_path = get_token_path()
    data = {
        "access_token": token,
        "api_url": api_url,
        "saved_at": time.time()
    }
    if org_id:
        data["org_id"] = org_id
    with open(token_path, "w") as f:
        json.dump(data, f)
    os.chmod(token_path, 0o600)


def clear_token():
    """Remove stored token."""
    token_path = get_token_path()
    if token_path.exists():
        token_path.unlink()


def get_machine_name() -> str:
    """Get a name for this machine."""
    try:
        cwd = os.getcwd()
        project_name = os.path.basename(cwd)
        import socket
        hostname = socket.gethostname()
        return f"{project_name} on {hostname}"
    except Exception:
        return "Claude Code MCP"


def device_code_flow(api_url: str) -> Optional[dict]:
    """Run the device code flow to authenticate.

    Returns dict with 'access_token' and 'org_id' on success, None on failure.
    """
    print("\n" + "=" * 60, file=sys.stderr)
    print("  Branch Monkey - Authentication Required", file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    machine_name = get_machine_name()

    try:
        response = requests.post(
            f"{api_url}/api/auth/device",
            json={"machine_name": machine_name},
            timeout=30
        )

        if not response.ok:
            print(f"Error: Failed to start authentication: {response.text}", file=sys.stderr)
            return None

        data = response.json()
        device_code = data["device_code"]
        user_code = data["user_code"]
        verification_uri = data["verification_uri"]
        expires_in = data.get("expires_in", 900)
        interval = data.get("interval", 5)

        print(f"\n  To authorize this device, visit:\n", file=sys.stderr)
        print(f"    {verification_uri}", file=sys.stderr)
        print(f"\n  Or enter this code at {api_url}/approve:\n", file=sys.stderr)
        print(f"    {user_code}", file=sys.stderr)
        print(f"\n  Waiting for approval...", file=sys.stderr)
        print("=" * 60 + "\n", file=sys.stderr)

        try:
            webbrowser.open(verification_uri)
        except Exception:
            pass

        start_time = time.time()
        while time.time() - start_time < expires_in:
            time.sleep(interval)

            poll_response = requests.get(
                f"{api_url}/api/auth/device",
                params={"device_code": device_code},
                timeout=30
            )

            if not poll_response.ok:
                error = poll_response.json().get("error", "unknown")
                if error == "expired_token":
                    print("  Code expired. Please try again.", file=sys.stderr)
                    return None
                elif error == "access_denied":
                    print("  Access denied.", file=sys.stderr)
                    return None
                continue

            poll_data = poll_response.json()

            if poll_data.get("status") == "approved":
                print("  Approved! You can now use Branch Monkey.", file=sys.stderr)
                return {
                    "access_token": poll_data.get("access_token"),
                    "org_id": poll_data.get("org_id")
                }

        print("  Timeout waiting for approval.", file=sys.stderr)
        return None

    except Exception as e:
        print(f"Error during authentication: {e}", file=sys.stderr)
        return None
