# Canvas Workflow

Step-by-step process for building a business operations canvas.

## Pre-flight
1. `kompany_status` — Verify connection is active
2. `kompany_project_list` — Find or confirm the target project
3. `kompany_project_focus(project_id)` — Lock scope to that project

## Build sequence

### Step 1: Create domains
Domains are visual groups representing departments or areas of the business.

```
kompany_domain_create(
  name="Marketing",
  description="Customer acquisition and brand awareness",
  color="#8b5cf6",
  position_x=0, position_y=0,
  width=360, height=430
)
```

**CRITICAL:** Store every returned ID. You need domain positions to place systems, and system IDs to create connections.

### Step 2: Create systems inside domains
Systems are individual business processes that live inside domains.

Position systems relative to their parent domain:
- First system: x = domain_x + 40, y = domain_y + 70
- Second system: x = domain_x + 40, y = domain_y + 70 + 160 + 40 = domain_y + 270

```
kompany_machine_create(
  name="Content Marketing",
  description="Blog posts, social media, SEO",
  machine_type="generator",
  metric_unit="leads",
  leading_metric_name="posts published",
  position_x=40, position_y=70
)
```

### Step 3: Create connections
Connect systems to show value flow between processes.

```
kompany_connection_create(
  source_machine_id="<id-from-step-2>",
  target_machine_id="<id-from-step-2>",
  label="qualified leads"
)
```

### Step 4: Add metrics
If you didn't set metric_unit/leading_metric_name during system creation, add metrics manually:

```
kompany_metric_add(
  machine_id="<id>",
  metric_name="leads",
  value=0,
  target=100,
  period="weekly"
)
```

### Step 5: Add notes
Notes provide context, goals, or documentation on the canvas.

```
kompany_note_create(
  content="Q1 Goal: 500 MRR customers\nFocus: organic acquisition",
  color="#ddd6fe",
  position_x=800, position_y=0,
  width=220, height=180
)
```

## Key rules
- Always create domains FIRST, then systems, then connections
- Store ALL returned IDs — you need them for connections and metrics
- Position systems inside their parent domain boundaries
- Use the layout constants from layout-positioning.md for consistent spacing
