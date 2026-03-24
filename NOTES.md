# Just Ralph It — Working Notes

## Final Architecture

### Stack
- **Python 3.12** + **FastAPI** + **uvicorn** behind **nginx** on justralph.it (80/443, Cloudflare SSL)
- **Anthropic Python SDK** for Ralphy (direct API calls, Claude Opus 4.6)
- **`claude` CLI** subprocess for Ralph loop (Claude Opus 4.6)
- **Beads (`bd` v0.60)** as the issue tracker — Dolt-based, filesystem-local, graph-aware dependencies
- **SQLite** for app metadata (users, projects, GitHub tokens — plaintext for now, accepted risk)
- **GitHub OAuth** via GitHub App (credentials in .env)
- **`ralphpujia`** bot account (authed via `gh` CLI) creates repos, pushes commits with `Co-authored-by: <user>`
- **Stripe** for per-project billing (live + test creds in .env)
- **SSE** for all real-time streaming (Ralph stdout, issue updates, notifications)
- **Domain**: justralph.it — nginx currently serves static HTML, will proxy to uvicorn app
- Pre-installed: claude 2.1.81, bd 0.60.0, opencode, python 3.12, node 22, nginx 1.24, gh CLI

### Data Layout
- App DB: `~/jri/data/jri.db` (SQLite)
- Projects: `~/jri/data/<username>/<project-name>/` (each is a git repo with `bd init`)
- User uploads: `<project>/uploads/` (flat directory, name collision → suffix: file.txt, file_1.txt, etc.)

### Pages / Screens

#### 1. Landing Page (`/`)
- "Just Ralph It" as heading
- `<p>`: "Tool to transform your software idea into a very detailed plan and let an AI agent build it entirely."
- "Sign in with GitHub" button
- Minimal, functional design

#### 2. Dashboard (`/dashboard`)
- List of existing projects: name, # of issues, link to GitHub repo
- "New Project" button
- Delete project: confirmation dialog with checkbox (default checked) "Also delete the GitHub repo?"

#### 3. Project Creation (`/new`)
- Fields: **name** (= repo name), **description** (placeholder: "What do you want to build?")
- On submit: `ralphpujia` creates GitHub repo, adds user as collaborator, initializes `bd init` + `git init`, redirects to main page

#### 4. Main Page (`/project/<name>`)
**Left panel: Chat**
- Text input (placeholder mentions markdown encouraged)
- Chat attachments: images/PDFs only, max 3MB — processed by Ralphy multimodally (NOT saved to disk)
- Input disabled only while Ralphy is processing ("AI is typing") — never because of Ralph
- "Just Ralph It" button: appears above input when Ralphy decides issues are ready, disappears after click
- After JRI clicked: "Stop" (graceful, waits for current iteration) and "Resume" buttons replace it

**Right panel: 4 Tabs**
1. **Issues**: Read-only, minimal custom view of beads issues. Grouped by epic. Expandable to show description/acceptance criteria. Polls `bd list --json`.
2. **CLAUDE.md**: Rendered markdown of project's root CLAUDE.md
3. **Ralph** (appears after JRI): Streaming stdout from Ralph's `claude` subprocess via SSE
4. **Uploads**: File manager for `<project>/uploads/`. List files, upload (any type/size), delete, rename. Flat directory.

### Ralphy (Interviewer Agent)

#### Integration
- Anthropic Python SDK, Claude Opus 4.6
- System prompt + tool definitions for `bd` CLI commands (create, update, dep add, etc.)
- Ralphy executes `bd` commands via subprocess on the server

#### Behavior
- **Intent first**: starts by asking about the problem, why it needs to exist
- Neutral, extremely persistent, patient
- Pushes back when warranted — states trade-offs, leaves decisions to user
- Valid outcome: user realizes they don't want to build it
- Tech stack: asks user if they want to discuss it. If not, Ralphy decides simplest/best.
- Deployment: prefers VPS if within scope, scales to external services if needed
- Not all projects are web dev
- Creates issues incrementally as topics are covered (deferred status)
- Briefly communicates notes/decisions in chat
- Issue management is entirely Ralphy's domain
- Instructed that human-provided files are in `uploads/`

#### Readiness Check
1. Ralphy judges completeness
2. User confirms
3. Subagent checks batch of issues + CLAUDE.md for ambiguities (can acceptance criteria be interpreted >1 way?)
4. Ralphy resolves flagged ambiguities with user
5. Issues marked as open → JRI button appears

#### CLAUDE.md
- Project-wide context needed for any issue
- Anything that belongs in a specific issue stays in the issue
- Ralphy generates and maintains it throughout conversation

### Ralph (Builder Agent)

#### Loop Mechanics
```
while there are ready issues:
    issue = `bd ready -n 1 --json`
    `bd update <id> --claim`
    read CLAUDE.md + directory CLAUDE.md files
    read full issue via `bd show <id> --json`
    TDD: write tests from acceptance criteria → implement → verify
    commit to main with Co-authored-by
    `bd close <id>`
    git push
    update CLAUDE.md (root or directory-specific) with discoveries
```

- Fresh `claude` subprocess per issue — completely new context each time
- Root access — installs/does whatever needed
- If deployed software: work in worktree, verify, merge
- If blocked (needs human identity/API keys/etc.): create issue assigned to "Human" with blocking dependency, in-app persistent banner notification, move to next iteration
- If discovers missing dependency: `bd create` new issue, mark current as blocked, stop. Next iteration handles.
- When all issues done: document in project README how to access the software. Ralphy announces completion with green toast.

#### Crash Recovery
- Save loop state before each iteration (issue ID, iteration number) in `~/jri/data/<user>/<project>/.ralph_state`
- On crash: `git reset --hard`, `bd update <id> --status open`, restart loop

### Stripe / Pricing
- Free tier: 1 project max, Ralphy chat always free
- Paid: base subscription → unlimited projects (per-project bidding still applies)
- On JRI click: bidding agent reads all issues, estimates cost per issue, sums, approximates upwards
- Stripe checkout → payment succeeds → Ralph loop starts
- Payment fails → Ralph doesn't start
- Future: +$10/mo for per-project VPS provisioning

### GitHub Integration
- **`ralphpujia`** bot account: creates repos, pushes code (authed via `gh` CLI)
- Commits: `Co-authored-by: User Name <user@email.com>`
- User added as collaborator
- Repo initialized with `bd init` + `git init` + initial push at project creation

### Key Constraints
- Issues must be COMPLETELY unambiguous
- Ralph must NOT produce buggy software that fails acceptance criteria
- Ralphy must NEVER let product decisions pass unresolved
- No collaboration features in v1
- No legacy repo support in v1
- 24-hour hackathon

### Security Notes (accepted for v1)
- GitHub tokens stored plaintext in SQLite
- Single shared OpenCode Zen API key (actually: using Anthropic SDK directly now with Opus)
- Root access for Ralph on shared VPS
