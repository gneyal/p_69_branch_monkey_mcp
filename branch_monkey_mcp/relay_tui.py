"""
Terminal UI for the Kompany Relay.

Shows a live dashboard with connection status, heartbeat, and system
statistics. Press L to view logs, Q to quit.
"""

import curses
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Callable, Dict, Any

# Reduce ESC key delay (default 1000ms is way too long)
os.environ.setdefault("ESCDELAY", "25")


class LogCapture:
    """Intercepts writes to a stream and stores them in a ring buffer."""

    def __init__(self, original, max_lines: int = 1000):
        self._original = original
        self._buffer: deque = deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self.encoding = getattr(original, "encoding", "utf-8")
        self.errors = getattr(original, "errors", "strict")

    def write(self, text: str) -> int:
        if text and text.strip():
            ts = datetime.now().strftime("%H:%M:%S")
            with self._lock:
                for line in text.rstrip("\n").split("\n"):
                    if line.strip():
                        self._buffer.append(f"{ts}  {line}")
        return len(text) if text else 0

    def flush(self):
        pass

    def fileno(self):
        return self._original.fileno()

    def isatty(self):
        return False

    def reconfigure(self, **kwargs):
        pass

    def get_lines(self, limit: int = 200) -> list:
        with self._lock:
            return list(self._buffer)[-limit:]

    @property
    def closed(self):
        return False


