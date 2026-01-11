"""
WebSocket Relay Manager for Cloud-to-Local Communication

This module manages WebSocket connections from local servers to the cloud,
enabling the cloud frontend to communicate with local Claude Code agents.

Architecture:
- Local server runs `branch-monkey-relay` and connects to cloud WebSocket
- Cloud stores connection mapped to user ID
- When user clicks "Local" in cloud UI, requests relay through WebSocket
- Output streams back through the same connection

Message Protocol:
- Request: {type: "request", id: "uuid", method: "POST", path: "/...", body: {...}}
- Response: {type: "response", id: "uuid", status: 200, body: {...}}
- Stream: {type: "stream", id: "uuid", event: "output", data: "..."}
- Control: {type: "ping"}, {type: "pong"}, {type: "disconnect"}
"""

import asyncio
import json
import uuid
from datetime import datetime
from typing import Dict, Optional, Set, Any
from dataclasses import dataclass, field


@dataclass
class RelayConnection:
    """Represents an active relay connection from a local server."""
    websocket: Any  # WebSocket type varies by framework
    user_id: str
    machine_id: str
    machine_name: str
    connected_at: datetime = field(default_factory=datetime.utcnow)
    last_heartbeat: datetime = field(default_factory=datetime.utcnow)
    capabilities: Dict[str, Any] = field(default_factory=dict)
    # Pending requests waiting for response
    pending_requests: Dict[str, asyncio.Future] = field(default_factory=dict)
    # Active streams (request_id -> set of listener queues)
    active_streams: Dict[str, Set[asyncio.Queue]] = field(default_factory=dict)


