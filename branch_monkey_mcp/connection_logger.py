"""
Connection Event Logger for Branch Monkey Relay.

Provides two ways to find disconnections:

1. **JSON-lines log file** (~/.kompany/connection_events.log)
   - Persistent, survives restarts
   - Queryable with jq, grep, or any JSON tool
   - Auto-rotates at 5MB

2. **In-memory ring buffer** (exposed via /api/relay/diagnostics)
   - Last 200 events, instant access
   - Includes computed stats (uptime, reconnect count, heartbeat success rate)
   - No disk I/O for reads

Usage:
    from branch_monkey_mcp.connection_logger import connection_logger

    connection_logger.log("connected", detail="Initial connection to Supabase")
    connection_logger.log("heartbeat_failed", detail="Timeout after 25s", error="ReadTimeout")
    connection_logger.log("disconnected", reason="heartbeat_timeout", detail="No heartbeat for 65s")

    # Get diagnostics dict for API response
    diagnostics = connection_logger.get_diagnostics()
"""

import json
import os
import threading
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Event types for structured filtering
EVENT_TYPES = {
    # Connection lifecycle
    "connecting",
    "connected",
    "disconnected",
    "reconnecting",
    "reconnected",
    "shutdown",

    # Heartbeat
    "heartbeat_ok",
    "heartbeat_failed",

    # Errors
    "connection_failed",
    "stream_error",
    "auth_error",
    "channel_error",

    # Health
    "health_check_triggered_reconnect",
}

LOG_DIR = Path.home() / ".kompany"
LOG_FILE = LOG_DIR / "connection_events.log"
MAX_LOG_SIZE = 5 * 1024 * 1024  # 5MB
RING_BUFFER_SIZE = 200


class ConnectionLogger:
    """Thread-safe connection event logger with file + memory backends."""

    def __init__(self):
        self._buffer: deque = deque(maxlen=RING_BUFFER_SIZE)
        self._lock = threading.Lock()
        self._stats = {
            "total_connects": 0,
            "total_disconnects": 0,
            "total_reconnects": 0,
            "total_heartbeat_ok": 0,
            "total_heartbeat_failed": 0,
            "last_connected_at": None,
            "last_disconnected_at": None,
            "session_start": datetime.now(timezone.utc).isoformat(),
        }
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        event: str,
        detail: Optional[str] = None,
        reason: Optional[str] = None,
        error: Optional[str] = None,
        attempt: Optional[int] = None,
        delay: Optional[float] = None,
    ):
        """
        Log a connection event.

        Args:
            event: Event type (e.g. "connected", "heartbeat_failed")
            detail: Human-readable description
            reason: Machine-readable reason code
            error: Error message if applicable
            attempt: Reconnection attempt number
            delay: Delay before next retry (seconds)
        """
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
        }
        if detail:
            entry["detail"] = detail
        if reason:
            entry["reason"] = reason
        if error:
            entry["error"] = error
        if attempt is not None:
            entry["attempt"] = attempt
        if delay is not None:
            entry["delay"] = round(delay, 1)

        # Update stats
        self._update_stats(event, entry)

        with self._lock:
            # Memory buffer
            self._buffer.append(entry)

            # File (best-effort, never block relay)
            try:
                self._rotate_if_needed()
                with open(LOG_FILE, "a") as f:
                    f.write(json.dumps(entry) + "\n")
            except Exception:
                pass  # Don't let logging break the relay

    def _update_stats(self, event: str, entry: dict):
        """Update running statistics based on event type."""
        if event == "connected":
            self._stats["total_connects"] += 1
            self._stats["last_connected_at"] = entry["ts"]
        elif event == "disconnected":
            self._stats["total_disconnects"] += 1
            self._stats["last_disconnected_at"] = entry["ts"]
        elif event == "reconnected":
            self._stats["total_reconnects"] += 1
            self._stats["last_connected_at"] = entry["ts"]
        elif event == "heartbeat_ok":
            self._stats["total_heartbeat_ok"] += 1
        elif event == "heartbeat_failed":
            self._stats["total_heartbeat_failed"] += 1

    def _rotate_if_needed(self):
        """Rotate log file if it exceeds max size."""
        try:
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > MAX_LOG_SIZE:
                rotated = LOG_FILE.with_suffix(".log.1")
                if rotated.exists():
                    rotated.unlink()
                LOG_FILE.rename(rotated)
        except Exception:
            pass

    def get_recent_events(self, limit: int = 50) -> list:
        """Get most recent events from the ring buffer."""
        with self._lock:
            events = list(self._buffer)
        return events[-limit:]

    def get_diagnostics(self) -> dict:
        """
        Build a diagnostics snapshot for the /api/relay/diagnostics endpoint.

        Returns dict with:
        - connection: current state + uptime
        - stats: lifetime counters
        - heartbeat: success rate + last failure
        - recent_events: last 50 events
        """
        events = self.get_recent_events(50)

        # Compute heartbeat success rate
        hb_total = self._stats["total_heartbeat_ok"] + self._stats["total_heartbeat_failed"]
        hb_rate = (
            round(self._stats["total_heartbeat_ok"] / hb_total * 100, 1)
            if hb_total > 0
            else None
        )

        # Find last failure event
        last_failure = None
        for e in reversed(events):
            if e["event"] in ("disconnected", "connection_failed", "heartbeat_failed", "stream_error"):
                last_failure = e
                break

        # Compute uptime since last connect
        uptime_seconds = None
        if self._stats["last_connected_at"]:
            try:
                connected_at = datetime.fromisoformat(self._stats["last_connected_at"])
                now = datetime.now(timezone.utc)
                # If last disconnect is after last connect, we're currently down
                if self._stats["last_disconnected_at"]:
                    disconnected_at = datetime.fromisoformat(self._stats["last_disconnected_at"])
                    if disconnected_at > connected_at:
                        uptime_seconds = 0
                    else:
                        uptime_seconds = int((now - connected_at).total_seconds())
                else:
                    uptime_seconds = int((now - connected_at).total_seconds())
            except Exception:
                pass

        return {
            "connection": {
                "last_connected_at": self._stats["last_connected_at"],
                "last_disconnected_at": self._stats["last_disconnected_at"],
                "uptime_seconds": uptime_seconds,
                "session_start": self._stats["session_start"],
            },
            "stats": {
                "total_connects": self._stats["total_connects"],
                "total_disconnects": self._stats["total_disconnects"],
                "total_reconnects": self._stats["total_reconnects"],
            },
            "heartbeat": {
                "total_ok": self._stats["total_heartbeat_ok"],
                "total_failed": self._stats["total_heartbeat_failed"],
                "success_rate_pct": hb_rate,
            },
            "last_failure": last_failure,
            "recent_events": events,
            "log_file": str(LOG_FILE),
        }


# Singleton instance
connection_logger = ConnectionLogger()