class RelayTUI:
    """Curses-based terminal UI for the Kompany Relay."""

    REFRESH_MS = 2000

    def __init__(self):
        self.state: Dict[str, Any] = {
            "version": "",
            "machine_name": "",
            "machine_id": "",
            "home_dir": "",
            "project": None,
            "project_path": None,
            "port": 18081,
            "dashboard_url": "",
            "cloud_url": "",
            "connection": "disconnected",
            "server_running": False,
            "last_heartbeat": None,
            "connected_at": None,
            "reconnect_count": 0,
            "requests_handled": 0,
            "auth_state": "idle",
            "auth_url": None,
            "auth_code": None,
        }
        self._stdout_capture = LogCapture(sys.stdout)
        self._stderr_capture = LogCapture(sys.stderr)
        self._view = "dashboard"
        self._running = True
        self._stop_callback: Optional[Callable] = None
        self._scroll_offset = 0
        self._original_stdout = sys.stdout
        self._original_stderr = sys.stderr

    def install_capture(self):
        """Redirect stdout/stderr to capture logs."""
        sys.stdout = self._stdout_capture
        sys.stderr = self._stderr_capture

    def restore_streams(self):
        """Restore original stdout/stderr."""
        sys.stdout = self._original_stdout
        sys.stderr = self._original_stderr

    def update(self, **kwargs):
        """Update state dict (thread-safe for simple dict updates)."""
        self.state.update(kwargs)

    def run(self, stop_callback: Optional[Callable] = None):
        """Run the TUI. Blocks until user quits."""
        self._stop_callback = stop_callback
        try:
            curses.wrapper(self._main_loop)
        except KeyboardInterrupt:
            pass
        finally:
            self.restore_streams()
            if self._stop_callback:
                self._stop_callback()

    def stop(self):
        self._running = False

    # ── curses main loop ─────────────────────────────────────────────

    def _main_loop(self, stdscr):
        curses.use_default_colors()
        curses.curs_set(0)
        # Short timeout so we poll keys every 100ms for responsive input
        stdscr.timeout(100)

        if curses.has_colors():
            curses.init_pair(1, curses.COLOR_GREEN, -1)
            curses.init_pair(2, curses.COLOR_RED, -1)
            curses.init_pair(3, curses.COLOR_YELLOW, -1)
            curses.init_pair(4, curses.COLOR_CYAN, -1)
            try:
                curses.init_pair(5, 8, -1)  # bright black (gray)
            except curses.error:
                curses.init_pair(5, curses.COLOR_WHITE, -1)

        last_draw = 0.0

        while self._running:
            # Redraw every REFRESH_MS
            now = time.monotonic()
            if now - last_draw >= self.REFRESH_MS / 1000.0:
                try:
                    stdscr.erase()
                    h, w = stdscr.getmaxyx()
                    if self._view == "dashboard":
                        self._draw_dashboard(stdscr, h, w)
                    else:
                        self._draw_logs(stdscr, h, w)
                    stdscr.refresh()
                except curses.error:
                    pass
                last_draw = time.monotonic()

            # getch blocks for up to 100ms (set by timeout above)
            key = stdscr.getch()
            if key != -1:
                self._handle_key(key)
                last_draw = 0.0  # Force redraw after key press

    def _handle_key(self, key):
        if key == ord("q") or key == ord("Q"):
            self._running = False
        elif key == ord("l") or key == ord("L"):
            self._view = "logs"
            self._scroll_offset = 0
        elif key == ord("b") or key == ord("B") or key == 27:
            self._view = "dashboard"
        elif key == curses.KEY_UP and self._view == "logs":
            self._scroll_offset += 1
        elif key == curses.KEY_DOWN and self._view == "logs":
            self._scroll_offset = max(0, self._scroll_offset - 1)

    # ── drawing helpers ──────────────────────────────────────────────

    def _put(self, stdscr, y, x, text, attr=0):
        h, w = stdscr.getmaxyx()
        if 0 <= y < h and 0 <= x < w:
            try:
                stdscr.addnstr(y, x, text, max(0, w - x - 1), attr)
            except curses.error:
                pass

    def _hline(self, stdscr, y, x, length):
        h, w = stdscr.getmaxyx()
        actual = min(length, w - x - 1)
        if 0 <= y < h and actual > 0:
            self._put(stdscr, y, x, "\u2500" * actual, self._dim())

    def _green(self):
        return curses.color_pair(1) if curses.has_colors() else 0

    def _red(self):
        return curses.color_pair(2) if curses.has_colors() else 0

    def _yellow(self):
        return curses.color_pair(3) if curses.has_colors() else 0

    def _cyan(self):
        return curses.color_pair(4) if curses.has_colors() else 0

    def _dim(self):
        return curses.color_pair(5) if curses.has_colors() else curses.A_DIM

    def _bold(self):
        return curses.A_BOLD

    # ── dashboard view ───────────────────────────────────────────────

    def _draw_dashboard(self, stdscr, h, w):
        s = self.state
        col = 2
        lbl_col = 4
        val_col = 20
        bar_w = min(50, w - 4)
        y = 1

        # Header
        self._put(stdscr, y, col, "Kompany Relay", self._bold())
        ver = f"v{s['version']}" if s["version"] else ""
        self._put(stdscr, y, col + 15, ver, self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 2

        # Auth screen (takes over dashboard while authenticating)
        if s["auth_state"] in ("authenticating", "waiting"):
            self._draw_auth(stdscr, y, col, bar_w)
            return

        # Machine info
        info = [
            ("Machine", s.get("machine_name", "\u2014")),
            ("Home", s.get("home_dir", "\u2014")),
        ]
        if s.get("project"):
            info.append(("Project", s["project"]))
        info.append(("Dashboard", s.get("dashboard_url", f"http://localhost:{s['port']}/")))

        for label, value in info:
            self._put(stdscr, y, lbl_col, label, self._dim())
            self._put(stdscr, y, val_col, str(value), self._bold())
            y += 1

        y += 1
        self._put(stdscr, y, lbl_col, "STATUS", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        # Cloud connection
        conn = s["connection"]
        if conn == "connected":
            dot_attr = self._green() | self._bold()
            label = "Connected"
        elif conn in ("connecting", "reconnecting"):
            dot_attr = self._yellow() | self._bold()
            label = conn.capitalize() + "..."
        else:
            dot_attr = self._red() | self._bold()
            label = "Disconnected"

        self._put(stdscr, y, lbl_col, "Cloud", self._dim())
        self._put(stdscr, y, val_col, "\u25cf", dot_attr)
        self._put(stdscr, y, val_col + 2, label)
        y += 1

        # Local server
        self._put(stdscr, y, lbl_col, "Local Server", self._dim())
        if s["server_running"]:
            self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold())
            self._put(stdscr, y, val_col + 2, f"Running :{s['port']}")
        else:
            self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, "Starting...")
        y += 1

        # Heartbeat
        self._put(stdscr, y, lbl_col, "Heartbeat", self._dim())
        hb = s.get("last_heartbeat")
        if hb:
            ago = int((datetime.now(timezone.utc) - hb).total_seconds())
            if ago < 60:
                self._put(stdscr, y, val_col, "\u25cf", self._green() | self._bold())
                self._put(stdscr, y, val_col + 2, f"OK  {ago}s ago")
            else:
                self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
                self._put(stdscr, y, val_col + 2, f"Stale  {ago}s ago")
        elif conn == "connected":
            self._put(stdscr, y, val_col, "\u25cf", self._yellow() | self._bold())
            self._put(stdscr, y, val_col + 2, "Waiting...")
        else:
            self._put(stdscr, y, val_col, "\u25cf", self._dim())
            self._put(stdscr, y, val_col + 2, "\u2014")
        y += 2

        # Stats
        self._put(stdscr, y, lbl_col, "STATS", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        self._put(stdscr, y, lbl_col, "Uptime", self._dim())
        self._put(stdscr, y, val_col, self._format_uptime())
        y += 1

        rc = s.get("reconnect_count", 0)
        self._put(stdscr, y, lbl_col, "Reconnects", self._dim())
        self._put(stdscr, y, val_col, str(rc), self._green() if rc == 0 else self._yellow())
        y += 1

        self._put(stdscr, y, lbl_col, "Requests", self._dim())
        self._put(stdscr, y, val_col, str(s.get("requests_handled", 0)))
        y += 2

        # Recent log lines
        self._put(stdscr, y, lbl_col, "RECENT", self._dim())
        y += 1
        self._hline(stdscr, y, col, bar_w)
        y += 1

        recent = self._stdout_capture.get_lines(3)
        for line in recent:
            if y >= h - 3:
                break
            self._put(stdscr, y, lbl_col, line[:bar_w], self._dim())
            y += 1

        # Footer
        footer_y = h - 2
        self._hline(stdscr, footer_y - 1, col, bar_w)
        self._put(stdscr, footer_y, lbl_col, "[L]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, lbl_col + 4, "Logs", self._dim())
        self._put(stdscr, footer_y, lbl_col + 12, "[Q]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, lbl_col + 16, "Quit", self._dim())

    def _draw_auth(self, stdscr, y, col, bar_w):
        s = self.state
        lbl_col = col + 2

        if s.get("auth_url"):
            self._put(stdscr, y, lbl_col, "Authorize this device:", self._bold())
            y += 2
            self._put(stdscr, y, lbl_col, "Visit:", self._dim())
            y += 1
            self._put(stdscr, y, lbl_col + 2, s.get("auth_url", ""), self._cyan() | self._bold())
            y += 2
            if s.get("auth_code"):
                self._put(stdscr, y, lbl_col, "Code:", self._dim())
                self._put(stdscr, y, lbl_col + 7, s["auth_code"], self._green() | self._bold())
                y += 2
            self._put(stdscr, y, lbl_col, "Waiting for approval...", self._yellow())
        else:
            self._put(stdscr, y, lbl_col, "Authenticating...", self._yellow())

        y += 2
        self._hline(stdscr, y, col, bar_w)
        y += 1
        self._put(stdscr, y, lbl_col + 2, "[Q]", self._cyan() | self._bold())
        self._put(stdscr, y, lbl_col + 6, "Quit", self._dim())

    # ── logs view ────────────────────────────────────────────────────

    def _draw_logs(self, stdscr, h, w):
        col = 2
        bar_w = min(w - 4, 100)

        # Header
        self._put(stdscr, 1, col, "Logs", self._bold())
        self._put(stdscr, 1, col + bar_w - 8, "[B] Back", self._cyan())
        self._hline(stdscr, 2, col, bar_w)

        # Log lines
        all_lines = self._stdout_capture.get_lines(500)
        visible_h = h - 6
        if visible_h <= 0:
            return

        # Clamp scroll
        max_scroll = max(0, len(all_lines) - visible_h)
        self._scroll_offset = min(self._scroll_offset, max_scroll)

        end = len(all_lines) - self._scroll_offset
        start = max(0, end - visible_h)
        end = max(start, end)
        visible = all_lines[start:end]

        for i, line in enumerate(visible):
            y = 3 + i
            if y >= h - 3:
                break
            attr = 0
            if "Error" in line or "error" in line:
                attr = self._red()
            elif "Warning" in line or "warning" in line:
                attr = self._yellow()
            elif "Connected" in line or "success" in line:
                attr = self._green()
            self._put(stdscr, y, col, line[: w - 4], attr)

        # Footer
        footer_y = h - 2
        self._hline(stdscr, footer_y - 1, col, bar_w)
        x = col + 2
        self._put(stdscr, footer_y, x, "[B]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Back", self._dim())
        x += 12
        self._put(stdscr, footer_y, x, "[\u2191\u2193]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 5, "Scroll", self._dim())
        x += 14
        self._put(stdscr, footer_y, x, "[Q]", self._cyan() | self._bold())
        self._put(stdscr, footer_y, x + 4, "Quit", self._dim())

        # Scroll indicator
        if len(all_lines) > visible_h:
            pct = int((end / max(len(all_lines), 1)) * 100)
            self._put(stdscr, footer_y, col + bar_w - 6, f"{pct:3d}%", self._dim())

    # ── helpers ──────────────────────────────────────────────────────

    def _format_uptime(self) -> str:
        connected_at = self.state.get("connected_at")
        if not connected_at or self.state["connection"] != "connected":
            return "\u2014"
        total = int((datetime.now(timezone.utc) - connected_at).total_seconds())
        if total < 60:
            return f"{total}s"
        if total < 3600:
            return f"{total // 60}m {total % 60}s"
        hours = total // 3600
        minutes = (total % 3600) // 60
        return f"{hours}h {minutes}m"
