"""
Machine metrics management tools.
"""

from .. import state
from ..api_client import api_get, api_post, api_put, api_delete
from ..mcp_app import mcp


@mcp.tool()
def monkey_metric_list(machine_id: str) -> str:
    """List all metrics for a machine.

    Args:
        machine_id: The UUID of the machine
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_get(f"/api/machines/{machine_id}/metrics")
        metrics = result.get("metrics", [])

        if not metrics:
            return f"No metrics found for machine `{machine_id[:8]}...`"

        output = f"# Metrics for machine `{machine_id[:8]}...`\n\n"
        for m in metrics:
            target_str = f" / target: {m.get('target')}" if m.get("target") else ""
            period_str = f" ({m.get('period', 'weekly')})"
            output += f"- **{m.get('metric_name')}**: {m.get('value')}{target_str}{period_str} (ID: `{m.get('id')}`)\n"

        return output
    except Exception as e:
        return f"Error fetching metrics: {str(e)}"


@mcp.tool()
def monkey_metric_add(
    machine_id: str,
    metric_name: str,
    value: float = 0,
    target: float = None,
    period: str = "weekly",
    label: str = None
) -> str:
    """Add a new metric to a machine.

    Args:
        machine_id: The UUID of the machine
        metric_name: Name of the metric (e.g. "leads", "revenue", "calls made")
        value: Current value (default: 0)
        target: Target value (optional)
        period: Metric period - weekly, monthly, daily (default: weekly)
        label: Optional label for the metric
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        payload = {
            "metric_name": metric_name,
            "value": value,
            "period": period
        }
        if target is not None:
            payload["target"] = target
        if label is not None:
            payload["label"] = label

        result = api_post(f"/api/machines/{machine_id}/metrics", payload)
        metric = result.get("metric", result)
        return f"✅ Added metric: {metric_name} = {value} (ID: {metric.get('id')}) to machine `{machine_id[:8]}...`"
    except Exception as e:
        return f"Error adding metric: {str(e)}"


@mcp.tool()
def monkey_metric_update(
    machine_id: str,
    metric_name: str,
    value: float = None,
    target: float = None,
    period: str = None,
    label: str = None
) -> str:
    """Update a metric on a machine by metric name.

    Args:
        machine_id: The UUID of the machine
        metric_name: Name of the metric to update
        value: New value (optional)
        target: New target value (optional)
        period: New period - weekly, monthly, daily (optional)
        label: New label (optional)
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        payload = {"metric_name": metric_name}
        if value is not None:
            payload["value"] = value
        if target is not None:
            payload["target"] = target
        if period is not None:
            payload["period"] = period
        if label is not None:
            payload["label"] = label

        result = api_put(f"/api/machines/{machine_id}/metrics", payload)
        metric = result.get("metric", result)
        return f"✅ Updated metric: {metric_name} on machine `{machine_id[:8]}...`"
    except Exception as e:
        return f"Error updating metric: {str(e)}"


@mcp.tool()
def monkey_metric_delete(
    machine_id: str,
    metric_name: str
) -> str:
    """Delete a metric from a machine by metric name.

    Args:
        machine_id: The UUID of the machine
        metric_name: Name of the metric to delete
    """
    if not state.CURRENT_PROJECT_ID:
        return "⚠️ No project focused. Use `monkey_project_focus <project_id>` first."

    try:
        result = api_delete(f"/api/machines/{machine_id}/metrics?metric_name={metric_name}")
        count = result.get("deleted_count", 1)
        return f"✅ Deleted metric: {metric_name} from machine `{machine_id[:8]}...` ({count} entries removed)"
    except Exception as e:
        return f"Error deleting metric: {str(e)}"
