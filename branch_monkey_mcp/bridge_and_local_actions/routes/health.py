"""
Health and status endpoints for the local server.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse

from ..config import get_default_working_dir
from ..agent_manager import agent_manager

router = APIRouter()


@router.get("/health")
def health_check():
    """Health check endpoint."""
    return {"status": "ok", "service": "branch-monkey-relay"}


def _get_dashboard_response():
    """Return the dashboard HTML file response."""
    dashboard_path = Path(__file__).parent.parent.parent / "static" / "dashboard.html"
    return FileResponse(dashboard_path, media_type="text/html")


@router.get("/")
def serve_root():
    """Serve the dashboard at root."""
    return _get_dashboard_response()


@router.get("/dashboard")
def serve_dashboard():
    """Serve the dashboard HTML."""
    return _get_dashboard_response()


@router.get("/api/status")
def api_status():
    """Status endpoint for frontend compatibility."""
    return {
        "status": "ok",
        "service": "branch-monkey-relay",
        "agents": len(agent_manager._agents),
        "mode": "local",
        "working_directory": get_default_working_dir()
    }
