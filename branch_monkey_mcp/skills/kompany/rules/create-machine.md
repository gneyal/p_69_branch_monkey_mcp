# How to Create a Machine (Full Workflow)

A machine is an automated business process on the canvas. Creating one properly requires: the machine itself, an agent to run it, a cron schedule, and placement in a domain.

## Step-by-step

### 1. Pick the domain
```
kompany_domain_list()
```
Identify which domain this machine belongs to (e.g. "Create Demand", "Operations", "Meta"). Note the domain's `position_x`, `position_y`, `width`, `height`.

### 2. Create the agent
Every machine needs an agent — the AI persona that does the work.
```
kompany_agent_create(
  name="Name — Role",            # e.g. "Dex — Decision Executor"
  description="What it does",
  system_prompt="...",            # Define personality, responsibilities, tools to use
  color="#8b5cf6"                 # Match domain color or pick unique
)
```
Save the returned `agent_id`.

Agent naming convention: short name + role descriptor (e.g. "Cara — Content Creator", "Bree — Morning Briefing", "Dex — Decision Executor").

### 3. Create the machine
```
kompany_machine_create(
  name="Machine Name",
  description="Short description",
  goal="What success looks like",
  machine_type="processor",       # generator|processor|funnel|monitor|router|aggregator|syncer|nurture
  metric_unit="output metric",    # e.g. "decisions executed", "posts published"
  leading_metric_name="input metric",  # e.g. "approved decisions", "content drafts"
  position_x=domain_x + 30,      # inside domain bounding box
  position_y=domain_y + 60 + (N * 120)  # N = number of existing machines in domain
)
```
Save the returned `machine_id`.

### 4. Assign the agent to the machine
```
kompany_machine_update(
  machine_id="...",
  agent_id="..."
)
```

### 5. Create the cron schedule
```
kompany_cron_create(
  name="Machine Name",
  schedule="*/15 * * * *",        # cron expression
  agent_id="...",
  task_prompt="What the agent should do each run"
)
```

Common schedules:
| Expression | Meaning |
|-----------|---------|
| `*/15 * * * *` | Every 15 minutes |
| `0 * * * *` | Every hour |
| `0 6 * * *` | Daily at 6 AM |
| `0 6 * * 1-5` | Weekdays at 6 AM |
| `0 */4 * * *` | Every 4 hours |

### 6. Position inside the domain
Machines belong to domains by **position on the canvas** — the machine must sit inside the domain's bounding box. There's no `domain_id` column.

Formula:
- `position_x` = `domain.position_x + 30` (left padding)
- `position_y` = `domain.position_y + 60 + (index * 120)` (top padding + stacking)

If the machine was created at (0,0), move it:
```
kompany_machine_update(
  machine_id="...",
  position_x=domain_x + 30,
  position_y=domain_y + 60 + (N * 120)
)
```

## Checklist
- [ ] Domain identified
- [ ] Agent created with system prompt
- [ ] Machine created with metrics
- [ ] Agent assigned to machine
- [ ] Cron schedule created
- [ ] Machine positioned inside domain bounding box

## Example: Decision Executor in Meta domain

```
# 1. Domain: Meta at (1632, 3360), 480x816
kompany_domain_list()

# 2. Agent
kompany_agent_create(
  name="Dex — Decision Executor",
  description="Picks up approved decisions and triggers execution",
  color="#8b5cf6",
  system_prompt="You are Dex, the Decision Executor..."
)
# → agent_id: 4d35adc9-...

# 3. Machine
kompany_machine_create(
  name="Decision Executor",
  description="Picks up approved decisions every 15 minutes...",
  goal="Execute all approved decisions within 15 minutes",
  machine_type="processor",
  metric_unit="decisions executed",
  leading_metric_name="approved decisions",
  position_x=1662,
  position_y=3540
)
# → machine_id: bf12e208-...

# 4. Assign agent
kompany_machine_update(machine_id="bf12e208-...", agent_id="4d35adc9-...")

# 5. Cron
kompany_cron_create(
  name="Decision Executor",
  schedule="*/15 * * * *",
  agent_id="4d35adc9-...",
  task_prompt="Check for approved decisions and execute them"
)
```
