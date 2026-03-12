# REST API

Push metrics to systems via HTTP. Use this when your agent doesn't have MCP access, or when integrating external tools (Zapier, n8n, cron jobs, CI pipelines).

## Authentication

All endpoints require a Bearer token (API key). Generate one from the system's API modal (three-dots menu → API) or from Settings.

```
Authorization: Bearer nf_live_xxxxx
```

Keys are project-scoped — one key works for all systems in that project.

## Base URL

```
https://kompany.dev/api/machines/{machine_id}/metrics
```

Replace `{machine_id}` with the system's UUID.

## Endpoints

### Read all metrics
```
GET /api/machines/{machine_id}/metrics
Authorization: Bearer {api_key}
```

Response:
```json
{
  "success": true,
  "metrics": [
    {
      "id": "uuid",
      "metric_name": "leads",
      "value": 42,
      "target": 50,
      "label": "Leads generated",
      "period": "weekly",
      "threshold_red": null,
      "threshold_yellow": null,
      "updated_at": "2026-02-09T..."
    }
  ]
}
```

### Create / push a metric
```
POST /api/machines/{machine_id}/metrics
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "metric_name": "leads",
  "value": 42,
  "target": 50,
  "label": "Leads generated",
  "period": "weekly",
  "threshold_red": 80,
  "threshold_yellow": 60
}
```

Required: `metric_name`, `value`
Optional: `target`, `label`, `period` (weekly|monthly|daily), `threshold_red`, `threshold_yellow`, `trend`, `trend_value`

If a metric with that name already exists, POST creates a new entry (history). Use PUT to update in place.

### Update a metric
```
PUT /api/machines/{machine_id}/metrics
Authorization: Bearer {api_key}
Content-Type: application/json

{
  "metric_name": "leads",
  "value": 45,
  "target": 60
}
```

Identify by `metric_name` or `metric_id`. Only provided fields are updated.

Updatable fields: `value`, `target`, `label`, `period`, `threshold_red`, `threshold_yellow`, `trend`, `trend_value`

### Delete a metric
```
DELETE /api/machines/{machine_id}/metrics?metric_name=leads
Authorization: Bearer {api_key}
```

Use `metric_name` or `metric_id` as query parameter.

## Alerts

Pushing a metric value above `threshold_red` or `threshold_yellow` automatically creates an alert on the system. When the value drops back below thresholds, the alert auto-resolves.

## Typical agent workflow

1. **Read** existing metrics to understand current state
2. **Update** the value (and optionally the target) of an existing metric
3. **Create** a new metric only if none exists for what you're tracking

```
# Step 1: Read
GET /api/machines/{id}/metrics

# Step 2: Update existing
PUT /api/machines/{id}/metrics
{"metric_name": "leads", "value": 47}

# Or create new
POST /api/machines/{id}/metrics
{"metric_name": "conversion_rate", "value": 3.2, "target": 5, "period": "weekly"}
```

## Use with AI agents

Any AI agent (Claude, GPT, etc.) can push metrics. Give it these instructions:

> You have access to the kompany REST API. Use it to track progress on the "{machine_name}" system.
> Read the current metrics first, then push updated values based on your work.
> Auth: Bearer {api_key}
> Endpoint: https://kompany.dev/api/machines/{machine_id}/metrics
