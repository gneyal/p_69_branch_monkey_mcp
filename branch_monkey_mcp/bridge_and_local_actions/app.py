"""
FastAPI application setup for the local server.

This module creates the FastAPI app, configures middleware, and sets up routes.
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .routes import main_router

# Create the FastAPI app
app = FastAPI(
    title="Branch Monkey Local Agent Server",
    description="Local server for running Claude Code agents",
    version="0.2.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include main router (all routes)
app.include_router(main_router)

# Include project discovery router
from ..project_discovery import router as project_discovery_router
app.include_router(project_discovery_router)


def run_server(port: int = 18081, host: str = "127.0.0.1"):
    """Run the FastAPI server."""
    import uvicorn
    print(f"\n[Server] Starting local agent server on http://{host}:{port}")
    uvicorn.run(app, host=host, port=port, log_level="warning")
