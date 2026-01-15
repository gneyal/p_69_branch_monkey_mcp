# Branch Monkey MCP Server

MCP (Model Context Protocol) server for [Branch Monkey](https://p-63-branch-monkey.pages.dev) - connecting Claude Code to your task management and team collaboration platform.

## Quick Start

Add this to your project's `.mcp.json`:

```json
{
  "mcpServers": {
    "branch-monkey-cloud": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/gneyal/p_69_branch_monkey_mcp.git", "branch-monkey-mcp"],
      "env": {
        "BRANCH_MONKEY_API_URL": "https://p-63-branch-monkey.pages.dev"
      }
    }
  }
}
```

Restart Claude Code. On first use, a browser will open for you to log in and approve the device.

## Features

- **No API key needed** - Authenticates via browser approval
- **Automatic token storage** - Saved in `~/.branch-monkey/token.json`
- **Task management** - Create, update, track tasks
- **Team collaboration** - Share tasks with your team
- **Prompt tracking** - Associate prompts with tasks

## Available Tools

### Status & Auth
- `monkey_status` - Get connection status
- `monkey_login` - Force re-authentication (use if having auth issues)
- `monkey_logout` - Clear auth token

### Projects & Organizations
- `monkey_project_list` - List all projects in your organization
- `monkey_project_focus` - Set focus to a specific project
- `monkey_project_clear` - Clear project focus
- `monkey_org_list` - List organizations you have access to

### Tasks
- `monkey_task_list` - List all tasks
- `monkey_task_create` - Create a new task
- `monkey_task_update` - Update a task
- `monkey_task_delete` - Delete a task
- `monkey_task_work` - Start working on a task
- `monkey_task_log` - Log progress on a task
- `monkey_task_complete` - Mark task as complete
- `monkey_task_search` - Search tasks
- `monkey_get_recent_tasks` - Get recently worked tasks
- `monkey_auto_resume` - Auto-detect related tasks

### Versions
- `monkey_version_list` - List versions
- `monkey_version_create` - Create a version

### Team
- `monkey_team_list` - List team members
- `monkey_team_add` - Add team member

### Machines
- `monkey_machine_list` - List machines
- `monkey_machine_create` - Create a machine

## Local Server Identification

When you run the relay client (`branch-monkey-relay`), your machine connects to Branch Monkey Cloud and becomes available for executing tasks.

### How Machine Identification Works

Each local machine is identified by a unique **machine ID** composed of:
- **Hostname** (e.g., `my-macbook-pro`)
- **Process ID** (e.g., `12345`)

Combined format: `my-macbook-pro-12345`

This ensures that:
- Each machine has a globally unique identifier
- Multiple relay instances on the same machine get different IDs
- The system can route requests to the correct machine

### Multiple Computers as "Local"

If you have multiple computers signed in:

1. **Each appears separately** - The cloud UI shows a list of all connected machines with their names and status
2. **You choose which to use** - When clicking "Local", you select from your connected machines
3. **Requests go to specific machines** - Each request is routed to your chosen machine via its unique channel
4. **Status tracking** - Heartbeats every 25 seconds keep connection status current

### What You See in the UI

For each connected machine, the system tracks:
- Machine name (hostname)
- Machine ID (hostname-pid)
- Connection time
- Last heartbeat (online status)
- Active capabilities

### Connection Behavior

- **Same machine, same ID reconnects**: The new connection replaces the old one (any pending requests are cancelled)
- **Same machine, different process**: Creates a new entry (useful for running multiple instances)
- **Machine goes offline**: Marked as offline when heartbeats stop

### Example Scenario

You have two computers:
- Work laptop (`work-laptop-5678`) - connected from office
- Home desktop (`home-pc-9012`) - connected from home

Both appear in the cloud UI. When you click "Local" to run a task, you choose which machine should execute it. The request is sent specifically to that machine's channel.

## Troubleshooting

### Authentication Issues

If you're having trouble connecting:

1. **Use `monkey_login`** - Forces re-authentication via browser
2. **Check the token file** - Stored at `~/.branch-monkey/token.json`
3. **Clear and retry** - Use `monkey_logout`, then restart Claude Code
4. **Check network** - Ensure you can access https://p-63-branch-monkey.pages.dev

### First-Time Setup

On first use:
1. A browser window opens automatically
2. Log in to Branch Monkey (or create an account)
3. Select the organization you want to connect to
4. Approve the device when prompted
5. Return to Claude Code - you're connected!

After connecting, use `monkey_project_list` to see available projects and `monkey_project_focus <id>` to select one.

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/) package manager

## License

MIT
