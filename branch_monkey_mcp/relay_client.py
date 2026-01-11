"""
WebSocket Relay Client for Local Server

This module allows a local Branch Monkey server to connect to the cloud
and receive relayed requests from the web UI.

Usage:
    branch-monkey-relay

The client:
1. Authenticates using device auth flow (if no cached token)
2. Establishes WebSocket connection to cloud relay hub
3. Receives requests and executes them locally
4. Streams responses back through the WebSocket
"""

import asyncio
import json
import os
import socket
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any

import httpx

# Try to import websockets
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False


# Config file location
CONFIG_DIR = Path.home() / ".branch-monkey"
TOKEN_FILE = CONFIG_DIR / "cloud_token.json"

# Cloud API URL
DEFAULT_CLOUD_URL = "https://newfactory.co"


class RelayClient:
    """
    WebSocket client that connects local server to cloud relay hub.

    Handles:
    - Device authentication flow
    - WebSocket connection management
    - Request/response relay
    - Auto-reconnection
    """

    def __init__(
        self,
        cloud_url: str = DEFAULT_CLOUD_URL,
        local_port: int = 8081,
        machine_name: Optional[str] = None
    ):
        self.cloud_url = cloud_url.rstrip("/")
        self.local_port = local_port
        self.machine_name = machine_name or self._get_machine_name()
        self.access_token: Optional[str] = None
        self.websocket = None
        self._running = False
        self._reconnect_delay = 1  # Start with 1 second
        self._max_reconnect_delay = 60  # Max 60 seconds

    def _get_machine_name(self) -> str:
        """Generate a human-readable machine name."""
        hostname = socket.gethostname()
        # Clean up common suffixes
        if hostname.endswith(".local"):
            hostname = hostname[:-6]
        return hostname

    def _load_token(self) -> Optional[str]:
        """Load cached access token from config file."""
        if TOKEN_FILE.exists():
            try:
                with open(TOKEN_FILE) as f:
                    data = json.load(f)
                    token = data.get("access_token")
                    if token:
                        print(f"[Relay] Loaded cached token")
                        return token
            except Exception as e:
                print(f"[Relay] Error loading token: {e}")
        return None

    def _save_token(self, token: str, user_id: str = None):
        """Save access token to config file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            json.dump({
                "access_token": token,
                "user_id": user_id,
                "created_at": datetime.utcnow().isoformat(),
                "cloud_url": self.cloud_url
            }, f, indent=2)
        print(f"[Relay] Saved token to {TOKEN_FILE}")

    def _clear_token(self):
        """Clear cached token."""
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
            print(f"[Relay] Cleared cached token")

    async def authenticate(self) -> bool:
        """
        Authenticate with the cloud using device auth flow.

        Returns True if authentication succeeds.
        """
        # Try cached token first
        self.access_token = self._load_token()
        if self.access_token:
            # Verify token is still valid
            if await self._verify_token():
                return True
            else:
                print("[Relay] Cached token is invalid, re-authenticating...")
                self._clear_token()

        # Start device auth flow
        print("\n[Relay] Starting device authentication...")
        print(f"[Relay] Connecting to {self.cloud_url}")

        async with httpx.AsyncClient() as client:
            # Request device code
            try:
                response = await client.post(
                    f"{self.cloud_url}/api/auth/device",
                    json={"machine_name": self.machine_name},
                    timeout=30
                )
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                print(f"[Relay] Failed to start device auth: {e}")
                return False

            device_code = data["device_code"]
            user_code = data["user_code"]
            verification_uri = data["verification_uri"]
            expires_in = data.get("expires_in", 900)
            interval = data.get("interval", 5)

            print(f"\n{'='*50}")
            print(f"Please visit: {verification_uri}")
            print(f"Your code: {user_code}")
            print(f"{'='*50}\n")
            print("Waiting for approval...")

            # Poll for approval
            start_time = time.time()
            while time.time() - start_time < expires_in:
                await asyncio.sleep(interval)

                try:
                    response = await client.get(
                        f"{self.cloud_url}/api/auth/device",
                        params={"device_code": device_code},
                        timeout=30
                    )
                    data = response.json()

                    if data.get("status") == "approved":
                        self.access_token = data["access_token"]
                        user_id = data.get("user_id")
                        self._save_token(self.access_token, user_id)
                        print("[Relay] Authentication successful!")
                        return True
                    elif data.get("error") == "access_denied":
                        print("[Relay] Authentication denied")
                        return False
                    elif data.get("error") == "expired_token":
                        print("[Relay] Device code expired")
                        return False
                    # else: still pending, continue polling

                except Exception as e:
                    print(f"[Relay] Polling error: {e}")

            print("[Relay] Authentication timed out")
            return False

    async def _verify_token(self) -> bool:
        """Verify that the cached token is still valid."""
        if not self.access_token:
            return False

        # Try a simple API call to verify
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(
                    f"{self.cloud_url}/api/relay/connections",
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    timeout=10
                )
                return response.status_code == 200
            except Exception:
                return False

    async def connect(self):
        """
        Connect to cloud relay WebSocket.

        This method runs indefinitely, handling reconnection.
        """
        if not self.access_token:
            if not await self.authenticate():
                print("[Relay] Authentication failed, cannot connect")
                return

        self._running = True
        self._reconnect_delay = 1

        while self._running:
            try:
                await self._connect_websocket()
            except Exception as e:
                if self._running:
                    print(f"[Relay] Connection error: {e}")
                    print(f"[Relay] Reconnecting in {self._reconnect_delay}s...")
                    await asyncio.sleep(self._reconnect_delay)
                    # Exponential backoff
                    self._reconnect_delay = min(
                        self._reconnect_delay * 2,
                        self._max_reconnect_delay
                    )

    async def _connect_websocket(self):
        """Establish and maintain WebSocket connection."""
        if not WEBSOCKETS_AVAILABLE:
            print("[Relay] websockets library not available")
            print("[Relay] Install with: pip install websockets")
            return

        # Convert http(s) to ws(s)
        ws_url = self.cloud_url.replace("https://", "wss://").replace("http://", "ws://")
        ws_url = f"{ws_url}/api/relay/ws?token={self.access_token}&machine_name={self.machine_name}"

        print(f"[Relay] Connecting to {ws_url[:50]}...")

        async with websockets.connect(ws_url, ping_interval=25, ping_timeout=10) as ws:
            self.websocket = ws
            self._reconnect_delay = 1  # Reset on successful connection

            # Wait for connection confirmation
            msg = await ws.recv()
            data = json.loads(msg)

            if data.get("type") == "connected":
                machine_id = data.get("machine_id")
                print(f"[Relay] Connected as '{self.machine_name}' (ID: {machine_id})")
                print(f"[Relay] Ready to receive requests from cloud")
            elif data.get("type") == "error":
                print(f"[Relay] Connection rejected: {data.get('message')}")
                if "token" in data.get("message", "").lower():
                    self._clear_token()
                return

            # Message loop
            async for message in ws:
                try:
                    data = json.loads(message)
                    await self._handle_message(ws, data)
                except json.JSONDecodeError:
                    print(f"[Relay] Invalid JSON: {message[:100]}")
                except Exception as e:
                    print(f"[Relay] Message handling error: {e}")

    async def _handle_message(self, ws, data: Dict[str, Any]):
        """Handle an incoming message from the cloud."""
        msg_type = data.get("type")

        if msg_type == "ping":
            await ws.send(json.dumps({"type": "pong"}))

        elif msg_type == "request":
            # Execute request locally and send response
            request_id = data.get("id")
            method = data.get("method", "GET")
            path = data.get("path", "/")
            body = data.get("body", {})

            print(f"[Relay] Received request: {method} {path}")

            try:
                response = await self._execute_local_request(method, path, body)
                await ws.send(json.dumps({
                    "type": "response",
                    "id": request_id,
                    "status": response.get("status", 200),
                    "body": response.get("body", response)
                }))
            except Exception as e:
                await ws.send(json.dumps({
                    "type": "response",
                    "id": request_id,
                    "status": 500,
                    "body": {"error": str(e)}
                }))

        elif msg_type == "stream_request":
            # Start streaming response
            request_id = data.get("id")
            path = data.get("path", "/")

            print(f"[Relay] Starting stream: {path}")

            # Run stream in background task
            asyncio.create_task(
                self._stream_local_request(ws, request_id, path)
            )

        elif msg_type == "disconnect":
            reason = data.get("reason", "unknown")
            print(f"[Relay] Server requested disconnect: {reason}")
            await ws.close()

    async def _execute_local_request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Execute a request against the local server."""
        url = f"http://localhost:{self.local_port}{path}"

        async with httpx.AsyncClient() as client:
            if method == "GET":
                response = await client.get(url, timeout=30)
            elif method == "POST":
                response = await client.post(url, json=body, timeout=30)
            elif method == "PUT":
                response = await client.put(url, json=body, timeout=30)
            elif method == "DELETE":
                response = await client.delete(url, timeout=30)
            elif method == "PATCH":
                response = await client.patch(url, json=body, timeout=30)
            else:
                return {"status": 405, "body": {"error": f"Method {method} not supported"}}

            try:
                return {"status": response.status_code, "body": response.json()}
            except Exception:
                return {"status": response.status_code, "body": {"text": response.text}}

    async def _stream_local_request(
        self,
        ws,
        request_id: str,
        path: str
    ):
        """Stream SSE events from local server to cloud."""
        url = f"http://localhost:{self.local_port}{path}"

        try:
            async with httpx.AsyncClient() as client:
                async with client.stream("GET", url, timeout=None) as response:
                    async for line in response.aiter_lines():
                        if not line:
                            continue

                        # Parse SSE format
                        if line.startswith("data: "):
                            data = line[6:]
                            try:
                                parsed = json.loads(data)
                                await ws.send(json.dumps({
                                    "type": "stream",
                                    "id": request_id,
                                    "data": parsed
                                }))

                                # Check for exit event
                                if parsed.get("type") == "exit":
                                    break
                            except json.JSONDecodeError:
                                # Send as raw text
                                await ws.send(json.dumps({
                                    "type": "stream",
                                    "id": request_id,
                                    "data": {"raw": data}
                                }))

        except Exception as e:
            print(f"[Relay] Stream error: {e}")
        finally:
            await ws.send(json.dumps({
                "type": "stream_end",
                "id": request_id
            }))

    def stop(self):
        """Stop the relay client."""
        self._running = False
        if self.websocket:
            asyncio.create_task(self.websocket.close())


