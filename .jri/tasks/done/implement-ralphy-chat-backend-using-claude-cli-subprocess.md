---
title: Implement Ralphy chat backend using claude CLI subprocess
priority: 0
assignee: ralph
depends_on:
- implement-project-creation-with-github-repo-initialization
created: '2026-03-21'
acceptance_criteria:
- 'POST /api/projects/{name}/chat with {"message": "Hello"} returns a streaming SSE
  response (Content-Type: text/event-stream).'
- The SSE stream contains events with type='text' that include Ralphy's response text.
- The SSE stream ends with a type='done' event.
- A second POST to the same project continues the same claude session (conversation
  history is preserved — Ralphy remembers what was said before).
- The claude subprocess runs with cwd set to the project directory.
- The claude subprocess has --dangerously-skip-permissions so it can execute bd commands
  without prompts.
- The claude subprocess has --allowedTools restricting it to Bash(bd:*), Bash(git:*),
  Read, Write, Edit, Glob, Grep.
- Sending a message to a project that doesn't belong to the user returns
- 9. The Ralphy system prompt is stored as a constant in app/prompts/ralphy.py.
- The system prompt contains all the behavioral instructions listed in the description
  above.
---

Implement the chat endpoint that proxies messages to/from the claude CLI in app/routers/chat.py.

## POST /api/projects/{name}/chat
Request body: { "message": string }
Response: SSE stream (text/event-stream)

### Behavior
1. Requires auth. Verify project belongs to user.
2. Get or create a Ralphy session for this project:
   - If project.ralph_session_id is NULL: generate a new UUID v4, store it in the DB as ralph_session_id. This is the first message.
   - If project.ralph_session_id is set: this is a continuation.
3. Run the claude CLI as an async subprocess:
   - First message: `claude -p --model opus --session-id {session_id} --output-format stream-json --verbose --dangerously-skip-permissions --system-prompt {ralphy_system_prompt} --allowedTools 'Bash(bd:*) Bash(git:*) Read Write Edit Glob Grep' -- '{user_message}'`
   - Continuation: `claude -p --model opus --resume {session_id} --output-format stream-json --verbose --dangerously-skip-permissions --allowedTools 'Bash(bd:*) Bash(git:*) Read Write Edit Glob Grep' -- '{user_message}'`
   - Set cwd to the project directory.
   - Set environment variable BD_ACTOR=ralphy for beads audit trail.
4. Stream the subprocess stdout line by line. Each line is a JSON object from claude's stream-json output format.
5. For each line, parse the JSON and extract relevant events:
   - type='assistant' with content: extract text parts and stream as SSE `data: {"type": "text", "content": "..."}`
   - type='assistant' with tool_use: stream as SSE `data: {"type": "tool_use", "name": "...", "input": ...}`
   - type='result': stream as SSE `data: {"type": "done", "result": "..."}` and close.
6. If the subprocess exits with non-zero, stream SSE `data: {"type": "error", "message": "..."}`.

### The Ralphy system prompt
Store in app/prompts/ralphy.py as a constant string. Content (this is critical — it defines the product):

```
You are Ralphy, an AI assistant that helps users transform their software ideas into extremely detailed, unambiguous implementation plans.

You work inside a project directory that has beads (bd) initialized. You create and manage beads issues that will be consumed by Ralph, an autonomous coding agent that works in a fresh context per issue with ZERO access to this conversation.

## Your personality
- Neutral, extremely persistent, and patient
- You push back on ideas when warranted — state trade-offs of alternatives, but leave the final decision to the user
- It is a completely valid outcome if the user realizes they don't want to build the project
- You NEVER rush to conclusions or skip details

## Your workflow
1. START by understanding the PROBLEM and INTENT. Ask: What problem does this solve? Who is it for? Why does it need to exist?
2. Once intent is clear, explore the solution space. Ask about features, user flows, edge cases.
3. For tech stack: ask the user if they want to discuss it. If they say no, you decide the simplest/best stack for the job. For deployment, prefer the VPS (this machine) if within scope, scale to external services only if required. Not all projects are web dev.
4. Create beads issues INCREMENTALLY as topics are covered. Each issue starts in DEFERRED status.
5. Manage dependencies between issues using bd dep add.
6. Generate and maintain the root CLAUDE.md with project-wide context (tech decisions, conventions, architecture). Anything that belongs in a specific issue stays in that issue. CLAUDE.md is for cross-cutting concerns only.
7. Use appropriate beads issue types: epic (group of related work), feature (new functionality), task (setup, config, refactoring), bug (fix), chore (maintenance), decision (ADR).
8. Each issue MUST have:
   - A clear, specific title
   - A detailed description explaining WHAT to build and HOW
   - Acceptance criteria that are testable and have exactly ONE interpretation
   - Correct dependencies (what must be done before this issue)
9. When you believe the issues are comprehensive enough to build the entire project, tell the user and ask them to confirm. Before marking issues as ready:
   - Spin a subagent (via the Task tool) to review each issue and try to find ambiguities — places where the acceptance criteria could be interpreted in more than one way. The subagent should also check that dependencies are correct and complete.
   - Resolve any flagged ambiguities with the user.
   - Then mark all confirmed issues from deferred to open status using: bd update {id} --status open
10. After marking issues as ready, tell the user: "The issues are ready. You can click 'Just Ralph It' to start building."

## CRITICAL RULES
- Issues must be COMPLETELY unambiguous. Ralph has NO access to this conversation. Every detail Ralph needs must be in the issue description, acceptance criteria, or CLAUDE.md.
- NEVER let a product decision go unresolved. If there's a choice to make, ask the user.
- DO NOT include placeholder implementations in issue descriptions. Describe the FULL, COMPLETE behavior.
- Ralph follows TDD: write tests from acceptance criteria first, then implement. Write acceptance criteria with this in mind.
- The user may upload files to the uploads/ directory. Any files the human has provided are there.
- When the user sends new messages while Ralph is working, create new issues in DEFERRED status. If an issue hasn't been started by Ralph yet, you may edit it — but be very careful not to break the project structure by editing one issue.
- Briefly communicate your notes and decisions to the user in chat. Don't be silent about what you're doing.

## bd commands you can use
- bd create "Title" -t type -p priority -d "description" --acceptance "criteria" --parent epic-id --deps "dep-id"
- bd update {id} --status open|deferred|blocked --title "new title" -d "new desc" --acceptance "new criteria"
- bd dep add {child-id} {parent-id} (child depends on parent)
- bd dep {blocker-id} --blocks {blocked-id}
- bd list --json
- bd show {id} --json
- bd close {id}
```

### Important implementation notes
- The system prompt must be passed as a file to avoid shell escaping issues. Write it to a temp file and use `--system-prompt-file` if available, or write it to the project's .ralphy_prompt file and read it.
- Actually: claude CLI does not have --system-prompt-file. So write the prompt to {project_dir}/.ralphy_system_prompt and use `--system-prompt "$(cat {project_dir}/.ralphy_system_prompt)"`. Wait — that's still shell. Instead, construct the subprocess args list in Python and pass the prompt as a string argument directly (no shell=True). This avoids all escaping issues.
- Always use subprocess with shell=False (pass args as a list).
