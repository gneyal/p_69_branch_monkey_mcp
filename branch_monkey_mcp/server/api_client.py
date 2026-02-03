"""
HTTP client for Branch Monkey API.

Provides authenticated requests with automatic token refresh.
"""

import sys
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import state
from .auth import clear_token, device_code_flow, save_token


_session = None


def create_session():
    """Create a requests session with retry strategy."""
    session = requests.Session()
    retry_strategy = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=[500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def get_session():
    """Get or create the HTTP session."""
    global _session
    if _session is None:
        _session = create_session()
    return _session


def reset_session():
    """Reset the HTTP session (used after re-authentication)."""
    global _session
    _session = None


def api_request(method: str, endpoint: str, **kwargs) -> dict:
    """Make an authenticated API request."""
    global _session
    url = f"{state.API_URL.rstrip('/')}/{endpoint.lstrip('/')}"

    headers = kwargs.pop("headers", {})
    if state.API_KEY:
        headers["Authorization"] = f"Bearer {state.API_KEY}"
    if state.ORG_ID:
        headers["X-Org-Id"] = state.ORG_ID
    if state.CURRENT_PROJECT_ID:
        headers["X-Project-Id"] = state.CURRENT_PROJECT_ID
    headers["Content-Type"] = "application/json"

    kwargs["headers"] = headers
    kwargs.setdefault("timeout", state.REQUEST_TIMEOUT)

    session = get_session()
    response = session.request(method, url, **kwargs)

    # Auto re-authenticate on 401
    if response.status_code == 401:
        print("\n[Branch Monkey] Token expired, re-authenticating...", file=sys.stderr)
        clear_token()
        reset_session()

        auth_result = device_code_flow(state.API_URL)
        if auth_result:
            state.API_KEY = auth_result.get("access_token")
            state.ORG_ID = auth_result.get("org_id")
            save_token(state.API_KEY, state.API_URL, state.ORG_ID)

            # Retry the request with new token
            headers["Authorization"] = f"Bearer {state.API_KEY}"
            if state.ORG_ID:
                headers["X-Org-Id"] = state.ORG_ID
            kwargs["headers"] = headers
            session = get_session()
            response = session.request(method, url, **kwargs)
        else:
            response.raise_for_status()

    response.raise_for_status()

    return response.json() if response.content else {}


def api_get(endpoint: str, **kwargs) -> dict:
    """Make a GET request."""
    return api_request("GET", endpoint, **kwargs)


def api_post(endpoint: str, data: dict = None, **kwargs) -> dict:
    """Make a POST request."""
    return api_request("POST", endpoint, json=data, **kwargs)


def api_put(endpoint: str, data: dict = None, **kwargs) -> dict:
    """Make a PUT request."""
    return api_request("PUT", endpoint, json=data, **kwargs)


def api_delete(endpoint: str, **kwargs) -> dict:
    """Make a DELETE request."""
    return api_request("DELETE", endpoint, **kwargs)
