"""
Global state management for the MCP server.

This module holds all mutable global state used across the server.
"""

import os
import uuid
import subprocess
from typing import Optional


def get_git_user_email() -> Optional[str]:
    """Get the git user email from the current repository or global config."""
    try:
        result = subprocess.run(
            ['git', 'config', 'user.email'],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


# Git user email (read once at startup)
GIT_USER_EMAIL = get_git_user_email()

# Current task being worked on
CURRENT_TASK_ID: Optional[int] = None
CURRENT_TASK_TITLE: Optional[str] = None

# Current project focus - all operations will be scoped to this project
CURRENT_PROJECT_ID: Optional[str] = None
CURRENT_PROJECT_NAME: Optional[str] = None

# Session identifier for activity logging
CURRENT_SESSION_ID = str(uuid.uuid4())[:8]

# Authentication state
API_KEY: Optional[str] = None
ORG_ID: Optional[str] = None

# Fallback URL if config fetch fails
FALLBACK_API_URL = "https://p-63-branch-monkey.pages.dev"


def _fetch_api_url() -> str:
    """Fetch API URL from /api/config endpoint."""
    try:
        import httpx
        response = httpx.get(f"{FALLBACK_API_URL}/api/config", timeout=5.0)
        if response.status_code == 200:
            config = response.json()
            app_domain = config.get("appDomain")
            if app_domain:
                return f"https://{app_domain}"
    except Exception:
        pass
    return FALLBACK_API_URL


# API configuration
API_URL = os.environ.get("BRANCH_MONKEY_API_URL") or _fetch_api_url()
REQUEST_TIMEOUT = 30