class RelayManager:
    """Manages WebSocket relay connections from local servers."""

    def __init__(self):
        # user_id -> {machine_id -> RelayConnection}
        self._connections: Dict[str, Dict[str, RelayConnection]] = {}
        # Machine ID to user ID lookup for quick access
        self._machine_to_user: Dict[str, str] = {}
        # Request timeout in seconds
        self._request_timeout = 60

    def register(
        self,
        websocket: Any,
        user_id: str,
        machine_id: str,
        machine_name: str,
        capabilities: Optional[Dict[str, Any]] = None
    ) -> RelayConnection:
        """Register a new relay connection."""
        conn = RelayConnection(
            websocket=websocket,
            user_id=user_id,
            machine_id=machine_id,
            machine_name=machine_name,
            capabilities=capabilities or {}
        )

        if user_id not in self._connections:
            self._connections[user_id] = {}

        # Close existing connection for same machine if exists
        if machine_id in self._connections[user_id]:
            old_conn = self._connections[user_id][machine_id]
            # Cancel any pending requests
            for future in old_conn.pending_requests.values():
                if not future.done():
                    future.set_exception(ConnectionError("Connection replaced"))

        self._connections[user_id][machine_id] = conn
        self._machine_to_user[machine_id] = user_id

        print(f"[Relay] Registered connection: user={user_id}, machine={machine_id}, name={machine_name}")
        return conn

    def unregister(self, machine_id: str) -> None:
        """Unregister a relay connection."""
        user_id = self._machine_to_user.get(machine_id)
        if not user_id:
            return

        if user_id in self._connections and machine_id in self._connections[user_id]:
            conn = self._connections[user_id][machine_id]

            # Cancel any pending requests
            for future in conn.pending_requests.values():
                if not future.done():
                    future.set_exception(ConnectionError("Connection closed"))

            # Notify stream listeners
            for stream_id, listeners in conn.active_streams.items():
                for queue in listeners:
                    try:
                        queue.put_nowait({"type": "stream_end", "id": stream_id, "reason": "disconnected"})
                    except Exception:
                        pass

            del self._connections[user_id][machine_id]
            if not self._connections[user_id]:
                del self._connections[user_id]

        del self._machine_to_user[machine_id]
        print(f"[Relay] Unregistered connection: machine={machine_id}")

    def get_connection(self, user_id: str, machine_id: str) -> Optional[RelayConnection]:
        """Get a specific connection."""
        return self._connections.get(user_id, {}).get(machine_id)

    def get_user_connections(self, user_id: str) -> Dict[str, RelayConnection]:
        """Get all connections for a user."""
        return self._connections.get(user_id, {})

    def get_all_connections(self) -> Dict[str, Dict[str, RelayConnection]]:
        """Get all connections (for admin/debug)."""
        return self._connections

    async def send_request(
        self,
        user_id: str,
        machine_id: str,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        timeout: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Send a request to a local server and wait for response.

        Returns the response body or raises an exception on error/timeout.
        """
        conn = self.get_connection(user_id, machine_id)
        if not conn:
            raise ConnectionError(f"No connection for machine {machine_id}")

        request_id = str(uuid.uuid4())
        request = {
            "type": "request",
            "id": request_id,
            "method": method,
            "path": path,
            "body": body or {},
            "headers": headers or {}
        }

        # Create future for response
        loop = asyncio.get_event_loop()
        future = loop.create_future()
        conn.pending_requests[request_id] = future

        try:
            # Send request
            await conn.websocket.send_json(request)
            print(f"[Relay] Sent request: {method} {path} (id={request_id[:8]})")

            # Wait for response with timeout
            response = await asyncio.wait_for(
                future,
                timeout=timeout or self._request_timeout
            )

            return response

        except asyncio.TimeoutError:
            raise TimeoutError(f"Request timed out after {timeout or self._request_timeout}s")
        finally:
            conn.pending_requests.pop(request_id, None)

    async def start_stream(
        self,
        user_id: str,
        machine_id: str,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None
    ) -> tuple[str, asyncio.Queue]:
        """
        Start a streaming request to a local server.

        Returns (request_id, queue) where queue receives stream events.
        """
        conn = self.get_connection(user_id, machine_id)
        if not conn:
            raise ConnectionError(f"No connection for machine {machine_id}")

        request_id = str(uuid.uuid4())
        queue = asyncio.Queue()

        # Register stream listener
        if request_id not in conn.active_streams:
            conn.active_streams[request_id] = set()
        conn.active_streams[request_id].add(queue)

        # Send stream request
        request = {
            "type": "stream_request",
            "id": request_id,
            "method": method,
            "path": path,
            "body": body or {}
        }

        await conn.websocket.send_json(request)
        print(f"[Relay] Started stream: {method} {path} (id={request_id[:8]})")

        return request_id, queue

    def add_stream_listener(self, user_id: str, machine_id: str, request_id: str) -> Optional[asyncio.Queue]:
        """Add a listener to an existing stream."""
        conn = self.get_connection(user_id, machine_id)
        if not conn or request_id not in conn.active_streams:
            return None

        queue = asyncio.Queue()
        conn.active_streams[request_id].add(queue)
        return queue

    def remove_stream_listener(self, user_id: str, machine_id: str, request_id: str, queue: asyncio.Queue) -> None:
        """Remove a listener from a stream."""
        conn = self.get_connection(user_id, machine_id)
        if conn and request_id in conn.active_streams:
            conn.active_streams[request_id].discard(queue)
            if not conn.active_streams[request_id]:
                del conn.active_streams[request_id]

    async def handle_message(self, machine_id: str, message: Dict[str, Any]) -> None:
        """
        Handle an incoming message from a local server.

        Message types:
        - response: Response to a request
        - stream: Stream event data
        - stream_end: End of stream
        - pong: Heartbeat response
        """
        user_id = self._machine_to_user.get(machine_id)
        if not user_id:
            print(f"[Relay] Message from unknown machine: {machine_id}")
            return

        conn = self.get_connection(user_id, machine_id)
        if not conn:
            return

        msg_type = message.get("type")
        msg_id = message.get("id")

        if msg_type == "response":
            # Complete the pending request future
            if msg_id in conn.pending_requests:
                future = conn.pending_requests[msg_id]
                if not future.done():
                    status = message.get("status", 200)
                    if status >= 400:
                        future.set_exception(
                            Exception(f"Request failed with status {status}: {message.get('body', {})}")
                        )
                    else:
                        future.set_result(message.get("body", {}))

        elif msg_type == "stream":
            # Forward to stream listeners
            if msg_id in conn.active_streams:
                for queue in conn.active_streams[msg_id]:
                    try:
                        await queue.put(message)
                    except Exception:
                        pass

        elif msg_type == "stream_end":
            # End stream and notify listeners
            if msg_id in conn.active_streams:
                for queue in conn.active_streams[msg_id]:
                    try:
                        await queue.put(message)
                    except Exception:
                        pass
                del conn.active_streams[msg_id]

        elif msg_type == "pong":
            conn.last_heartbeat = datetime.utcnow()

    async def send_ping(self, machine_id: str) -> None:
        """Send a ping to a machine."""
        user_id = self._machine_to_user.get(machine_id)
        if not user_id:
            return

        conn = self.get_connection(user_id, machine_id)
        if conn:
            try:
                await conn.websocket.send_json({"type": "ping"})
            except Exception:
                pass

    def to_dict(self, user_id: str) -> list:
        """Get connections as list of dicts for API response."""
        connections = self.get_user_connections(user_id)
        return [
            {
                "machine_id": machine_id,
                "machine_name": conn.machine_name,
                "connected_at": conn.connected_at.isoformat(),
                "last_heartbeat": conn.last_heartbeat.isoformat(),
                "capabilities": conn.capabilities,
                "active_streams": len(conn.active_streams),
                "pending_requests": len(conn.pending_requests)
            }
            for machine_id, conn in connections.items()
        ]


# Singleton instance
relay_manager = RelayManager()
