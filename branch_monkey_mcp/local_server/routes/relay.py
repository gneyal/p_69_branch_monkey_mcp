"""
Relay status and heartbeat endpoints.
"""

from datetime import datetime

from fastapi import APIRouter
from pydantic import BaseModel

from ..config import get_relay_status, update_relay_status

router = APIRouter()


class RelayHeartbeat(BaseModel):
    """Relay heartbeat request."""
    machine_id: str
    machine_name: str
    cloud_url: str


@router.get("/status")
def relay_status():
    """Get current relay connection status."""
    status = get_relay_status()
    # Check if heartbeat is recent (within 60 seconds)
    if status["last_heartbeat"]:
        last_hb = datetime.fromisoformat(status["last_heartbeat"])
        age_seconds = (datetime.utcnow() - last_hb).total_seconds()
        status["connected"] = age_seconds < 60
        status["heartbeat_age_seconds"] = int(age_seconds)
    return status


@router.post("/heartbeat")
def relay_heartbeat(heartbeat: RelayHeartbeat):
    """Receive heartbeat from relay client to indicate it's connected."""
    update_relay_status(
        connected=True,
        machine_id=heartbeat.machine_id,
        machine_name=heartbeat.machine_name,
        cloud_url=heartbeat.cloud_url
    )
    return {"status": "ok", "received": datetime.utcnow().isoformat()}


@router.post("/disconnect")
def relay_disconnect():
    """Mark relay as disconnected."""
    update_relay_status(connected=False)
    return {"status": "ok", "disconnected": True}
