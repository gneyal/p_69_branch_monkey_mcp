---
name: kompany
description: Build business operations canvases with Kompany. Use when user asks to map business processes, create company canvases, or mentions /kompany.
---

# Kompany — Business Operations Canvas Builder

Build visual business canvases using `kompany_*` MCP tools. The canvas renders live at the Kompany dashboard.

## Concepts
- **Domains** — Departments/areas (visual groups on the canvas)
- **Systems** — Business processes (live inside domains)
- **Connections** — Value flows between systems (edges)
- **Metrics** — KPIs tracked per system
- **Notes** — Context/goals displayed on the canvas

## Build workflow

### 1. Pre-flight
```
kompany_status()                        # verify connection
kompany_project_list()                  # find project
kompany_project_focus(project_id)       # lock scope
```

### 2. Create domains (departments)
```
kompany_domain_create(name, description, color, position_x, position_y, width, height)
```
**CRITICAL: Store every returned ID.** You need them for system placement and connections.

### 3. Create systems inside domains
```
kompany_machine_create(name, description, machine_type, metric_unit, leading_metric_name, position_x, position_y)
```
Position relative to parent domain: x = domain_x + 40, y = domain_y + 70 + idx * 200

Types: generator, processor, funnel, monitor, router, aggregator, syncer, nurture

### 4. Connect systems
```
kompany_connection_create(source_machine_id, target_machine_id, label)
```

### 5. Add notes
```
kompany_note_create(content, color, position_x, position_y, width=220, height=180)
```

## Layout constants
| Constant | Value | Description |
|----------|-------|-------------|
| M_W | 240 | System width |
| M_H | 160 | System height |
| PAD | 40 | Domain side/bottom padding |
| PAD_TOP | 70 | Domain top padding |
| GAP | 50 | Gap between domains |
| M_GAP | 40 | Gap between systems |

**Domain sizing:**
- Height for N systems: `160*N + 70 + 40 + 40*(N-1)` → 1=270, 2=470, 3=670
- Width for N columns: `240*N + 80 + 40*(N-1)` → 1=320, 2=600

**System position inside domain:**
- x = domain_x + 40
- y = domain_y + 70 + idx * (160 + 40)

**Domain layout:** Place left-to-right, each domain_x = prev_domain_x + prev_width + 50

## Colors
| Use | Domain color | Note bg |
|-----|-------------|---------|
| Marketing | #8b5cf6 | #ddd6fe |
| Sales | #3b82f6 | #bfdbfe |
| Delivery | #22c55e | #bbf7d0 |
| Revenue | #f59e0b | #fed7aa |
| Retention | #ef4444 | #fecdd3 |

## Metrics
Set `metric_unit` (output: "leads", "revenue") and `leading_metric_name` (input: "calls made", "posts published") on system creation to auto-seed metrics. Or add manually:
```
kompany_metric_add(machine_id, metric_name, value=0, target=100, period="weekly")
```
Periods: weekly (default), monthly, daily

## Templates
Match the user's business type to a template structure:

- **SaaS**: Awareness → Acquisition → Activation → Revenue → Retention (5 domains, 10 systems)
- **E-Commerce**: Marketing → Storefront → Fulfillment → Support (4 domains, 8 systems)
- **Agency**: Biz Dev → Sales → Delivery → Account Mgmt (4 domains, 8 systems)
- **Content Creator**: Production → Distribution → Monetization → Community
- **Product Company**: R&D → Production → Sales → After-Sales
- **Restaurant**: Marketing → Front of House → Kitchen → Operations
- **Real Estate**: Lead Gen → Listings → Sales → Closing

If no template matches, ask the user about their departments, processes, and value flows.

## Task management
```
kompany_task_create(title, description, status="todo", priority=0)
kompany_task_work(task_id, workflow="execute")    # execute|plan|ask
kompany_task_log(task_id, content)
kompany_task_complete(task_id, summary, worktree_path, files_changed)
```

## Agent management
Agents are custom AI personas with system prompts that can be assigned to tasks.
```
kompany_agent_list()                              # list all agents
kompany_agent_create(name, system_prompt, description?, color?, icon?)  # create agent
kompany_agent_get(agent_id)                       # get agent details
kompany_agent_update(agent_id, name?, system_prompt?, ...)  # update agent
kompany_agent_delete(agent_id)                    # delete agent (not defaults)
kompany_apply_agent(agent_slug, instructions, context?)  # run agent
```

## REST API
Systems expose an HTTP API for pushing metrics externally (no MCP needed). Authenticate with a Bearer token (API key from Settings or the system's API modal).
```
GET    /api/machines/{id}/metrics              # read all metrics
POST   /api/machines/{id}/metrics              # create metric
PUT    /api/machines/{id}/metrics              # update metric
DELETE /api/machines/{id}/metrics?metric_name=X # delete metric
```
See rules/rest-api.md for full reference.

For detailed reference on any topic, read the supplementary files in this skill's directory:
- rules/create-machine.md — **Full machine creation workflow** (agent + cron + domain placement)
- rules/canvas-workflow.md, rules/layout-positioning.md, rules/templates.md
- rules/metrics-goals.md, rules/task-workflow.md, rules/rest-api.md
