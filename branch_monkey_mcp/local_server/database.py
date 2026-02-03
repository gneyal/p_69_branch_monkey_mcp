"""
Database operations for the local server.

This module handles SQLite persistence for dev server state.
"""

import socket
import sqlite3
from pathlib import Path
from typing import Dict

# Database path for persisting dev server state
_DB_PATH = Path(__file__).parent.parent.parent / ".branch_monkey" / "data.db"


def _is_port_in_use(port: int) -> bool:
    """Check if a port is currently in use."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(('localhost', port)) == 0


def init_dev_servers_db():
    """Initialize the dev_servers table if it doesn't exist."""
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dev_servers (
            run_id TEXT PRIMARY KEY,
            task_id TEXT,
            task_number INTEGER,
            port INTEGER NOT NULL,
            worktree_path TEXT,
            started_at TEXT NOT NULL,
            pid INTEGER
        )
    """)
    conn.commit()
    conn.close()


def save_dev_server_to_db(run_id: str, info: dict):
    """Save dev server info to database."""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("""
        INSERT OR REPLACE INTO dev_servers
        (run_id, task_id, task_number, port, worktree_path, started_at, pid)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        run_id,
        info.get("task_id"),
        info.get("task_number"),
        info["port"],
        info.get("worktree_path"),
        info.get("started_at"),
        info.get("process").pid if info.get("process") else None
    ))
    conn.commit()
    conn.close()


def delete_dev_server_from_db(run_id: str):
    """Delete dev server from database."""
    conn = sqlite3.connect(_DB_PATH)
    conn.execute("DELETE FROM dev_servers WHERE run_id = ?", (run_id,))
    conn.commit()
    conn.close()


def load_dev_servers_from_db(running_dev_servers: Dict[str, dict]):
    """Load dev servers from database and validate they're still running.

    Args:
        running_dev_servers: Dict to populate with recovered dev servers
    """
    if not _DB_PATH.exists():
        return

    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    cursor = conn.execute("SELECT * FROM dev_servers")
    rows = cursor.fetchall()
    conn.close()

    for row in rows:
        run_id = row["run_id"]
        port = row["port"]
        pid = row["pid"]

        # Check if the port is still in use (server still running)
        if _is_port_in_use(port):
            running_dev_servers[run_id] = {
                "process": None,  # Can't restore process object
                "port": port,
                "task_id": row["task_id"],
                "task_number": row["task_number"],
                "run_id": run_id,
                "worktree_path": row["worktree_path"],
                "started_at": row["started_at"],
                "pid": pid
            }
            print(f"[DevServer] Restored dev server {run_id} on port {port}")
        else:
            # Server no longer running, clean up DB
            delete_dev_server_from_db(run_id)
            print(f"[DevServer] Cleaned up stale dev server {run_id}")
