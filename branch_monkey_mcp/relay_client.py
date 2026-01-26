"""
Relay Client for Kompany Cloud

This module allows a local machine to connect to Kompany Cloud
and receive relayed requests from the web UI.

VERSION: 0.2.0

The client:
1. Authenticates using device auth flow (if no cached token)
2. Gets connection config from cloud (Supabase URL, key, etc.)
3. Connects to Supabase Realtime channel
4. Registers as a compute node
5. Receives requests and executes them locally
6. Streams responses back through the channel

Features:
- Auto-reconnect with exponential backoff on connection loss
- Health monitoring to detect silent disconnections
- Graceful shutdown handling

Usage:
    branch-monkey-relay
"""

import asyncio
import json
import os
import random
import socket
import sys
import time
import webbrowser
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

import httpx


# Reconnection settings
INITIAL_RECONNECT_DELAY = 1  # seconds
MAX_RECONNECT_DELAY = 60  # seconds
RECONNECT_BACKOFF_MULTIPLIER = 2
MAX_RECONNECT_ATTEMPTS = None  # None = unlimited
CONNECTION_HEALTH_CHECK_INTERVAL = 30  # seconds
HEARTBEAT_TIMEOUT = 60  # seconds - consider connection dead if no heartbeat succeeds


class ConnectionState(Enum):
    """Connection state for the relay client."""
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"

# Version
VERSION = "0.2.0"

# Config file location
CONFIG_DIR = Path.home() / ".kompany"
TOKEN_FILE = CONFIG_DIR / "relay_token.json"
MACHINE_ID_FILE = CONFIG_DIR / "machine_id"

# Cloud API URL - fallback if /api/config fetch fails
FALLBACK_CLOUD_URL = "https://p-63-branch-monkey.pages.dev"


def fetch_cloud_url_from_config(fallback_url: str = FALLBACK_CLOUD_URL) -> str:
    """
    Fetch the cloud URL from the /api/config endpoint.
    This makes the relay domain-agnostic by reading the configured appDomain.
    """
    try:
        import httpx
        response = httpx.get(f"{fallback_url}/api/config", timeout=5.0)
        if response.status_code == 200:
            config = response.json()
            app_domain = config.get("appDomain")
            if app_domain:
                cloud_url = f"https://{app_domain}"
                print(f"[Relay] Using domain from config: {app_domain}")
                return cloud_url
    except Exception as e:
        print(f"[Relay] Could not fetch config: {e}")
    return fallback_url


# Will be resolved at runtime
DEFAULT_CLOUD_URL = FALLBACK_CLOUD_URL


