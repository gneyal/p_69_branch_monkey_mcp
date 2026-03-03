"""
Relay Client for Kompany Cloud

This module allows a local machine to connect to Kompany Cloud
and receive relayed requests from the web UI.

VERSION: 4

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
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any

import httpx
import websockets

from .connection_logger import connection_logger


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
VERSION = "4"

# Config file location
CONFIG_DIR = Path.home() / ".kompany"
TOKEN_FILE = CONFIG_DIR / "relay_token.json"
MACHINE_ID_FILE = CONFIG_DIR / "machine_id"
PERSISTENT_CONFIG_FILE = CONFIG_DIR / "config.json"

# Cloud API URL - fallback if /api/config fetch fails
FALLBACK_CLOUD_URL = "https://kompany.dev"

# Stream bridge URL - Cloudflare Durable Object for direct streaming
DEFAULT_STREAM_BRIDGE_URL = "https://stream-bridge.gneyal.workers.dev"


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


def load_persistent_config() -> Dict[str, Any]:
    """Load persistent relay settings (home_dir, etc.)."""
    if PERSISTENT_CONFIG_FILE.exists():
        try:
            with open(PERSISTENT_CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_persistent_config(updates: Dict[str, Any]):
    """Save persistent relay settings (merges with existing)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = load_persistent_config()
    config.update(updates)
    with open(PERSISTENT_CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


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
        machine_name: Optional[str] = None,
        tui=None
    ):
        self.cloud_url = cloud_url.rstrip("/")
        self.local_port = local_port
        self.machine_name = machine_name or self._get_machine_name()
        self.machine_id = self._get_stable_machine_id()

        # Auth data
        self.access_token: Optional[str] = None
        self.user_id: Optional[str] = None
        self.org_id: Optional[str] = None
        self.user_email: Optional[str] = None
        self.org_name: Optional[str] = None

        # Relay config (from cloud)
        self.relay_config: Optional[Dict[str, Any]] = None

        # Supabase client
        self.supabase = None
        self.channel = None

        self._running = False

        # Stream bridge (Cloudflare DO) for direct streaming
        self._do_ws: Optional[websockets.WebSocketClientProtocol] = None
        self._do_ws_task: Optional[asyncio.Task] = None
        self._do_ws_reconnect = True
        self.stream_bridge_url: Optional[str] = None

        # Connection state tracking for auto-reconnect
        self.connection_state = ConnectionState.DISCONNECTED
        self.reconnect_attempts = 0
        self.last_successful_heartbeat: Optional[datetime] = None
        self.last_channel_activity: Optional[datetime] = None
        self._channel_liveness_failures = 0
        self.should_reconnect = True  # False when explicitly disconnected
        self._reconnect_task: Optional[asyncio.Task] = None
        self._health_check_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._background_tasks: set = set()
        self._do_reconnect_attempts = 0
        self._auth_refreshing = False
        self.tui = tui
        self._request_count = 0

    def _tui_update(self, **kwargs):
        """Update TUI state if active."""
        if self.tui:
            self.tui.update(**kwargs)

    def _create_tracked_task(self, coro):
        """Create an asyncio task that's tracked for cleanup."""
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return task

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

    async def _fetch_account_info(self):
        """Fetch user email and org name from the cloud API."""
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        if self.org_id:
            headers["X-Org-Id"] = self.org_id

        async with httpx.AsyncClient() as client:
            # Fetch org name (and user email from org membership)
            if not self.org_name:
                try:
                    resp = await client.get(
                        f"{self.cloud_url}/api/organizations",
                        headers=headers,
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        orgs = data.get("organizations", [])

                        # Try to match by org_id, otherwise use first org
                        if self.org_id:
                            for org in orgs:
                                if str(org.get("id")) == str(self.org_id):
                                    self.org_name = org.get("name")
                                    break
                        if not self.org_name and len(orgs) >= 1:
                            self.org_name = orgs[0].get("name")
                            # Also capture org_id if we didn't have one
                            if not self.org_id:
                                self.org_id = str(orgs[0].get("id"))

                        # Some endpoints return user info alongside orgs
                        if not self.user_email:
                            self.user_email = data.get("email") or data.get("user_email")
                except Exception as e:
                    print(f"[Relay] Could not fetch org info: {e}")

            # Fetch user email from /api/me
            if not self.user_email:
                try:
                    resp = await client.get(
                        f"{self.cloud_url}/api/me",
                        headers=headers,
                        timeout=10,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        self.user_email = (
                            data.get("email")
                            or data.get("user_email")
                            or data.get("user", {}).get("email")
                        )
                except Exception:
                    pass  # /api/me may not exist yet

    async def _fetch_account_info_supabase(self):
        """Fetch user/org info directly from Supabase after connecting."""
        if not self.supabase:
            return

        try:
            # Get user email from device_codes table
            if self.user_id and not self.user_email:
                result = await self.supabase.table("device_codes").select(
                    "user_id"
                ).eq("access_token", self.access_token).eq(
                    "status", "approved"
                ).limit(1).execute()
                # device_codes has user_id but not email; try auth admin
                # Fall back: check if compute_nodes has email-like data
                pass

            # Get org name from organizations table
            if not self.org_name:
                result = await self.supabase.table("organizations").select(
                    "id, name"
                ).execute()
                orgs = result.data or []
                if self.org_id:
                    for org in orgs:
                        if str(org.get("id")) == str(self.org_id):
                            self.org_name = org.get("name")
                            break
                if not self.org_name and len(orgs) >= 1:
                    self.org_name = orgs[0].get("name")
                    if not self.org_id:
                        self.org_id = str(orgs[0].get("id"))

            if self.org_name or self.user_email:
                self._tui_update(user_email=self.user_email, org_name=self.org_name)
                # Update cached token
                self._save_token({
                    "access_token": self.access_token,
                    "user_id": self.user_id,
                    "org_id": self.org_id,
                    "user_email": self.user_email,
                    "org_name": self.org_name,
                    "relay_config": self.relay_config,
                })
                if self.org_name:
                    print(f"[Relay] Organization: {self.org_name}")
                if self.user_email:
                    print(f"[Relay] User: {self.user_email}")
        except Exception as e:
            print(f"[Relay] Could not fetch account info from Supabase: {e}")

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
            self.user_email = cached.get("user_email")
            self.org_name = cached.get("org_name")
            self.relay_config = cached.get("relay_config")
            self.machine_id = cached.get("machine_id", self.machine_id)

            if self.relay_config:
                return True
            else:
                print("[Relay] Cached token missing relay config, re-authenticating...")
                self._clear_token()

        # Start device auth flow
        print("\n[Relay] Starting device authentication...")
        self._tui_update(auth_state="authenticating")
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
                self._tui_update(auth_state="failed")
                return False

            device_code = data["device_code"]
            user_code = data["user_code"]
            verification_uri = data["verification_uri"]
            expires_in = data.get("expires_in", 900)
            interval = data.get("interval", 5)

            self._tui_update(auth_state="waiting", auth_url=verification_uri, auth_code=user_code)

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
                        self.user_email = data.get("user_email")
                        self.org_name = data.get("org_name")
                        self.relay_config = data.get("relay_config")

                        if not self.relay_config:
                            print("[Relay] Error: No relay config in response")
                            return False

                        # Fetch account info (user email, org name) if not in auth response
                        await self._fetch_account_info()

                        # Save everything
                        self._save_token({
                            "access_token": self.access_token,
                            "user_id": self.user_id,
                            "org_id": self.org_id,
                            "user_email": self.user_email,
                            "org_name": self.org_name,
                            "relay_config": self.relay_config
                        })

                        print("\n[Relay] Authentication successful!")
                        self._tui_update(auth_state="authenticated")
                        return True

                    elif data.get("error") == "access_denied":
                        print("[Relay] Authentication denied")
                        self._tui_update(auth_state="failed")
                        return False
                    elif data.get("error") == "expired_token":
                        print("[Relay] Device code expired")
                        self._tui_update(auth_state="failed")
                        return False

                    # Still pending
                    if poll_count % 6 == 0:
                        print("[Relay] Still waiting for approval...")

                except Exception as e:
                    print(f"[Relay] Polling error: {e}")

            print("[Relay] Authentication timed out")
            self._tui_update(auth_state="failed")
            return False

    async def _connect_channel(self) -> bool:
        """
        Internal method to establish Supabase Realtime connection.
        Returns True if successful, False otherwise.
        """
        # Import supabase here to avoid import errors if not installed
        try:
            from supabase import acreate_client, AsyncClient
            from supabase.lib.client_options import AsyncClientOptions
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
        self._tui_update(connection="connecting")

        print(f"\n[Relay] Connecting to Supabase Realtime...")
        if self.user_email:
            print(f"[Relay] User: {self.user_email}")
        if self.org_name:
            print(f"[Relay] Organization: {self.org_name}")
        print(f"[Relay] Machine: {self.machine_name} ({self.machine_id})")
        print(f"[Relay] Local port: {self.local_port}")

        try:
            # Create Supabase client with higher realtime retries for long-running relay
            options = AsyncClientOptions(
                realtime={"max_retries": 50, "initial_backoff": 1.0}
            )
            self.supabase = await acreate_client(supabase_url, supabase_key, options)

            # Channel name for this machine
            channel_name = f"{channel_prefix}:{self.user_id}:{self.machine_id}"

            # Subscribe to channel
            self.channel = self.supabase.channel(channel_name)

            # Handle incoming messages
            def on_request(payload):
                self._create_tracked_task(self._handle_message(payload))

            def on_disconnect(payload):
                print(f"\n[Relay] Received disconnect command from cloud")
                self._create_tracked_task(self._shutdown())

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

            # Fetch account info via Supabase if not already known
            if not self.user_email or not self.org_name:
                await self._fetch_account_info_supabase()

            # Register this machine
            await self._register_machine()

            # Send initial heartbeat to local server
            await self._send_local_heartbeat()

            # Update state
            self.connection_state = ConnectionState.CONNECTED
            self.reconnect_attempts = 0
            self.last_successful_heartbeat = datetime.utcnow()
            self.last_channel_activity = datetime.utcnow()
            self._channel_liveness_failures = 0
            self._tui_update(connection="connected", connected_at=datetime.now(timezone.utc))

            connection_logger.log("connected", detail=f"Channel {channel_name}")

            # Connect to stream bridge (DO) for direct streaming
            await self._connect_stream_bridge()

            return True

        except Exception as e:
            print(f"[Relay] Connection failed: {e}")
            self.connection_state = ConnectionState.DISCONNECTED
            self._tui_update(connection="disconnected")
            connection_logger.log("connection_failed", error=str(e))
            return False

    async def _disconnect_channel(self):
        """Disconnect from Supabase Realtime channel and stream bridge."""
        # Close DO stream bridge
        self._do_ws_reconnect = False
        if self._do_ws_task and not self._do_ws_task.done():
            self._do_ws_task.cancel()
            self._do_ws_task = None
        if self._do_ws:
            try:
                await self._do_ws.close()
            except Exception:
                pass
            self._do_ws = None
            self._tui_update(stream_bridge=None)

        # Cancel tracked background tasks
        for task in list(self._background_tasks):
            if not task.done():
                task.cancel()
        self._background_tasks.clear()

        try:
            if self.channel and self.supabase:
                await self.supabase.remove_channel(self.channel)
                self.channel = None

            # Close the Supabase realtime client to stop its internal tasks
            if self.supabase:
                try:
                    await self.supabase.realtime.close()
                except Exception:
                    pass
                self.supabase = None

            self.connection_state = ConnectionState.DISCONNECTED
            self._channel_liveness_failures = 0
            self._tui_update(connection="disconnected")
            connection_logger.log("disconnected", detail="Channel removed")
            print("[Relay] Disconnected from channel")
        except Exception as e:
            print(f"[Relay] Error during disconnect: {e}")

    async def _connect_stream_bridge(self):
        """Connect to Cloudflare DO stream bridge for direct streaming."""
        # Resolve stream bridge URL from config, env, or default
        url = (
            (self.relay_config or {}).get("stream_bridge_url")
            or os.environ.get("STREAM_BRIDGE_URL")
            or DEFAULT_STREAM_BRIDGE_URL
        )
        # Allow disabling with empty string
        if not url:
            print("[Relay] Stream bridge disabled (empty URL)")
            return

        self.stream_bridge_url = url
        ws_url = url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/")
        ws_url = f"{ws_url}/relay/{self.machine_id}?token={self.access_token}"

        try:
            self._do_ws = await websockets.connect(ws_url, ping_interval=30, ping_timeout=10)
            self._do_ws_reconnect = True
            self._do_reconnect_attempts = 0  # Reset backoff on success
            print(f"[Relay] Connected to stream bridge: {self.stream_bridge_url}")
            connection_logger.log("stream_bridge_connected", detail=self.stream_bridge_url)
            self._tui_update(stream_bridge=True)

            # Start listener for incoming messages (stream_start from browsers)
            self._do_ws_task = asyncio.create_task(self._do_ws_listen())
        except Exception as e:
            print(f"[Relay] Could not connect to stream bridge: {e}")
            connection_logger.log("stream_bridge_failed", error=str(e))
            self._do_ws = None
            self._tui_update(stream_bridge=str(e)[:60])

    async def _do_ws_listen(self):
        """Listen for messages from the DO stream bridge (browser → relay)."""
        try:
            async for raw in self._do_ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")
                if msg_type == "stream_start":
                    self._create_tracked_task(self._handle_stream_start(data, via_do=True))
                elif msg_type == "stream_stop":
                    print(f"[Relay] Stream stop via DO: stream_id={data.get('stream_id')}")
                elif msg_type == "ping":
                    try:
                        await self._do_ws.send(json.dumps({"type": "pong"}))
                    except Exception:
                        pass
        except websockets.ConnectionClosed as e:
            print(f"[Relay] Stream bridge disconnected: code={e.code} reason={e.reason}")
            connection_logger.log("stream_bridge_disconnected", error=str(e))
        except asyncio.CancelledError:
            return
        except Exception as e:
            print(f"[Relay] Stream bridge listener error: {e}")
            connection_logger.log("stream_bridge_error", error=str(e))
        finally:
            self._do_ws = None
            self._tui_update(stream_bridge=False)
            # Auto-reconnect if still running
            if self._do_ws_reconnect and self._running and self.connection_state == ConnectionState.CONNECTED:
                self._create_tracked_task(self._reconnect_stream_bridge())

    async def _reconnect_stream_bridge(self):
        """Reconnect to the stream bridge with exponential backoff (5-60s)."""
        delay = min(5 * (2 ** self._do_reconnect_attempts), 60)
        jitter = delay * 0.2 * random.random()
        self._do_reconnect_attempts += 1
        total_delay = delay + jitter

        print(f"[Relay] Reconnecting to stream bridge in {total_delay:.1f}s (attempt {self._do_reconnect_attempts})...")
        await asyncio.sleep(total_delay)

        if self._running and self._do_ws_reconnect and self.connection_state == ConnectionState.CONNECTED:
            await self._connect_stream_bridge()

    async def _send_stream_data(self, use_do: bool, data: dict):
        """Send stream data via DO WebSocket or Supabase broadcast."""
        try:
            if use_do and self._do_ws:
                await self._do_ws.send(json.dumps(data))
            elif self.channel:
                await self.channel.send_broadcast("stream_event", data)
                self.last_channel_activity = datetime.utcnow()
        except Exception as e:
            connection_logger.log("channel_send_failed", error=str(e), detail="stream_event")
            raise  # Re-raise so callers can handle stream failures

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
            self._tui_update(connection="reconnecting", reconnect_count=self.reconnect_attempts)

            connection_logger.log(
                "reconnecting",
                detail=f"Attempt {self.reconnect_attempts}",
                attempt=self.reconnect_attempts,
                delay=delay,
            )
            print(f"[Relay] Reconnecting in {delay:.1f}s (attempt {self.reconnect_attempts})...")
            await asyncio.sleep(delay)

            if not self.should_reconnect or not self._running:
                return

            try:
                # Clean up old connection
                await self._disconnect_channel()

                # Attempt reconnection
                if await self._connect_channel():
                    connection_logger.log(
                        "reconnected",
                        detail=f"After {self.reconnect_attempts} attempts",
                        attempt=self.reconnect_attempts,
                    )
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

    async def _check_channel_alive(self) -> bool:
        """Test channel liveness by attempting a broadcast send."""
        if not self.channel:
            return False
        try:
            await self.channel.send_broadcast("heartbeat", {
                "machine_id": self.machine_id,
                "ts": datetime.utcnow().isoformat()
            })
            self.last_channel_activity = datetime.utcnow()
            return True
        except Exception as e:
            connection_logger.log("channel_send_failed", error=str(e), detail="liveness probe")
            return False

    async def _refresh_auth(self):
        """Re-authenticate in background when token expires (401)."""
        if self._auth_refreshing:
            return
        self._auth_refreshing = True
        try:
            print("[Relay] Re-authenticating...")
            self._clear_token()
            if await self.authenticate():
                connection_logger.log("auth_refreshed", detail="Token refreshed successfully")
                print("[Relay] Re-authentication successful")
                # Reconnect stream bridge with new token (it uses the relay token)
                if self._do_ws:
                    try:
                        await self._do_ws.close()
                    except Exception:
                        pass
                    self._do_ws = None
                    self._do_reconnect_attempts = 0
                    await self._connect_stream_bridge()
            else:
                connection_logger.log("auth_expired", detail="Re-authentication failed")
                print("[Relay] Re-authentication failed")
        except Exception as e:
            print(f"[Relay] Re-authentication error: {e}")
        finally:
            self._auth_refreshing = False

    async def _health_check_loop(self):
        """Monitor connection health via direct channel liveness probe.

        Instead of relying on cloud heartbeat staleness (which conflates
        cloud API health with channel health), we test the Supabase channel
        directly by attempting a broadcast send every 30s.
        """
        while self._running:
            try:
                await asyncio.sleep(CONNECTION_HEALTH_CHECK_INTERVAL)

                if self.connection_state != ConnectionState.CONNECTED:
                    self._channel_liveness_failures = 0
                    continue

                # Direct channel liveness test
                alive = await self._check_channel_alive()

                if alive:
                    self._channel_liveness_failures = 0
                    connection_logger.log("channel_liveness_ok")
                else:
                    self._channel_liveness_failures += 1
                    print(f"[Relay] Channel liveness probe failed ({self._channel_liveness_failures}x)")

                    # 2 consecutive failures → channel is dead, reconnect
                    if self._channel_liveness_failures >= 2:
                        connection_logger.log(
                            "health_check_triggered_reconnect",
                            detail=f"Channel liveness failed {self._channel_liveness_failures}x",
                            reason="channel_dead",
                        )
                        print(f"[Relay] Channel appears dead — reconnecting")
                        self._channel_liveness_failures = 0
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

        # Fetch account info if not cached (e.g. old token file)
        if not self.user_email or not self.org_name:
            await self._fetch_account_info()
            # Re-save token with updated info
            if self.user_email or self.org_name:
                self._save_token({
                    "access_token": self.access_token,
                    "user_id": self.user_id,
                    "org_id": self.org_id,
                    "user_email": self.user_email,
                    "org_name": self.org_name,
                    "relay_config": self.relay_config,
                })

        self._tui_update(
            user_email=self.user_email,
            org_name=self.org_name,
        )

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

            # Cancel all tracked fire-and-forget tasks
            for task in list(self._background_tasks):
                if not task.done():
                    task.cancel()
            self._background_tasks.clear()

            # Wait for tasks to finish
            tasks = [t for t in [self._heartbeat_task, self._health_check_task] if t]
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass

            await self._unregister_machine()

    async def _cloud_heartbeat(self, status: str = "online"):
        """Register/heartbeat via cloud API (bypasses RLS)."""
        headers = {}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.cloud_url}/api/relay/heartbeat",
                headers=headers,
                json={
                    "machine_id": self.machine_id,
                    "machine_name": self.machine_name,
                    "status": status,
                    "local_port": self.local_port,
                },
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json()

    async def _register_machine(self):
        """Register this machine as a compute node via the cloud API."""
        try:
            await self._cloud_heartbeat("online")
            print(f"[Relay] Registered compute node via cloud API")
            self._tui_update(registered=True)
        except Exception as e:
            print(f"[Relay] Warning: Could not register compute node: {e}")
            self._tui_update(registered=str(e)[:80])

    async def _heartbeat_loop(self):
        """Send periodic heartbeats to cloud API and local server.

        Cloud API failures (500s, timeouts) are logged but do NOT trigger
        Supabase reconnection — the channel liveness probe in _health_check_loop
        handles actual channel death separately.
        """
        consecutive_failures = 0

        while self._running:
            try:
                await asyncio.sleep(25)

                if self.connection_state != ConnectionState.CONNECTED:
                    continue

                try:
                    # Heartbeat to cloud API
                    await self._cloud_heartbeat("online")

                    # Success - reset failure counter
                    self.last_successful_heartbeat = datetime.utcnow()
                    consecutive_failures = 0
                    self._tui_update(last_heartbeat=datetime.now(timezone.utc))
                    connection_logger.log("heartbeat_ok")

                except Exception as e:
                    error_str = str(e)

                    # Handle 401 Unauthorized — trigger background re-auth
                    if "401" in error_str or "Unauthorized" in error_str:
                        connection_logger.log("auth_expired", detail="Cloud API returned 401, re-authenticating")
                        print(f"[Relay] Cloud API 401 — triggering re-authentication")
                        self._create_tracked_task(self._refresh_auth())
                        continue  # Don't count as heartbeat failure

                    consecutive_failures += 1
                    connection_logger.log(
                        "heartbeat_failed",
                        detail=f"Consecutive failure #{consecutive_failures}",
                        error=error_str,
                    )

                    if consecutive_failures <= 3 or consecutive_failures % 10 == 0:
                        print(f"[Relay] Cloud heartbeat failed ({consecutive_failures}x): {e}")

                    # Self-heal: after 7 consecutive failures (~3 min), trigger full reconnect
                    if consecutive_failures >= 7:
                        print(f"[Relay] {consecutive_failures} consecutive heartbeat failures — triggering reconnect")
                        connection_logger.log("heartbeat_triggered_reconnect", detail=f"{consecutive_failures} consecutive failures")
                        consecutive_failures = 0
                        self._trigger_reconnect()

                # Heartbeat to local server (so dashboard knows relay is connected)
                await self._send_local_heartbeat()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[Relay] Heartbeat loop error: {e}")

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
            await self._cloud_heartbeat("offline")
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
        connection_logger.log("shutdown", detail="Graceful shutdown")
        print("[Relay] Shutting down gracefully...")
        self._running = False
        self.should_reconnect = False
        await self._unregister_machine()
        print("[Relay] Disconnected. Goodbye!")
        sys.exit(0)

    def _send_pong(self):
        """Respond to ping with pong (wrapped in task for async send)."""
        async def _do_pong():
            try:
                await self.channel.send_broadcast("pong", {
                    "machine_id": self.machine_id,
                    "timestamp": datetime.utcnow().isoformat()
                })
                self.last_channel_activity = datetime.utcnow()
            except Exception as e:
                connection_logger.log("channel_send_failed", error=str(e), detail="pong")

        self._create_tracked_task(_do_pong())

    async def _handle_message(self, payload: Dict[str, Any]):
        """Handle incoming message from cloud."""
        try:
            # The broadcast payload may be nested: { event: "...", payload: { actual data } }
            # or flat: { actual data }
            actual_payload = payload.get("payload", payload) if isinstance(payload, dict) else payload

            msg_type = actual_payload.get("type", "request")
            self._request_count += 1
            self._tui_update(requests_handled=self._request_count)
            print(f"[Relay] Received {msg_type}: {actual_payload.get('method', '')} {actual_payload.get('path', '')}")

            if msg_type == "stream_start":
                # Start SSE streaming for an agent
                self._create_tracked_task(self._handle_stream_start(actual_payload))
            elif msg_type in ("request", "stream_request"):
                response = await self._execute_local_request(actual_payload)
                try:
                    await self.channel.send_broadcast("response", response)
                    self.last_channel_activity = datetime.utcnow()
                    print(f"[Relay] Sent response: status={response.get('status')}")
                except Exception as e:
                    connection_logger.log("channel_send_failed", error=str(e), detail="response")
                    print(f"[Relay] Failed to send response: {e}")

        except Exception as e:
            print(f"[Relay] Error handling message: {e}")

    async def _handle_stream_start(self, payload: Dict[str, Any], via_do: bool = False):
        """Handle SSE stream start request - connect to local SSE and forward events.

        Args:
            payload: The stream_start message with stream_id and agent_id.
            via_do: If True, the request came via the DO WebSocket, so stream
                    events back through the DO. Otherwise use Supabase broadcast.
        """
        stream_id = payload.get("stream_id")
        agent_id = payload.get("agent_id")

        if not stream_id or not agent_id:
            print(f"[Relay] Stream start missing stream_id or agent_id")
            return

        use_do = via_do and self._do_ws is not None
        transport = "DO bridge" if use_do else "Supabase"

        url = f"http://127.0.0.1:{self.local_port}/api/local-claude/agents/{agent_id}/stream"
        print(f"[Relay] Starting SSE stream for agent {agent_id}, stream_id={stream_id} via {transport}")

        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url) as response:
                    if response.status_code != 200:
                        await self._send_stream_data(use_do, {
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
                                    print(f"[Relay] Forwarding event #{event_count} type={event_type} via {transport}")
                                # Forward the event
                                await self._send_stream_data(use_do, {
                                    "stream_id": stream_id,
                                    "event": event
                                })

                                # Check for exit event
                                if event.get("type") == "exit":
                                    print(f"[Relay] Stream ended for agent {agent_id}")
                                    break

                            except json.JSONDecodeError:
                                # Forward raw data if not JSON
                                await self._send_stream_data(use_do, {
                                    "stream_id": stream_id,
                                    "raw": data
                                })

        except Exception as e:
            connection_logger.log(
                "stream_error",
                detail=f"Agent {agent_id}, stream {stream_id}",
                error=str(e),
            )
            print(f"[Relay] Stream error for agent {agent_id}: {e}")
            try:
                await self._send_stream_data(use_do, {
                    "stream_id": stream_id,
                    "type": "error",
                    "error": str(e)
                })
            except Exception:
                pass

    async def _execute_local_request(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Execute request on local server and return response."""
        request_id = request.get("id")
        method = request.get("method", "GET")
        path = request.get("path", "/")
        body = request.get("body", {})
        headers = request.get("headers", {})

        url = f"http://127.0.0.1:{self.local_port}{path}"

        try:
            # Longer timeout for POST/PUT/PATCH as they may involve AI operations
            read_timeout = 55 if method == "GET" else 180
            async with httpx.AsyncClient() as client:
                if method == "GET":
                    response = await client.get(url, headers=headers, timeout=read_timeout)
                elif method == "POST":
                    response = await client.post(url, json=body, headers=headers, timeout=read_timeout)
                elif method == "PUT":
                    response = await client.put(url, json=body, headers=headers, timeout=read_timeout)
                elif method == "DELETE":
                    response = await client.delete(url, headers=headers, timeout=read_timeout)
                elif method == "PATCH":
                    response = await client.patch(url, json=body, headers=headers, timeout=read_timeout)
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
            import traceback
            print(f"[Relay] Request handler exception: {type(e).__name__}: {e}")
            traceback.print_exc()
            return {
                "type": "response",
                "id": request_id,
                "status": 500,
                "body": {"error": str(e) or f"{type(e).__name__}: unknown error"}
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
        from .bridge_and_local_actions import run_server, set_default_working_dir, set_home_directory
        if home_dir:
            set_home_directory(home_dir)
        if working_dir:
            set_default_working_dir(working_dir)
        elif home_dir:
            set_default_working_dir(home_dir)
        run_server(port=port)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Poll for server readiness (up to 3 seconds)
    import time
    for _ in range(15):
        time.sleep(0.2)
        if is_port_in_use(port):
            print(f"[Relay] Local agent server started on port {port}")
            return thread

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


def _run_with_tui(args, home_dir, current_project, onboarding_needed=False):
    """Run the relay with terminal UI."""
    import threading
    from .relay_tui import RelayTUI

    tui = RelayTUI()

    # Callback when user sets home dir during onboarding or [H] edit
    def on_home_set(path):
        save_persistent_config({"home_dir": path})
        try:
            from .bridge_and_local_actions import set_home_directory
            set_home_directory(path)
        except Exception:
            pass

    tui._on_home_set = on_home_set

    # Callback when user toggles launchd service (install/uninstall)
    def on_launchd_toggle(do_install):
        import subprocess
        if do_install:
            home = tui.state.get("home_dir")
            if install_launchd_service(home):
                tui.update(launchd="running")
                print("[Relay] Launchd service installed.")
            else:
                tui.update(launchd="error")
                print("[Relay] Failed to install launchd service.")
        else:
            # Uninstall
            if LAUNCHD_PLIST_PATH.exists():
                subprocess.run(["launchctl", "unload", str(LAUNCHD_PLIST_PATH)], capture_output=True)
                LAUNCHD_PLIST_PATH.unlink(missing_ok=True)
            tui.update(launchd="not_installed")
            print("[Relay] Launchd service removed.")
        tui.update(launchd_prompt="done")

    tui._on_launchd_install = on_launchd_toggle

    # Callback when user logs out
    def on_logout():
        if TOKEN_FILE.exists():
            TOKEN_FILE.unlink()
        print("[Relay] Logged out. Token cleared.")

    tui._on_logout = on_logout

    # Detect current launchd status
    if sys.platform == "darwin":
        ld_status = check_launchd_status()
        if ld_status["running"]:
            launchd_state = "running"
        elif ld_status["installed"]:
            launchd_state = "installed"
        else:
            launchd_state = "not_installed"
    else:
        launchd_state = None

    # Pre-populate user/org info from cached token
    cached_user_email = None
    cached_org_name = None
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE) as f:
                cached = json.load(f)
                cached_user_email = cached.get("user_email")
                cached_org_name = cached.get("org_name")
        except Exception:
            pass

    tui.update(
        version=VERSION,
        machine_name=args.name or socket.gethostname(),
        home_dir=home_dir,
        project=os.path.basename(current_project) if current_project else None,
        project_path=current_project,
        port=args.port,
        dashboard_url=f"http://localhost:{args.port}/",
        cloud_url=args.cloud_url,
        user_email=cached_user_email,
        org_name=cached_org_name,
        onboarding_needed=onboarding_needed,
        launchd=launchd_state,
    )
    tui.install_capture()

    # Start local server
    if not args.no_server:
        start_server_in_background(
            port=args.port,
            home_dir=home_dir,
            working_dir=current_project,
        )
        tui.update(server_running=is_port_in_use(args.port))

    # Start relay in background thread
    relay_ref = [None]

    def run_relay():
        client = RelayClient(
            cloud_url=args.cloud_url,
            local_port=args.port,
            machine_name=args.name,
            tui=tui,
        )
        relay_ref[0] = client
        try:
            asyncio.run(client.connect())
        except Exception as e:
            print(f"[Relay] Error: {e}")

    relay_thread = threading.Thread(target=run_relay, daemon=True)
    relay_thread.start()

    # TUI runs in main thread (blocks until quit)
    tui.run(stop_callback=lambda: relay_ref[0] and relay_ref[0].stop())


LAUNCHD_LABEL = "dev.kompany.relay"
LAUNCHD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def check_launchd_status() -> dict:
    """Check launchd service status. Returns dict with installed, running, pid."""
    import subprocess

    if sys.platform != "darwin":
        return {"installed": False, "running": False, "pid": None}

    if not LAUNCHD_PLIST_PATH.exists():
        return {"installed": False, "running": False, "pid": None}

    result = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {"installed": True, "running": False, "pid": None}

    # Parse output — launchctl list <label> outputs lines like: "PID" = 1234;
    pid = None
    for line in result.stdout.strip().splitlines():
        cols = line.split()
        if len(cols) >= 1 and cols[-1] == LAUNCHD_LABEL:
            pid = cols[0] if cols[0] != "-" else None
            break

    return {"installed": True, "running": pid is not None, "pid": pid}


def install_launchd_service(home_dir: str = None) -> bool:
    """Install the relay as a launchd service. Returns True on success."""
    import shutil
    import subprocess

    if sys.platform != "darwin":
        return False

    binary = shutil.which("branch-monkey-relay")
    if not binary:
        return False

    # Build ProgramArguments — always --no-tui since launchd has no TTY
    program_args = [binary, "--no-tui"]

    if not home_dir:
        persistent_cfg = load_persistent_config()
        home_dir = persistent_cfg.get("home_dir")
    if home_dir:
        program_args.extend(["--dir", home_dir])

    # Build the plist XML
    args_xml = "\n".join(f"        <string>{a}</string>" for a in program_args)
    current_path = os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>
    <key>WorkingDirectory</key>
    <string>{home_dir or str(Path.home() / "Code")}</string>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>{CONFIG_DIR / "relay.log"}</string>
    <key>StandardErrorPath</key>
    <string>{CONFIG_DIR / "relay.err.log"}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{current_path}</string>
    </dict>
</dict>
</plist>
"""

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Unload first if already loaded (ignore errors)
    if LAUNCHD_PLIST_PATH.exists():
        subprocess.run(
            ["launchctl", "unload", str(LAUNCHD_PLIST_PATH)],
            capture_output=True,
        )

    LAUNCHD_PLIST_PATH.write_text(plist_content)

    result = subprocess.run(
        ["launchctl", "load", str(LAUNCHD_PLIST_PATH)],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _launchd_install():
    """CLI handler for 'branch-monkey-relay install'."""
    import shutil

    if sys.platform != "darwin":
        print("Error: launchd services are only supported on macOS.")
        sys.exit(1)

    binary = shutil.which("branch-monkey-relay")
    if not binary:
        print("Error: 'branch-monkey-relay' not found in PATH.")
        print("Make sure it's installed (pip install -e . or pip install branch-monkey-mcp).")
        sys.exit(1)

    persistent_cfg = load_persistent_config()
    home_dir = persistent_cfg.get("home_dir")

    if install_launchd_service(home_dir):
        print(f"Service '{LAUNCHD_LABEL}' installed and started.")
        print(f"  Plist: {LAUNCHD_PLIST_PATH}")
        print(f"  Logs:  {CONFIG_DIR / 'relay.log'}")
        print(f"  Errors: {CONFIG_DIR / 'relay.err.log'}")
        print()
        print("The relay will auto-start on login and restart if it crashes.")
        print("Use 'branch-monkey-relay uninstall' to remove the service.")
    else:
        print("Error: Failed to install launchd service.")
        sys.exit(1)


def _launchd_uninstall():
    """Uninstall the branch-monkey-relay launchd service."""
    import subprocess

    if sys.platform != "darwin":
        print("Error: launchd services are only supported on macOS.")
        sys.exit(1)

    if not LAUNCHD_PLIST_PATH.exists():
        print(f"Service not installed (no plist at {LAUNCHD_PLIST_PATH}).")
        return

    # Unload the service
    result = subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST_PATH)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"Warning: launchctl unload returned: {result.stderr.strip()}")

    # Remove the plist
    LAUNCHD_PLIST_PATH.unlink()
    print(f"Service '{LAUNCHD_LABEL}' uninstalled.")
    print(f"Removed {LAUNCHD_PLIST_PATH}")


def _launchd_status():
    """CLI handler for 'branch-monkey-relay status'."""
    if sys.platform != "darwin":
        print("Error: launchd services are only supported on macOS.")
        sys.exit(1)

    status = check_launchd_status()
    if not status["installed"]:
        print(f"Service not installed (no plist at {LAUNCHD_PLIST_PATH}).")
        return

    print(f"Service '{LAUNCHD_LABEL}':")
    print(f"  Plist:  {LAUNCHD_PLIST_PATH}")
    if status["running"]:
        print(f"  PID:    {status['pid']}")
        print(f"  Status: running")
    else:
        print(f"  Status: installed but not running")

    log_path = CONFIG_DIR / "relay.log"
    if log_path.exists():
        print(f"  Log:    {log_path}")


def main():
    """CLI entry point for branch-monkey-relay."""
    # Ensure output is not buffered (for background processes)
    sys.stdout.reconfigure(line_buffering=True)

    # Handle subcommands before argparse
    if len(sys.argv) > 1 and sys.argv[1] in ("install", "uninstall", "status"):
        cmd = sys.argv[1]
        if cmd == "install":
            _launchd_install()
        elif cmd == "uninstall":
            _launchd_uninstall()
        elif cmd == "status":
            _launchd_status()
        return

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
    parser.add_argument(
        "--no-tui",
        action="store_true",
        help="Disable terminal UI, show raw logs instead"
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

    # Load persistent config (saved home_dir from onboarding)
    persistent_cfg = load_persistent_config()
    onboarding_needed = "home_dir" not in persistent_cfg

    # Use saved home_dir if available and no explicit --dir was set
    if not dir_explicitly_set and not env_working_dir:
        saved_home = persistent_cfg.get("home_dir")
        if saved_home and os.path.isdir(saved_home):
            home_dir = saved_home

    # Terminal UI mode (default when running in a terminal)
    use_tui = not args.no_tui and sys.stdout.isatty()
    if use_tui:
        try:
            from .relay_tui import RelayTUI  # noqa: F401
            _run_with_tui(args, home_dir, current_project, onboarding_needed=onboarding_needed)
            return
        except ImportError:
            pass  # Fall through to raw logs

    # Load cached account info for display
    cached_user_email = None
    cached_org_name = None
    if TOKEN_FILE.exists():
        try:
            with open(TOKEN_FILE) as f:
                cached = json.load(f)
                cached_user_email = cached.get("user_email")
                cached_org_name = cached.get("org_name")
        except Exception:
            pass

    print(f"")
    print(f"\033[1mKompany Relay\033[0m v{VERSION}")
    print(f"")
    print(f"  \033[38;2;107;114;128mThis connects your machine to kompany.dev so you can\033[0m")
    print(f"  \033[38;2;107;114;128mrun AI agents on your local codebase from the cloud.\033[0m")
    print(f"")
    if cached_user_email:
        print(f"  User:      \033[1m{cached_user_email}\033[0m")
    if cached_org_name:
        print(f"  Org:       \033[1m{cached_org_name}\033[0m")
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
