# Task Workflow

How to create, manage, and execute tasks with AI agents.

## Task lifecycle

```
Create → Work → Log progress → Complete (with PR)
```

### 1. Create a task
```
kompany_task_create(
  title="Implement user authentication",
  description="Add OAuth login with Google and GitHub",
  status="todo",
  priority=1
)
```

Status options: `todo`, `in_progress`, `done`, `blocked`
Priority: 0 (normal), 1 (high), 2 (urgent)

### 2. Start working on a task
```
kompany_task_work(task_id=1, workflow="execute")
```

**Workflow types:**
- `ask` — Quick question or research. Answer directly, no code changes.
- `plan` — Design and architecture. Create a plan, get approval before implementing.
- `execute` — Full implementation. Creates a worktree, writes code, creates a PR.

### 3. Log progress
```
kompany_task_log(task_id=1, content="Implemented OAuth flow, testing now")
```

### 4. Complete with PR
```
kompany_task_complete(
  task_id=1,
  summary="Added Google and GitHub OAuth login with session management",
  worktree_path="/path/to/worktree",
  files_changed="src/auth.ts, src/routes/login.ts"
)
```

This creates a GitHub PR and marks the task as done.

## Task search and filtering
```
kompany_task_search(query="authentication", status="todo")
kompany_task_list()  # all tasks for focused project
```

## Context management

Contexts are reusable knowledge snippets attached to tasks.

### Create a context
```
kompany_context_create(
  name="Auth Architecture",
  content="We use JWT tokens stored in httpOnly cookies...",
  context_type="code"
)
```

Context types: `general`, `code`, `docs`, `spec`, `requirement`

### Link context to a task
```
kompany_task_link_context(task_id="<uuid>", context_id="<uuid>")
```

### Find relevant contexts
```
kompany_task_similar_contexts(task_id="<uuid>")  # AI-powered suggestions
kompany_context_search(query="auth")              # keyword search
kompany_context_recent(limit=5)                   # recently used
```

## Versions
Group tasks by release version:
```
kompany_version_create(key="v1.0", label="Version 1.0", description="MVP launch")
kompany_task_create(title="...", version="v1.0")
```