class RelayClient:
    """
    Relay client that connects local machine to Kompany Cloud
    using Supabase Realtime.

    Handles:
    - Device authentication flow
    - Supabase Realtime connection
    - Request/response relay
    - Auto-reconnection with exponential backoff
    - Health monitoring
    - Compute node registration
    """

    def __init__(
        self,
        cloud_url: str = DEFAULT_CLOUD_URL,
        local_port: int = 18081,
        machine_name: Optional[str] = None
    ):
        self.cloud_url = cloud_url.rstrip("/")
        self.local_port = local_port
        self.machine_name = machine_name or self._get_machine_name()
        self.machine_id = self._get_stable_machine_id()

        # Auth data
        self.access_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.org_id: Optional[str] = None

        # Relay config (from cloud)
        self.relay_config: Optional[Dict[str, Any]] = None

        # Supabase client
        self.supabase = None
        self.channel = None

        self._running = False

        # Connection state tracking for auto-reconnect
        self.connection_state = ConnectionState.DISCONNECTED
        self.reconnect_attempts = 0
        self.last_successful_heartbeat: Optional[datetime] = None
        self.should_reconnect = True  # False when explicitly disconnected
        self._reconnect_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

    def _get_reconnect_delay(self) -> float:
        """Calculate reconnect delay with exponential backoff and jitter."""
        delay = min(
            INITIAL_RECONNECT_DELAY * (RECONNECT_BACKOFF_MULTIPLIER ** self.reconnect_attempts),
            MAX_RECONNECT_DELAY
        )
        # Add jitter (±20%) to prevent thundering herd
        jitter = delay * 0.2 * (random.random() * 2 - 1)
        return delay + jitter

    def _get_machine_name(self) -> str:
        """Generate a human-readable machine name."""
        return socket.gethostname()

    def _get_stable_machine_id(self) -> str:
        """Get or create a stable machine ID that persists across restarts."""
        if MACHINE_ID_FILE.exists():
            return MACHINE_ID_FILE.read_text().strip()

        # Generate once, reuse forever
        machine_id = f"{self.machine_name}-{uuid.uuid4().hex[:8]}"
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        MACHINE_ID_FILE.write_text(machine_id)
        return machine_id

    def _load_token(self) -> Optional[Dict[str, Any]]:
        """Load cached token and config from file."""
        if TOKEN_FILE.exists():
            try:
                with open(TOKEN_FILE) as f:
                    data = json.load(f)
                    if data.get("access_token") and data.get("cloud_url") == self.cloud_url:
                        print(f"[Relay] Using saved token for {data.get('machine_name', 'unknown')}")
                        return data
            except Exception as e:
                print(f"[Relay] Error loading token: {e}")
        return None

    def _save_token(self, data: Dict[str, Any]):
        """Save token and config to file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data["cloud_url"] = self.cloud_url
        data["machine_name"] = self.machine_name
        data["machine_id"] = self.machine_id
        data["saved_at"] = datetime.utcnow().isoformat()

        with open(TOKEN_FILE, "w") as f:
            json.dump(data, f, indent=2)
        print(f"[Relay] Token saved to {TOKEN_FILE}")

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
        cached = self._load_token()
        if cached:
            self.access_token = cached.get("access_token")
            self.user_id = cached.get("user_id")
            self.org_id = cached.get("org_id")
            self.relay_config = cached.get("relay_config")
            self.machine_id = cached.get("machine_id", self.machine_id)

            if self.relay_config:
                return True
            else:
                print("[Relay] Cached token missing relay config, re-authenticating...")
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
            print(f"  To authorize this device, visit:")
            print(f"  {verification_uri}")
            print(f"\n  Or go to {self.cloud_url}/approve")
            print(f"  and enter code: {user_code}")
            print(f"{'='*50}\n")

            # Auto-open browser for authentication
            try:
                webbrowser.open(verification_uri)
                print(f"[Relay] Opening browser for authentication...")
            except Exception:
                pass  # Browser open failed, user can manually visit URL

            print(f"[Relay] Waiting for approval (expires in {expires_in//60} minutes)...")

            # Poll for approval
            start_time = time.time()
            poll_count = 0
            while time.time() - start_time < expires_in:
                await asyncio.sleep(interval)
                poll_count += 1

                try:
                    response = await client.get(
                        f"{self.cloud_url}/api/auth/device",
                        params={"device_code": device_code},
                        timeout=30
                    )
                    data = response.json()

                    if data.get("status") == "approved":
                        self.access_token = data["access_token"]
                        self.user_id = data.get("user_id")
                        self.org_id = data.get("org_id")
                        self.relay_config = data.get("relay_config")

                        if not self.relay_config:
                            print("[Relay] Error: No relay config in response")
                            return False

                        # Save everything
                        self._save_token({
                            "access_token": self.access_token,
                            "user_id": self.user_id,
                            "org_id": self.org_id,
                            "relay_config": self.relay_config
                        })

                        print("\n[Relay] Authentication successful!")
                        return True

                    elif data.get("error") == "access_denied":
                        print("[Relay] Authentication denied")
                        return False
                    elif data.get("error") == "expired_token":
                        print("[Relay] Device code expired")
                        return False

                    # Still pending
                    if poll_count % 6 == 0:
                        print("[Relay] Still waiting for approval...")

                except Exception as e:
                    print(f"[Relay] Polling error: {e}")

            print("[Relay] Authentication timed out")
            return False

    async def _connect_channel(self) -> bool:
        """
        Internal method to establish Supabase Realtime connection.
        Returns True if successful, False otherwise.
        """
        # Import supabase here to avoid import errors if not installed
        try:
            from supabase import acreate_client, AsyncClient
        except ImportError:
            print("[Relay] Error: supabase library not installed")
            print("[Relay] Install with: pip install supabase")
            return False

        supabase_url = self.relay_config.get("supabase_url")
        supabase_key = self.relay_config.get("supabase_key")
        channel_prefix = self.relay_config.get("channel_prefix", "relay")

        if not supabase_url or not supabase_key:
            print("[Relay] Error: Missing Supabase config")
            return False

        self.connection_state = ConnectionState.CONNECTING

        print(f"\n[Relay] Connecting to Supabase Realtime...")
        print(f"[Relay] User ID: {self.user_id}")
        print(f"[Relay] Machine ID: {self.machine_id}")
        print(f"[Relay] Machine name: {self.machine_name}")
        print(f"[Relay] Local port: {self.local_port}")

        try:
            # Create Supabase client
            self.supabase = await acreate_client(supabase_url, supabase_key)

            # Channel name for this machine
            channel_name = f"{channel_prefix}:{self.user_id}:{self.machine_id}"

            # Subscribe to channel
            self.channel = self.supabase.channel(channel_name)

            # Handle incoming messages
            def on_request(payload):
                asyncio.create_task(self._handle_message(payload))

            def on_disconnect(payload):
                print(f"\n[Relay] Received disconnect command from cloud")
                asyncio.create_task(self._shutdown())

            self.channel.on_broadcast("request", on_request)
            self.channel.on_broadcast("stream_request", on_request)
            self.channel.on_broadcast("stream_start", on_request)
            self.channel.on_broadcast("ping", lambda _: self._send_pong())
            self.channel.on_broadcast("disconnect", on_disconnect)

            # Subscribe (returns channel object, throws on error)
            await self.channel.subscribe()

            print(f"\n[Relay] Connected to Supabase Realtime!")
            print(f"[Relay] Channel: {channel_name}")
            print(f"[Relay] Ready to receive requests from cloud\n")

            # Register this machine
            await self._register_machine()

            # Send initial heartbeat to local server
            await self._send_local_heartbeat()

            # Update state
            self.connection_state = ConnectionState.CONNECTED
            self.reconnect_attempts = 0
            self.last_successful_heartbeat = datetime.utcnow()

            return True

        except Exception as e:
            print(f"[Relay] Connection failed: {e}")
            self.connection_state = ConnectionState.DISCONNECTED
            return False

    async def _disconnect_channel(self):
        """Disconnect from Supabase Realtime channel."""
        try:
            if self.channel and self.supabase:
                await self.supabase.remove_channel(self.channel)
                self.channel = None
            self.connection_state = ConnectionState.DISCONNECTED
            print("[Relay] Disconnected from channel")
        except Exception as e:
            print(f"[Relay] Error during disconnect: {e}")

    async def _reconnect(self):
        """Attempt to reconnect with exponential backoff."""
        if not self.should_reconnect:
            return

        self.connection_state = ConnectionState.RECONNECTING

        while self.should_reconnect and self._running:
            # Check max attempts
            if MAX_RECONNECT_ATTEMPTS is not None and self.reconnect_attempts >= MAX_RECONNECT_ATTEMPTS:
                print(f"[Relay] Max reconnect attempts ({MAX_RECONNECT_ATTEMPTS}) reached. Giving up.")
                self._running = False
                return

            delay = self._get_reconnect_delay()
            self.reconnect_attempts += 1

            print(f"[Relay] Reconnecting in {delay:.1f}s (attempt {self.reconnect_attempts})...")
            await asyncio.sleep(delay)

            if not self.should_reconnect or not self._running:
                return

            try:
                # Clean up old connection
                await self._disconnect_channel()

                # Attempt reconnection
                if await self._connect_channel():
                    print(f"[Relay] Reconnected successfully!")
                    return

            except Exception as e:
                print(f"[Relay] Reconnection attempt {self.reconnect_attempts} failed: {e}")
                continue

    async def _trigger_reconnect(self):
        """Trigger a reconnection attempt."""
        if self.connection_state == ConnectionState.RECONNECTING:
            return  # Already reconnecting

        print("[Relay] Triggering reconnection...")
        self.connection_state = ConnectionState.DISCONNECTED

        # Cancel existing reconnect task if any
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()

        # Start reconnection
        self._reconnect_task = asyncio.create_task(self._reconnect())

    async def _health_check_loop(self):
        """Monitor connection health and trigger reconnect if needed."""
        while self._running:
            try:
                await asyncio.sleep(CONNECTION_HEALTH_CHECK_INTERVAL)

                if self.connection_state != ConnectionState.CONNECTED:
                    continue

                # Check if heartbeat has succeeded recently
                if self.last_successful_heartbeat:
                    time_since_heartbeat = datetime.utcnow() - self.last_successful_heartbeat
                    if time_since_heartbeat.total_seconds() > HEARTBEAT_TIMEOUT:
                        print(f"[Relay] No successful heartbeat for {time_since_heartbeat.total_seconds():.0f}s - reconnecting")
                        await self._trigger_reconnect()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Relay] Health check error: {e}")

    async def connect(self):
        """
        Connect to Supabase Realtime and start receiving requests.
        Includes auto-reconnect on connection loss.
        """
        if not self.relay_config:
            if not await self.authenticate():
                print("[Relay] Authentication failed, cannot connect")
                return

        self._running = True

        # Initial connection
        if not await self._connect_channel():
            print("[Relay] Initial connection failed, starting reconnection loop...")
            await self._reconnect()
            if not self._running:
                return

        # Start background tasks
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._health_check_task = asyncio.create_task(self._health_check_loop())

        try:
            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)

                # If disconnected and should reconnect, trigger it
                if (self.connection_state == ConnectionState.DISCONNECTED
                    and self.should_reconnect
                    and (not self._reconnect_task or self._reconnect_task.done())):
                    self._reconnect_task = asyncio.create_task(self._reconnect())

        except KeyboardInterrupt:
            print("\n[Relay] Shutting down...")
        except asyncio.CancelledError:
            print("\n[Relay] Task cancelled, shutting down...")
        finally:
            self._running = False
            self.should_reconnect = False

            # Cancel background tasks
            if self._heartbeat_task:
                self._heartbeat_task.cancel()
            if self._health_check_task:
                self._health_check_task.cancel()
            if self._reconnect_task:
                self._reconnect_task.cancel()

            # Wait for tasks to finish
            tasks = [t for t in [self._heartbeat_task, self._health_check_task] if t]
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass

            await self._unregister_machine()

    async def _register_machine(self):
        """Register this machine as a compute node in the database."""
        try:
            await self.supabase.table("compute_nodes").upsert({
                "machine_id": self.machine_id,
                "user_id": self.user_id,
                "name": self.machine_name,
                "node_type": "local",
                "status": "online",
                "last_heartbeat": datetime.utcnow().isoformat(),
                "config": {"local_port": self.local_port},
                "capabilities": {"claude": True}
            }, on_conflict="machine_id").execute()
            print(f"[Relay] Registered compute node in database")
        except Exception as e:
            print(f"[Relay] Warning: Could not register compute node: {e}")

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to keep connection alive and detect disconnects."""
        consecutive_failures = 0

        while self._running:
            try:
                await asyncio.sleep(25)

                if self.connection_state != ConnectionState.CONNECTED:
                    continue

                # Heartbeat to Supabase
                await self.supabase.table("compute_nodes").update({
                    "last_heartbeat": datetime.utcnow().isoformat(),
                    "status": "online"
                }).eq("machine_id", self.machine_id).execute()

                # Heartbeat to local server (so dashboard knows relay is connected)
                await self._send_local_heartbeat()

                # Success - reset failure counter
                self.last_successful_heartbeat = datetime.utcnow()
                consecutive_failures = 0

            except asyncio.CancelledError:
                break
            except Exception as e:
                consecutive_failures += 1
                print(f"[Relay] Heartbeat error (attempt {consecutive_failures}): {e}")

                # If heartbeats keep failing, connection might be dead
                if consecutive_failures >= 3:
                    print(f"[Relay] Multiple heartbeat failures - connection may be dead")
                    await self._trigger_reconnect()
                    consecutive_failures = 0

    async def _send_local_heartbeat(self):
        """Send heartbeat to local server to indicate relay is connected."""
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"http://127.0.0.1:{self.local_port}/api/relay/heartbeat",
                    json={
                        "machine_id": self.machine_id,
                        "machine_name": self.machine_name,
                        "cloud_url": self.cloud_url
                    },
                    timeout=5
                )
        except Exception:
            pass  # Local server might not support this yet

    async def _unregister_machine(self):
        """Mark compute node as offline."""
        try:
            await self.supabase.table("compute_nodes").update({
                "status": "offline"
            }).eq("machine_id", self.machine_id).execute()
        except Exception:
            pass
        # Notify local server of disconnection
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    f"http://127.0.0.1:{self.local_port}/api/relay/disconnect",
                    timeout=5
                )
        except Exception:
            pass

    async def _shutdown(self):
        """Gracefully shutdown the relay client."""
        print("[Relay] Shutting down gracefully...")
        self._running = False
        self.should_reconnect = False
        await self._unregister_machine()
        print("[Relay] Disconnected. Goodbye!")
        sys.exit(0)

    def _send_pong(self):
        """Respond to ping with pong."""
        self.channel.send_broadcast("pong", {
            "machine_id": self.machine_id,
            "timestamp": datetime.utcnow().isoformat()
        })

    async def _handle_message(self, payload: Dict[str, Any]):
        """Handle incoming message from cloud."""
        try:
            # The broadcast payload may be nested: { event: "...", payload: { actual data } }
            # or flat: { actual data }
            actual_payload = payload.get("payload", payload) if isinstance(payload, dict) else payload

            msg_type = actual_payload.get("type", "request")
            print(f"[Relay] Received {msg_type}: {actual_payload.get('method', '')} {actual_payload.get('path', '')}")

            if msg_type == "stream_start":
                # Start SSE streaming for an agent
                asyncio.create_task(self._handle_stream_start(actual_payload))
            elif msg_type in ("request", "stream_request"):
                response = await self._execute_local_request(actual_payload)
                await self.channel.send_broadcast("response", response)
                print(f"[Relay] Sent response: status={response.get('status')}")

        except Exception as e:
            print(f"[Relay] Error handling message: {e}")

    async def _handle_stream_start(self, payload: Dict[str, Any]):
        """Handle SSE stream start request - connect to local SSE and forward events."""
        stream_id = payload.get("stream_id")
        agent_id = payload.get("agent_id")

        if not stream_id or not agent_id:
            print(f"[Relay] Stream start missing stream_id or agent_id")
            return

        url = f"http://127.0.0.1:{self.local_port}/api/local-claude/agents/{agent_id}/stream"
        print(f"[Relay] Starting SSE stream for agent {agent_id}, stream_id={stream_id}")

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        await self.channel.send_broadcast("stream_event", {
                            "stream_id": stream_id,
                            "type": "error",
                            "error": f"Failed to connect to local SSE: {response.status_code}"
                        })
                        return

                    print(f"[Relay] Connected to local SSE for agent {agent_id}")
                    event_count = 0

                    async for line in response.aiter_lines():
                        if not line or line.startswith(":"):
                            # Empty line or comment (heartbeat)
                            continue

                        if line.startswith("data: "):
                            data = line[6:]  # Remove "data: " prefix
                            try:
                                event = json.loads(data)
                                event_count += 1
                                event_type = event.get("type", "unknown")
                                if event_count <= 5 or event_count % 10 == 0:
                                    print(f"[Relay] Forwarding event #{event_count} type={event_type} for agent {agent_id}")
                                # Forward the event through Realtime
                                await self.channel.send_broadcast("stream_event", {
                                    "stream_id": stream_id,
                                    "event": event
                                })

                                # Check for exit event
                                if event.get("type") == "exit":
                                    print(f"[Relay] Stream ended for agent {agent_id}")
                                    break

                            except json.JSONDecodeError:
                                # Forward raw data if not JSON
                                await self.channel.send_broadcast("stream_event", {
                                    "stream_id": stream_id,
                                    "raw": data
                                })

        except Exception as e:
            print(f"[Relay] Stream error for agent {agent_id}: {e}")
            await self.channel.send_broadcast("stream_event", {
                "stream_id": stream_id,
                "type": "error",
                "error": str(e)
            })

    async def _execute_local_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Execute request on local server and return response."""
        request_id = request.get("id")
        method = request.get("method", "GET")
        path = request.get("path", "/")
        body = request.get("body", {})
        headers = request.get("headers", {})

        url = f"http://127.0.0.1:{self.local_port}{path}"

        try:
            async with httpx.AsyncClient() as client:
                if method == "GET":
                    response = await client.get(url, headers=headers, timeout=55)
                elif method == "POST":
                    response = await client.post(url, json=body, headers=headers, timeout=55)
                elif method == "PUT":
                    response = await client.put(url, json=body, headers=headers, timeout=55)
                elif method == "DELETE":
                    response = await client.delete(url, headers=headers, timeout=55)
                elif method == "PATCH":
                    response = await client.patch(url, json=body, headers=headers, timeout=55)
                else:
                    return {
                        "type": "response",
                        "id": request_id,
                        "status": 405,
                        "body": {"error": f"Method {method} not supported"}
                    }

                try:
                    response_body = response.json()
                except Exception:
                    response_body = {"text": response.text}

                return {
                    "type": "response",
                    "id": request_id,
                    "status": response.status_code,
                    "body": response_body
                }

        except Exception as e:
            return {
                "type": "response",
                "id": request_id,
                "status": 500,
                "body": {"error": str(e)}
            }

    def stop(self):
        """Stop the relay client."""
        self._running = False
        self.should_reconnect = False


def run_relay_client(
    cloud_url: str = DEFAULT_CLOUD_URL,
    local_port: int = 18081,
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
    local_port: int = 18081,
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


def is_port_in_use(port: int) -> bool:
    """Check if a port is already in use."""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('127.0.0.1', port)) == 0


def start_server_in_background(port: int = 18081, home_dir: Optional[str] = None, working_dir: Optional[str] = None):
    """Start the local agent server in a background thread."""
    import threading

    # Check if port is already in use
    if is_port_in_use(port):
        print(f"[Relay] Port {port} is already in use - skipping local server")
        print(f"[Relay] Another relay might be running. Kill it with: lsof -ti:{port} | xargs kill -9")
        return None

    def run():
        from .local_server import run_server, set_default_working_dir, set_home_directory
        if home_dir:
            set_home_directory(home_dir)
        if working_dir:
            set_default_working_dir(working_dir)
        elif home_dir:
            set_default_working_dir(home_dir)
        run_server(port=port)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Wait a moment and verify the server started
    import time
    time.sleep(0.5)
    if is_port_in_use(port):
        print(f"[Relay] Local agent server started on port {port}")
    else:
        print(f"[Relay] Warning: Server may have failed to start on port {port}")

    return thread


def setup_mcp_config(working_dir: str, cloud_url: str = DEFAULT_CLOUD_URL) -> bool:
    """
    Set up MCP config in the project's .mcp.json file.
    Returns True if config was created or updated.
    """
    mcp_file = Path(working_dir) / ".mcp.json"

    # The MCP server config to add
    mcp_server_config = {
        "command": "uvx",
        "args": ["--from", "git+https://github.com/gneyal/p_69_branch_monkey_mcp.git", "branch-monkey-mcp"],
        "env": {
            "BRANCH_MONKEY_API_URL": cloud_url
        }
    }

    try:
        if mcp_file.exists():
            # Read existing config
            with open(mcp_file, "r") as f:
                config = json.load(f)

            # Ensure mcpServers exists
            if "mcpServers" not in config:
                config["mcpServers"] = {}

            # Check if already configured
            if "branch-monkey-cloud" in config["mcpServers"]:
                print(f"[MCP] Config already exists in {mcp_file}")
                return False

            # Add our config
            config["mcpServers"]["branch-monkey-cloud"] = mcp_server_config

            with open(mcp_file, "w") as f:
                json.dump(config, f, indent=2)

            print(f"[MCP] Added branch-monkey-cloud to {mcp_file}")
            return True
        else:
            # Create new config
            config = {
                "mcpServers": {
                    "branch-monkey-cloud": mcp_server_config
                }
            }

            with open(mcp_file, "w") as f:
                json.dump(config, f, indent=2)

            print(f"[MCP] Created {mcp_file} with branch-monkey-cloud config")
            return True

    except Exception as e:
        print(f"[MCP] Warning: Could not set up MCP config: {e}")
        return False


def main():
    """CLI entry point for branch-monkey-relay."""
    # Ensure output is not buffered (for background processes)
    sys.stdout.reconfigure(line_buffering=True)

    import argparse

    # Resolve cloud URL dynamically from /api/config
    resolved_cloud_url = fetch_cloud_url_from_config(FALLBACK_CLOUD_URL)

    parser = argparse.ArgumentParser(
        description="Connect your machine to Kompany Cloud"
    )
    parser.add_argument(
        "--cloud-url",
        default=os.environ.get("BRANCH_MONKEY_CLOUD_URL", resolved_cloud_url),
        help=f"Cloud URL (default: auto-detected from config)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("BRANCH_MONKEY_LOCAL_PORT", "18081")),
        help="Local server port (default: 18081)"
    )
    parser.add_argument(
        "--name",
        default=None,
        help="Machine name (default: hostname)"
    )
    parser.add_argument(
        "--no-server",
        action="store_true",
        help="Skip starting local server (use if server is running separately)"
    )
    parser.add_argument(
        "--no-mcp",
        action="store_true",
        help="Skip setting up MCP config in .mcp.json"
    )
    parser.add_argument(
        "--dir", "-d",
        default=os.getcwd(),
        help="Working directory for agent execution (default: current directory)"
    )

    args = parser.parse_args()

    # Check for working directory in this order:
    # 1. --dir flag (if explicitly provided, not default)
    # 2. BRANCH_MONKEY_WORKING_DIR environment variable
    # 3. Current directory (default)
    env_working_dir = os.environ.get("BRANCH_MONKEY_WORKING_DIR")
    dir_explicitly_set = args.dir != os.getcwd()

    if dir_explicitly_set:
        # --dir was explicitly provided
        working_dir = os.path.abspath(args.dir)
    elif env_working_dir:
        # Use environment variable
        working_dir = os.path.abspath(os.path.expanduser(env_working_dir))
        print(f"[Relay] Using working directory from BRANCH_MONKEY_WORKING_DIR: {working_dir}")
    else:
        # Use current directory
        working_dir = os.getcwd()

    # Validate the directory exists
    if not os.path.isdir(working_dir):
        print(f"[Relay] Error: Directory does not exist: {working_dir}")
        sys.exit(1)

    # Set up MCP config unless --no-mcp is specified
    if not args.no_mcp:
        setup_mcp_config(working_dir, args.cloud_url)

    # Determine home directory (parent of projects) vs current project
    # Home is typically the Code folder, project is a subfolder
    home_dir = working_dir
    current_project = None

    # Check if working_dir looks like a project (has .git, package.json, etc.)
    project_markers = ['.git', 'package.json', 'pyproject.toml', 'Cargo.toml', 'go.mod', 'pom.xml']
    is_project = any(os.path.exists(os.path.join(working_dir, marker)) for marker in project_markers)

    if is_project:
        # working_dir is a project, home is its parent
        current_project = working_dir
        home_dir = os.path.dirname(working_dir)

    print(f"")
    print(f"\033[1mKompany Relay\033[0m v{VERSION}")
    print(f"")
    print(f"  \033[38;2;107;114;128mThis connects your machine to kompany.dev so you can\033[0m")
    print(f"  \033[38;2;107;114;128mrun AI agents on your local codebase from the cloud.\033[0m")
    print(f"")
    print(f"  Home:      \033[1m{home_dir}\033[0m")
    if current_project:
        project_name = os.path.basename(current_project)
        print(f"  Project:   \033[1m{project_name}\033[0m \033[38;2;107;114;128m({current_project})\033[0m")
    else:
        print(f"  Project:   \033[38;2;107;114;128m(none selected - pick one in dashboard)\033[0m")
    print(f"  Dashboard: \033[1mhttp://localhost:{args.port}/\033[0m")
    print(f"")

    # Start local agent server unless --no-server is specified
    if not args.no_server:
        print(f"\033[38;2;107;114;128mStarting local server...\033[0m")
        start_server_in_background(port=args.port, home_dir=home_dir, working_dir=current_project)
        time.sleep(1)
    else:
        print(f"\033[38;2;107;114;128mSkipping local server (--no-server)\033[0m")

    print(f"\033[38;2;107;114;128mConnecting to {args.cloud_url}...\033[0m")
    run_relay_client(
        cloud_url=args.cloud_url,
        local_port=args.port,
        machine_name=args.name
    )


if __name__ == "__main__":
    main()
