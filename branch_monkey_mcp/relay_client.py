"""
Relay Client for Branch Monkey Cloud

This module allows a local machine to connect to Branch Monkey Cloud
and receive relayed requests from the web UI.

The client:
1. Authenticates using device auth flow (if no cached token)
2. Gets connection config from cloud (Supabase URL, key, etc.)
3. Connects to Supabase Realtime channel
4. Registers as a compute node
5. Receives requests and executes them locally
6. Streams responses back through the channel

Usage:
    branch-monkey-relay
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

# Config file location
CONFIG_DIR = Path.home() / ".branch-monkey"
TOKEN_FILE = CONFIG_DIR / "relay_token.json"

# Cloud API URL
DEFAULT_CLOUD_URL = "https://p-63-branch-monkey.pages.dev"


class RelayClient:
    """
    Relay client that connects local machine to Branch Monkey Cloud
    using Supabase Realtime.

    Handles:
    - Device authentication flow
    - Supabase Realtime connection
    - Request/response relay
    - Auto-reconnection
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
        self.machine_id = f"{self.machine_name}-{os.getpid()}"

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

    def _get_machine_name(self) -> str:
        """Generate a human-readable machine name."""
        return socket.gethostname()

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

    async def connect(self):
        """
        Connect to Supabase Realtime and start receiving requests.
        """
        if not self.relay_config:
            if not await self.authenticate():
                print("[Relay] Authentication failed, cannot connect")
                return

        # Import supabase here to avoid import errors if not installed
        try:
            from supabase import acreate_client, AsyncClient
        except ImportError:
            print("[Relay] Error: supabase library not installed")
            print("[Relay] Install with: pip install supabase")
            return

        supabase_url = self.relay_config.get("supabase_url")
        supabase_key = self.relay_config.get("supabase_key")
        channel_prefix = self.relay_config.get("channel_prefix", "relay")

        if not supabase_url or not supabase_key:
            print("[Relay] Error: Missing Supabase config")
            return

        print(f"\n[Relay] Connecting to Supabase Realtime...")
        print(f"[Relay] User ID: {self.user_id}")
        print(f"[Relay] Machine ID: {self.machine_id}")
        print(f"[Relay] Machine name: {self.machine_name}")
        print(f"[Relay] Local port: {self.local_port}")

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
        self.channel.on_broadcast("ping", lambda _: self._send_pong())
        self.channel.on_broadcast("disconnect", on_disconnect)

        # Subscribe
        await self.channel.subscribe()

        print(f"\n[Relay] Connected to Supabase Realtime!")
        print(f"[Relay] Channel: {channel_name}")
        print(f"[Relay] Ready to receive requests from cloud\n")

        # Register this machine
        await self._register_machine()

        # Send initial heartbeat to local server
        await self._send_local_heartbeat()

        self._running = True

        # Start heartbeat loop
        heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            print("\n[Relay] Shutting down...")
        finally:
            self._running = False
            heartbeat_task.cancel()
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
        """Send periodic heartbeats to keep connection alive."""
        while self._running:
            try:
                await asyncio.sleep(25)
                # Heartbeat to Supabase
                await self.supabase.table("compute_nodes").update({
                    "last_heartbeat": datetime.utcnow().isoformat(),
                    "status": "online"
                }).eq("machine_id", self.machine_id).execute()
                # Heartbeat to local server (so dashboard knows relay is connected)
                await self._send_local_heartbeat()
            except Exception as e:
                print(f"[Relay] Heartbeat error: {e}")

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
            msg_type = payload.get("type", "request")
            print(f"[Relay] Received {msg_type}: {payload.get('method', '')} {payload.get('path', '')}")

            if msg_type in ("request", "stream_request"):
                response = await self._execute_local_request(payload)
                self.channel.send_broadcast("response", response)
                print(f"[Relay] Sent response: status={response.get('status')}")

        except Exception as e:
            print(f"[Relay] Error handling message: {e}")

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


def start_server_in_background(port: int = 18081, working_dir: Optional[str] = None):
    """Start the local agent server in a background thread."""
    import threading

    def run():
        from .local_server import run_server, set_default_working_dir
        if working_dir:
            set_default_working_dir(working_dir)
        run_server(port=port)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    print(f"[Relay] Local agent server started on port {port}")
    if working_dir:
        print(f"[Relay] Working directory: {working_dir}")
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

    parser = argparse.ArgumentParser(
        description="Connect your machine to Branch Monkey Cloud"
    )
    parser.add_argument(
        "--cloud-url",
        default=os.environ.get("BRANCH_MONKEY_CLOUD_URL", DEFAULT_CLOUD_URL),
        help=f"Cloud URL (default: {DEFAULT_CLOUD_URL})"
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

    # Resolve working directory to absolute path
    working_dir = os.path.abspath(args.dir)

    # Set up MCP config unless --no-mcp is specified
    if not args.no_mcp:
        setup_mcp_config(working_dir, args.cloud_url)

    print(f"\n🐵 Branch Monkey Relay")
    print(f"   Cloud: {args.cloud_url}")
    print(f"   Local port: {args.port}")
    print(f"   Working dir: {working_dir}")

    # Start local agent server unless --no-server is specified
    if not args.no_server:
        print(f"\n[Relay] Starting local agent server...")
        start_server_in_background(port=args.port, working_dir=working_dir)
        # Give server time to start
        time.sleep(1)
    else:
        print(f"\n[Relay] Skipping local server (--no-server)")

    print(f"\n[Relay] Starting cloud relay connection...")
    run_relay_client(
        cloud_url=args.cloud_url,
        local_port=args.port,
        machine_name=args.name
    )


if __name__ == "__main__":
    main()
