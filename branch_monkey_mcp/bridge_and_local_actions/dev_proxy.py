"""
Development proxy server for the local server.

This module provides a reverse proxy that can route requests to
different development servers based on the current task/run context.
"""

import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional

import httpx


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


def set_proxy_port(port: int) -> bool:
    """Set the proxy port (restarts proxy if running)."""
    global _proxy_state
    was_running = _proxy_state["running"]
    old_target = _proxy_state["target_port"]
    old_run_id = _proxy_state["target_run_id"]

    if was_running:
        stop_dev_proxy()

    _proxy_state["proxy_port"] = port

    if was_running:
        if start_dev_proxy(port):
            if old_target:
                set_proxy_target(old_target, old_run_id)
            return True
        return False
    return True