def run_relay_client(
    cloud_url: str = DEFAULT_CLOUD_URL,
    local_port: int = 8081,
    machine_name: Optional[str] = None
):
    """
    Run the relay client.

    This is a blocking call that runs until interrupted.
    """
    client = RelayClient(
        cloud_url=cloud_url,
        local_port=local_port,
        machine_name=machine_name
    )

    async def main():
        await client.connect()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Relay] Shutting down...")
        client.stop()


async def start_relay_client_async(
    cloud_url: str = DEFAULT_CLOUD_URL,
    local_port: int = 8081,
    machine_name: Optional[str] = None
) -> RelayClient:
    """
    Start the relay client as an async task.

    Returns the client instance for control.
    """
    client = RelayClient(
        cloud_url=cloud_url,
        local_port=local_port,
        machine_name=machine_name
    )

    # Start in background task
    asyncio.create_task(client.connect())

    return client


def main():
    """CLI entry point for branch-monkey-relay."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Connect local Branch Monkey server to cloud relay"
    )
    parser.add_argument(
        "--cloud-url",
        default=os.environ.get("BRANCH_MONKEY_CLOUD_URL", DEFAULT_CLOUD_URL),
        help="Cloud URL (default: https://newfactory.co)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BRANCH_MONKEY_LOCAL_PORT", "8081")),
        help="Local server port (default: 8081)"
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Machine name (default: hostname)"
    )

    args = parser.parse_args()

    print(f"[Relay] Starting relay client...")
    print(f"[Relay] Cloud URL: {args.cloud_url}")
    print(f"[Relay] Local port: {args.port}")

    run_relay_client(
        cloud_url=args.cloud_url,
        local_port=args.port,
        machine_name=args.name
    )


if __name__ == "__main__":
    main()
