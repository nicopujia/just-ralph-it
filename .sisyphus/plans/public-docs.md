# Public Docs Hub for JRI

## TL;DR
> **Summary**: Add a public `/docs` hub backed by markdown content, rendered through FastAPI/Jinja, with explicit child pages for Overview, Agents, Best Practices, Pricing, Privacy/Security, Deployment, and FAQ.
> **Deliverables**:
> - Public `/docs` route plus 7 child pages
> - Markdown content source under `src/content/docs/`
> - Public docs layout/navigation with logged-out and logged-in CTAs
> - Content aligned with current pricing/config and the original PRD philosophy
> **Effort**: Large
> **Parallel**: YES - 2 waves
> **Critical Path**: 1 → 2 → 3 → 4/5/6/7/8

## Context
### Original Request
Create public docs at `/docs` explaining how JRI works, the two agents, best practices, what to expect, pricing, and related product guidance. Keep the docs in markdown for easier future editing.

### Interview Summary
- Audience: both prospects and active users, with one unified narrative
- Tone: clear, direct, text-first
- IA: `/docs` hub plus dedicated child pages, not a single long page
- Required pages: Overview, Agents, Best Practices, Pricing, Privacy/Security, FAQ, Deployment
- Pricing must be fully public and show: 3 free projects, `$4/task`, `$20/mo Pro`
- Pro should be described as unlimited projects plus 1 VPS for all projects
- Public workflow framing: idea → spec → build → iterate
- Use Ralph and Ralphy prominently, but explain them with a simple mental model
- Best-practice guidance should emphasize continuing the conversation until ambiguities are resolved
- Docs should mention GitHub login, repo creation, and task-file based work structure in user-facing terms
- Philosophy from the original PRD should be prominent
- Ownership/risk should be stated clearly; hard limitations should be mentioned lightly
- Privacy wording should remain minimal; security wording must avoid vendor/model details and formal guarantee language
- Founder support should be described as available when needed, without SLA-style promises
- No automated tests requested; all verification remains agent-executed

### Metis Review (gaps addressed)
- Fixed IA default: canonical public docs live under `/docs/*`, not separate `/pricing` or `/privacy` pages in v1
- Fixed routing default: use explicit routes for each approved page instead of slug-driven catchalls in v1
- Fixed content default: markdown lives in `src/content/docs/` with lightweight frontmatter for title/nav metadata and server-side rendering
- Fixed pricing drift risk: source factual pricing values from `app.config` and/or `/api/pricing`, not hardcoded disconnected copy
- Fixed nav risk: docs pages get their own public nav behavior rather than inheriting the authenticated app nav unchanged
- Fixed scope creep: exclude search, versioning, changelog, localization, CMS, legal rewrites, and internal deployment runbooks

## Work Objectives
### Core Objective
Implement a public documentation system for JRI that is easy to maintain in markdown, accurate to the current product, faithful to the PRD philosophy, and usable by both prospects and active users without exposing internal-only details.

### Deliverables
- `/docs` hub page at `GET /docs`
- Explicit child pages:
  - `/docs/overview` — H1/title label: `Overview`
  - `/docs/agents` — H1/title label: `Agents`
  - `/docs/best-practices` — H1/title label: `Best Practices`
  - `/docs/pricing` — H1/title label: `Pricing`
  - `/docs/privacy-security` — H1/title label: `Privacy & Security`
  - `/docs/deployment` — H1/title label: `Deployment`
  - `/docs/faq` — H1/title label: `FAQ`
- Hub H1/title label: `Public Docs`
- Shared docs nav order: `Public Docs`, `Overview`, `Agents`, `Best Practices`, `Pricing`, `Privacy & Security`, `Deployment`, `FAQ`
- Markdown source files in `src/content/docs/`:
  - `overview.md`
  - `agents.md`
  - `best-practices.md`
  - `pricing.md`
  - `privacy-security.md`
  - `deployment.md`
  - `faq.md`
- Shared docs metadata/index module in app code mapping route → markdown file → nav label → page title
- Public docs template(s) with breadcrumb, page title, section nav, and CTA behavior
- Entry points to docs from the landing page and from docs pages themselves

### Definition of Done (verifiable conditions with commands)
- `curl -i http://127.0.0.1:8000/docs` returns `200` and contains `Public Docs` hub content
- `curl -i http://127.0.0.1:8000/docs/pricing` returns `200` and contains `3 free projects`, `$4/task`, and `$20/mo Pro`
- `curl -i http://127.0.0.1:8000/docs/agents` returns `200` and contains both `Ralphy` and `Ralph`
- `curl -i http://127.0.0.1:8000/docs/privacy-security` returns `200` and does **not** contain model/vendor names or beta-gating details
- `curl -i http://127.0.0.1:8000/docs/nope` returns the explicitly planned not-found behavior for v1 (`404`)
- A browser QA pass confirms the landing page exposes a docs entry point and docs pages show the correct CTA for logged-out vs logged-in states

### Must Have
- Markdown-backed content that non-developers can edit later without touching template markup
- Accurate public pricing values tied to current app config
- Clear public explanation of Ralph vs Ralphy
- Prominent philosophy around clarifying intent before coding
- Clear ownership/risk language
- Light-but-present limitations language
- Dedicated deployment page that explains user-facing behavior while excluding internal ops details
- Stable explicit routes for all approved docs pages

### Must NOT Have (guardrails, AI slop patterns, scope boundaries)
- No docs search, versioning, localization, CMS, changelog, or full knowledge-base scope
- No separate `/pricing` or `/privacy` routes in v1
- No invented product promises, legal claims, SLA promises, or enterprise-security language
- No vendor/model details, beta restrictions, whitelist/freelist mechanics, or internal operational runbooks
- No generic AI-marketing filler or manifesto prose detached from confirmed product behavior
- No automated test suite additions for this work

## Verification Strategy
> ZERO HUMAN INTERVENTION — all verification is agent-executed.
- Test decision: none (no automated tests), per user instruction
- QA policy: Every task includes agent-executed scenarios with exact selectors/commands
- Evidence: `.sisyphus/evidence/task-{N}-{slug}.{ext}`
- Runtime assumption for QA commands: local app running at `http://127.0.0.1:8000`

## Execution Strategy
### Parallel Execution Waves
> Target: 5-8 tasks per wave. <3 per wave (except final) = under-splitting.
> Extract shared dependencies as Wave-1 tasks for max parallelism.

Wave 1: foundation — content system, routing, docs layout/navigation

Wave 2: approved content pages — overview/agents, best-practices/faq, pricing/deployment, privacy/security

### Dependency Matrix (full, all tasks)
| Task | Depends On | Blocks |
|------|------------|--------|
| 1 | — | 2, 4, 5, 6, 7, 8 |
| 2 | 1 | 3, 4, 5, 6, 7, 8 |
| 3 | 2 | 4, 5, 6, 7, 8 |
| 4 | 1, 2, 3 | F1-F4 |
| 5 | 1, 2, 3 | F1-F4 |
| 6 | 1, 2, 3 | F1-F4 |
| 7 | 1, 2, 3 | F1-F4 |
| 8 | 1, 2, 3 | F1-F4 |

### Agent Dispatch Summary (wave → task count → categories)
- Wave 1 → 3 tasks → `unspecified-high`, `visual-engineering`
- Wave 2 → 5 tasks → `writing`, `unspecified-high`
- Final Verification Wave → 4 tasks → `oracle`, `unspecified-high`, `deep`

## TODOs
> Implementation + Test = ONE task. Never separate.
> EVERY task MUST have: Agent Profile + Parallelization + QA Scenarios.

- [ ] 1. Establish markdown docs content system

  **What to do**: Create a dedicated markdown-backed docs content system under `src/content/docs/` with one file per approved page (`overview.md`, `agents.md`, `best-practices.md`, `pricing.md`, `privacy-security.md`, `deployment.md`, `faq.md`). Add a small app-side helper module (recommended path: `src/app/docs.py`) that (a) defines the canonical docs registry for v1, (b) parses YAML frontmatter using the same frontmatter shape already used elsewhere in the repo, and (c) renders markdown server-side after escaping raw HTML before conversion. Require frontmatter fields `title`, `description`, `nav_label`, and `order`; derive the route slug from the registry, not from user-provided frontmatter.
  **Must NOT do**: Do not add slug-based dynamic routing, docs search, versioning, or a CMS. Do not permit raw HTML passthrough in markdown. Do not hardcode pricing constants in this helper.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` — Reason: this is core architecture for content loading, sanitization, and metadata ownership.
  - Skills: `[]` — No injected skills are needed; repo-native patterns are sufficient.
  - Omitted: `[]` — No relevant repo skill is available.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 2, 4, 5, 6, 7, 8 | Blocked By: none

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/app/tasks.py:14-36` — Existing YAML frontmatter composition pattern; mirror the same shape for docs metadata.
  - Pattern: `src/app/tasks.py:39-55` — Existing frontmatter parsing logic to reuse or adapt for docs markdown files.
  - Pattern: `src/templates/project.html:1236-1274` — Existing markdown rendering + HTML escaping approach; docs rendering should preserve the escaping principle even if implemented server-side.
  - Dependency: `pyproject.toml:6-17` — `markdown==3.7` already exists; avoid unnecessary dependency sprawl.
  - Confirmed content inventory: only these v1 pages are allowed — Overview, Agents, Best Practices, Pricing, Privacy/Security, Deployment, FAQ.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `python - <<'PY'
from app.docs import DOCS_PAGES
expected = ["overview", "agents", "best-practices", "pricing", "privacy-security", "deployment", "faq"]
assert list(DOCS_PAGES.keys()) == expected
for slug, page in DOCS_PAGES.items():
    assert page["title"] and page["nav_label"]
PY` exits 0.
  - [ ] `python - <<'PY'
from app.docs import render_doc_page
html = render_doc_page("# Hello\n\n<script>alert(1)</script>\n")
assert "<h1>Hello</h1>" in html
assert "<script>" not in html
assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
PY` exits 0.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Registry exposes every approved docs page
    Tool: Bash
    Steps: Run the first acceptance script; capture stdout/stderr to .sisyphus/evidence/task-1-markdown-system.txt
    Expected: Script exits 0 and confirms exactly seven child pages in the approved order
    Evidence: .sisyphus/evidence/task-1-markdown-system.txt

  Scenario: Raw HTML is escaped in markdown rendering
    Tool: Bash
    Steps: Run the second acceptance script with a literal <script> tag in sample markdown; save output/assertions to .sisyphus/evidence/task-1-markdown-system-error.txt
    Expected: Rendered HTML contains escaped script text and no executable script element
    Evidence: .sisyphus/evidence/task-1-markdown-system-error.txt
  ```

  **Commit**: YES | Message: `feat(docs): add markdown docs registry` | Files: `src/app/docs.py`, `src/content/docs/*.md`

- [ ] 2. Add explicit public docs routes in the pages router

  **What to do**: Extend `src/app/routers/pages.py` to serve public docs routes from the registry created in Task 1. Implement explicit handlers for `/docs`, `/docs/overview`, `/docs/agents`, `/docs/best-practices`, `/docs/pricing`, `/docs/privacy-security`, `/docs/deployment`, and `/docs/faq`. Keep docs public for both anonymous and authenticated users. Reuse the existing logged-in detection helper so docs templates know whether to show `LOGIN WITH GITHUB` or `GO TO DASHBOARD`. Leave unknown `/docs/*` paths to FastAPI’s normal `404` behavior; do not introduce a slug catchall in v1.
  **Must NOT do**: Do not move docs behind auth, do not add `/pricing` or `/privacy` standalone routes, and do not require JavaScript for first paint of docs content.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` — Reason: route ownership and request/view-state integration are correctness-sensitive.
  - Skills: `[]` — Existing router patterns are sufficient.
  - Omitted: `[]` — No relevant repo skill is available.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 3, 4, 5, 6, 7, 8 | Blocked By: 1

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/app/routers/pages.py:14-17` — Existing public page router + shared templates setup.
  - Pattern: `src/app/routers/pages.py:20-35` — Existing `_is_logged_in` helper to reuse for docs CTA state.
  - Pattern: `src/app/routers/pages.py:38-43` — Public landing page handler pattern; docs pages should remain public in the same style.
  - Pattern: `src/app/main.py:36-44` — Router registration already includes `pages.router`; prefer extending it instead of adding a second public router unless absolutely necessary.
  - Contract: `src/app/docs.py` — Canonical mapping from approved slug to markdown source and metadata.
  - Confirmed route ownership: all v1 public docs pages live under `/docs/*` only.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `python - <<'PY'
import httpx
for path in ["/docs", "/docs/overview", "/docs/agents", "/docs/best-practices", "/docs/pricing", "/docs/privacy-security", "/docs/deployment", "/docs/faq"]:
    r = httpx.get(f"http://127.0.0.1:8000{path}", follow_redirects=False, timeout=10)
    assert r.status_code == 200, (path, r.status_code)
PY` exits 0.
  - [ ] `python - <<'PY'
import httpx
r = httpx.get("http://127.0.0.1:8000/docs/nope", follow_redirects=False, timeout=10)
assert r.status_code == 404, r.status_code
PY` exits 0.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Every approved docs route resolves publicly
    Tool: Bash
    Steps: Start the app locally, run the first acceptance script, and save route/status output to .sisyphus/evidence/task-2-docs-routes.txt
    Expected: Every approved /docs route returns HTTP 200 without requiring a session cookie
    Evidence: .sisyphus/evidence/task-2-docs-routes.txt

  Scenario: Unknown docs path stays unsupported in v1
    Tool: Bash
    Steps: Run the second acceptance script against /docs/nope and capture the response to .sisyphus/evidence/task-2-docs-routes-error.txt
    Expected: The response is HTTP 404, proving no slug catchall was introduced
    Evidence: .sisyphus/evidence/task-2-docs-routes-error.txt
  ```

  **Commit**: YES | Message: `feat(docs): add public docs routes` | Files: `src/app/routers/pages.py`

- [ ] 3. Build the public docs layout, navigation, and landing entry point

  **What to do**: Add dedicated docs templates (recommended: `src/templates/docs_index.html` and `src/templates/docs_page.html`) extending `base.html` but overriding nav so docs always render a public docs header. Header behavior must be exact: left side breadcrumb `Just Ralph It / Docs / {Current Page}` for child pages and `Just Ralph It / Docs` for the hub; right side shows `LOGIN WITH GITHUB` linking `/auth/login` when anonymous, or `GO TO DASHBOARD` linking `/projects` when logged in. Add a persistent docs section-nav listing Hub + all seven child pages. Update `src/templates/landing.html` so the landing card has a secondary `READ DOCS` link to `/docs`; keep existing primary CTA behavior unchanged.
  **Must NOT do**: Do not alter dashboard, new-project, or project-page navigation outside what docs pages themselves need. Do not introduce a sidebar/search UI in v1.

  **Recommended Agent Profile**:
  - Category: `visual-engineering` — Reason: this is public-page IA, template structure, and CTA behavior.
  - Skills: `[]` — Existing template patterns are enough.
  - Omitted: `[]` — No relevant repo skill is available.

  **Parallelization**: Can Parallel: NO | Wave 1 | Blocks: 4, 5, 6, 7, 8 | Blocked By: 2

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/templates/base.html:163-177` — Shared authenticated-nav block; docs pages must override this rather than inherit it unchanged.
  - Pattern: `src/templates/landing.html:155-187` — Existing landing card CTA area; add `READ DOCS` here without changing primary login/dashboard behavior.
  - Pattern: `src/templates/landing.html:66-80` — Card treatment and visual tone to mirror lightly for docs hub cards.
  - Pattern: `src/app/routers/pages.py:38-43` — Landing already passes `logged_in`; docs routes must pass equivalent view-state to templates.
  - Confirmed CTA rule: anonymous docs users see `LOGIN WITH GITHUB`; authenticated docs users see `GO TO DASHBOARD`.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `python - <<'PY'
import httpx
html = httpx.get('http://127.0.0.1:8000/').text
assert 'READ DOCS' in html
assert '/docs' in html
PY` exits 0.
  - [ ] `python - <<'PY'
import httpx
html = httpx.get('http://127.0.0.1:8000/docs/overview').text
assert 'Just Ralph It / Docs / Overview' in html
assert 'LOGIN WITH GITHUB' in html
PY` exits 0 for anonymous state.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Landing page exposes docs as a public entry point
    Tool: Playwright
    Steps: Open http://127.0.0.1:8000/; wait for the landing card; verify `a:has-text("READ DOCS")` points to `/docs`; click it; verify URL becomes `/docs`
    Expected: The secondary docs CTA is visible and navigates correctly without affecting the primary CTA
    Evidence: .sisyphus/evidence/task-3-docs-layout.png

  Scenario: Docs CTA changes based on auth state
    Tool: Playwright
    Steps: First as anonymous user, open `/docs/overview` and assert `a:has-text("LOGIN WITH GITHUB")` is visible; then with a valid session cookie (same pattern as `tests/test_e2e_happy_paths.py:97-118`), reopen `/docs/overview` and assert `a:has-text("GO TO DASHBOARD")` points to `/projects`
    Expected: Anonymous and authenticated docs views show the exact planned CTA switch
    Evidence: .sisyphus/evidence/task-3-docs-layout-error.png
  ```

  **Commit**: YES | Message: `feat(docs): add docs layout and landing entry` | Files: `src/templates/docs_index.html`, `src/templates/docs_page.html`, `src/templates/landing.html`

- [ ] 4. Implement the `/docs` hub page and docs index copy

  **What to do**: Build the hub page as the canonical docs index at `/docs`. Use the docs registry order to render a card/list for all seven child pages. The hub page copy must include: (1) an H1 of `Public Docs`, (2) a short explanation that JRI helps turn ideas into clear, buildable task specs before Ralph executes, (3) the workflow `idea → spec → build → iterate`, and (4) a short “Start here” framing that sends readers to Overview, Agents, and Best Practices first. Add a brief philosophy callout based on the PRD: clarify intent thoroughly before coding; shallow specs produce shallow output. Keep the hub public, text-first, and concise.
  **Must NOT do**: Do not turn the hub into a separate marketing landing page, do not add testimonials, and do not introduce claims beyond the approved product positioning.

  **Recommended Agent Profile**:
  - Category: `writing` — Reason: this is public-facing structure + concise foundational copy.
  - Skills: `[]` — No injected writing skill is available.
  - Omitted: `[]` — No relevant repo skill is available.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: F1-F4 | Blocked By: 1, 2, 3

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/templates/landing.html:166-187` — Existing concise card-based public messaging; mirror its brevity, not its exact copy.
  - Pattern: `src/templates/base.html:108-158` — Existing nav/breadcrumb styling primitives to keep docs visually consistent.
  - Source of truth: `src/app/docs.py` — Use registry order and metadata rather than hardcoding page lists in the template.
  - Confirmed copy decisions: hub must prominently reflect the PRD philosophy and route readers first to Overview, Agents, and Best Practices.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `python - <<'PY'
import httpx
html = httpx.get('http://127.0.0.1:8000/docs', timeout=10).text
required = ['Public Docs', 'idea → spec → build → iterate', 'Overview', 'Agents', 'Best Practices', 'Pricing', 'Privacy/Security', 'Deployment', 'FAQ']
for item in required:
    assert item in html, item
PY` exits 0.
  - [ ] `python - <<'PY'
import httpx
html = httpx.get('http://127.0.0.1:8000/docs', timeout=10).text
assert 'clarify intent' in html.lower()
assert 'shallow' in html.lower()
PY` exits 0.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Docs hub presents the full approved IA
    Tool: Playwright
    Steps: Open http://127.0.0.1:8000/docs; verify `h1` is `Public Docs`; assert visible links for Overview, Agents, Best Practices, Pricing, Privacy/Security, Deployment, and FAQ; click Overview and confirm `/docs/overview`
    Expected: The hub functions as the single index for the approved docs tree
    Evidence: .sisyphus/evidence/task-4-docs-hub.png

  Scenario: Hub copy stays within approved scope
    Tool: Bash
    Steps: Fetch `/docs`; assert the page contains the philosophy callout but does not contain forbidden strings such as `whitelist`, `freelist`, or model/provider names; write results to .sisyphus/evidence/task-4-docs-hub-error.txt
    Expected: Required framing is present and forbidden internal details are absent
    Evidence: .sisyphus/evidence/task-4-docs-hub-error.txt
  ```

  **Commit**: YES | Message: `feat(docs): add public docs hub` | Files: `src/templates/docs_index.html`, `src/app/routers/pages.py`

- [ ] 5. Author the Overview and Agents pages

  **What to do**: Write `src/content/docs/overview.md` and `src/content/docs/agents.md` with final approved public messaging. `overview.md` must explain what JRI is, who it is for, why clarified intent matters, and how the end-to-end flow works (`idea → spec → build → iterate`). It must explicitly say JRI works best from clear tasks/specs, is suitable for non-coders, and differs from both hiring developers and prompt-first copilots by forcing deeper clarification before coding. `agents.md` must explain Ralph and Ralphy with a simple mental model: Ralphy extracts/clarifies intent into buildable tasks; Ralph executes from that clarified plan. Mention task files in user-facing terms, but do not expose internal folder structures or implementation internals.
  **Must NOT do**: Do not mention vendor/model names, internal prompts, beta gating, or low-level `.jri/tasks` mechanics. Do not undermine confidence by hedging deliverable quality beyond the approved collaboration framing.

  **Recommended Agent Profile**:
  - Category: `writing` — Reason: this is core explanatory product copy with strict scope and terminology decisions.
  - Skills: `[]` — No injected writing skill is available.
  - Omitted: `[]` — No relevant repo skill is available.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: F1-F4 | Blocked By: 1, 2, 3

  **References** (executor has NO interview context — be exhaustive):
  - Source: `README.md` — Current concise explanation of Ralph vs Ralphy and app architecture.
  - Pattern: `src/app/prompts/ralphy.py` (explore summary only) — Ralphy is the interviewer/planner; keep public wording high-level.
  - Pattern: `src/app/prompts/ralph.py` (explore summary only) — Ralph is the builder/executor; keep public wording high-level.
  - User-approved positioning: explain the agents with a simple mental model; use `Ralph` and `Ralphy` prominently; make philosophy prominent; compare against both hiring devs and coding copilots.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `python - <<'PY'
import httpx
overview = httpx.get('http://127.0.0.1:8000/docs/overview', timeout=10).text
for item in ['What JRI is', 'idea → spec → build → iterate', 'non-coders', 'clarify intent']:
    assert item in overview, item
agents = httpx.get('http://127.0.0.1:8000/docs/agents', timeout=10).text
for item in ['Ralphy', 'Ralph', 'task', 'clarify', 'build']:
    assert item in agents, item
PY` exits 0.
  - [ ] `python - <<'PY'
import httpx
text = httpx.get('http://127.0.0.1:8000/docs/agents', timeout=10).text.lower()
for forbidden in ['glm-5', 'gpt-5', 'opencode', 'whitelist', 'freelist']:
    assert forbidden not in text, forbidden
PY` exits 0.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Overview and Agents explain the mental model clearly
    Tool: Playwright
    Steps: Open `/docs/overview`; verify visible headings for what JRI is and how it works; then navigate to `/docs/agents`; verify the page contains separate sections for Ralphy and Ralph
    Expected: Both pages are readable, public, and aligned to the approved mental model
    Evidence: .sisyphus/evidence/task-5-overview-agents.png

  Scenario: Forbidden internal details do not leak into agent docs
    Tool: Bash
    Steps: Fetch `/docs/overview` and `/docs/agents`; assert absence of vendor/model names and beta/internal terms; save assertions to .sisyphus/evidence/task-5-overview-agents-error.txt
    Expected: The pages remain user-facing and avoid internal implementation details
    Evidence: .sisyphus/evidence/task-5-overview-agents-error.txt
  ```

  **Commit**: YES | Message: `docs(public): add overview and agents pages` | Files: `src/content/docs/overview.md`, `src/content/docs/agents.md`

- [ ] 6. Author the Best Practices and FAQ pages

  **What to do**: Write `src/content/docs/best-practices.md` and `src/content/docs/faq.md`. `best-practices.md` must explicitly teach the approved collaboration behavior: start with a clear project idea, keep chatting until ambiguities are resolved, prefer concrete expectations, and keep refining the spec before Ralph runs. It must not tell users to break work into phases/tasks as the primary best practice; the main advice is continued clarification. `faq.md` must answer exactly these objections in v1: (1) can non-coders use this? (2) how is this different from hiring developers or using coding copilots? (3) what happens if it fails? The failure answer must mention founder support is available when needed, without promising response times or legal guarantees.
  **Must NOT do**: Do not add unrelated FAQ entries about beta programs, vendor choices, or enterprise compliance. Do not create a tutorial-style playbook.

  **Recommended Agent Profile**:
  - Category: `writing` — Reason: these pages are editorially constrained and depend on exact user-approved guidance.
  - Skills: `[]` — No injected writing skill is available.
  - Omitted: `[]` — No relevant repo skill is available.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: F1-F4 | Blocked By: 1, 2, 3

  **References** (executor has NO interview context — be exhaustive):
  - PRD guidance (user-provided): thorough clarification before coding is the core philosophy.
  - Confirmed best-practice correction: the main best practice is to keep chatting until ambiguities are gone, not to emphasize phased breakdown as the headline advice.
  - Confirmed FAQ scope: non-coders, differentiation, and failure/support only.
  - Support wording constraint: “available when needed,” with no SLA or formal guarantee language.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `python - <<'PY'
import httpx
best = httpx.get('http://127.0.0.1:8000/docs/best-practices', timeout=10).text.lower()
assert 'clear project idea' in best
assert 'ambigu' in best
assert 'keep chatting' in best or 'continue the conversation' in best
faq = httpx.get('http://127.0.0.1:8000/docs/faq', timeout=10).text
for item in ['Can non-coders use this?', 'How is this different', 'What happens if it fails?']:
    assert item in faq, item
PY` exits 0.
  - [ ] `python - <<'PY'
import httpx
faq = httpx.get('http://127.0.0.1:8000/docs/faq', timeout=10).text.lower()
assert 'available when needed' in faq
for forbidden in ['response time', 'sla', 'guarantee']:
    assert forbidden not in faq, forbidden
PY` exits 0.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Best Practices reinforces the approved collaboration behavior
    Tool: Playwright
    Steps: Open `/docs/best-practices`; verify the page visibly tells users to come with a clear project idea and continue clarifying until ambiguities are gone; capture a screenshot
    Expected: The page teaches clarification-first usage, not a tutorial playbook
    Evidence: .sisyphus/evidence/task-6-best-practices-faq.png

  Scenario: FAQ support wording stays within approved boundaries
    Tool: Bash
    Steps: Fetch `/docs/faq`; assert presence of `available when needed` and absence of `SLA`, `guaranteed`, or timing promises; write results to .sisyphus/evidence/task-6-best-practices-faq-error.txt
    Expected: FAQ gives high-touch support guidance without overpromising
    Evidence: .sisyphus/evidence/task-6-best-practices-faq-error.txt
  ```

  **Commit**: YES | Message: `docs(public): add best practices and faq pages` | Files: `src/content/docs/best-practices.md`, `src/content/docs/faq.md`

- [ ] 7. Author the Pricing and Deployment pages from current product truth

  **What to do**: Write `src/content/docs/pricing.md` and `src/content/docs/deployment.md` and wire them so factual pricing values come from current product truth, not stale copy. The Pricing page must explicitly show `3 free projects`, `$4/task`, and `$20/mo Pro`, and describe Pro as including unlimited projects plus `1 VPS for all your projects` (the current model, replacing the outdated per-project VPS language from the old PRD). It must explain pricing in plain language without exposing freelist/beta exceptions. The Deployment page must stay public and dedicated, but limited to user-facing expectations: JRI can deploy projects, deployment is part of the product journey, and infrastructure details stay intentionally abstract. It may mention GitHub repo creation and deployed project availability in user-facing terms, but must not expose internal subdomain routing, systemd, or other ops internals.
  **Must NOT do**: Do not hardcode old PRD deployment semantics, do not mention whitelist/freelist handling, and do not expose internal deployment commands or architecture details.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` — Reason: this work mixes editorial copy with correctness-sensitive product facts and anti-drift rules.
  - Skills: `[]` — Existing repo references are enough.
  - Omitted: `[]` — No relevant repo skill is available.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: F1-F4 | Blocked By: 1, 2, 3

  **References** (executor has NO interview context — be exhaustive):
  - Source of truth: `src/app/config.py:72-75` — Current pricing constants: 3 free projects, $4/task, $20/mo for unlimited projects + VPS.
  - Source of truth: `src/app/routers/ralph.py:463-470` — Public pricing API already exposes the same values in dollars.
  - Pattern: `src/templates/new_project.html:113-120` — Existing public-ish UI fetches pricing dynamically; docs should stay aligned with the same truth source.
  - Pattern: `src/templates/project.html:1095-1108` — Current paid-plan messaging uses `1 VPS for all your projects`; preserve this updated model.
  - Constraint from user: public docs must include pricing explicitly, but beta restrictions and unstable internal details must stay out.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `python - <<'PY'
import httpx
html = httpx.get('http://127.0.0.1:8000/docs/pricing', timeout=10).text
for item in ['3 free projects', '$4/task', '$20/mo Pro', '1 VPS for all your projects']:
    assert item in html, item
PY` exits 0.
  - [ ] `python - <<'PY'
import httpx
html = httpx.get('http://127.0.0.1:8000/docs/deployment', timeout=10).text.lower()
for required in ['deployment', 'github', 'project']:
    assert required in html, required
for forbidden in ['systemd', 'x-subdomain', 'whitelist', 'freelist']:
    assert forbidden not in html, forbidden
PY` exits 0.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Pricing page shows the exact public plan values
    Tool: Playwright
    Steps: Open `/docs/pricing`; verify visible text for `3 free projects`, `$4/task`, and `$20/mo Pro`; verify Pro copy includes `1 VPS for all your projects`
    Expected: The page reflects current app pricing and the updated per-user VPS framing
    Evidence: .sisyphus/evidence/task-7-pricing-deployment.png

  Scenario: Deployment page stays user-facing and avoids ops leakage
    Tool: Bash
    Steps: Fetch `/docs/deployment`; assert presence of user-facing deployment language and absence of `systemd`, `x-subdomain`, `whitelist`, and `freelist`; save output to .sisyphus/evidence/task-7-pricing-deployment-error.txt
    Expected: The page explains deployment at the right abstraction level
    Evidence: .sisyphus/evidence/task-7-pricing-deployment-error.txt
  ```

  **Commit**: YES | Message: `docs(public): add pricing and deployment pages` | Files: `src/content/docs/pricing.md`, `src/content/docs/deployment.md`

- [ ] 8. Author the Privacy/Security page and finish docs-wide polish

  **What to do**: Write `src/content/docs/privacy-security.md` with the approved minimal data-handling claim, clear ownership/risk language, and light limitations language. The page must say JRI uses the project context and account data needed to operate the service, uses GitHub-authenticated access, and does not publish vendor/model details or formal security guarantees. It must clearly state that users own the code, keys, and accountability for what they build. Mention limits lightly in user-facing terms (examples: physical presence, locked services, human identity, human taste) without turning the page into a fear-driven warning. Finish docs-wide polish by ensuring all docs pages cross-link through the shared docs nav, page titles/H1s match the approved IA, and landing/docs navigation remains coherent.
  **Must NOT do**: Do not write legal-policy language, compliance claims, retention schedules, vendor disclosures, or beta/internal access rules. Do not invent extra limitation categories beyond the PRD-backed examples unless required for clarity.

  **Recommended Agent Profile**:
  - Category: `unspecified-high` — Reason: this page combines sensitive product positioning with final docs consistency work.
  - Skills: `[]` — Existing repo references are enough.
  - Omitted: `[]` — No relevant repo skill is available.

  **Parallelization**: Can Parallel: YES | Wave 2 | Blocks: F1-F4 | Blocked By: 1, 2, 3

  **References** (executor has NO interview context — be exhaustive):
  - Pattern: `src/app/routers/pages.py:20-35` — Use the same login-state concept for docs CTA behavior; privacy copy must remain public and unauthenticated.
  - Constraint: minimal privacy wording only; avoid formal guarantees and vendor/model details.
  - Constraint: ownership/risk should be stated clearly.
  - Constraint: limitations should be mentioned lightly, not as a separate alarmist manifesto.
  - Docs-wide polish dependency: ensure shared docs nav from Task 3 reaches every child page and keeps the approved IA intact.

  **Acceptance Criteria** (agent-executable only):
  - [ ] `python - <<'PY'
import httpx
html = httpx.get('http://127.0.0.1:8000/docs/privacy-security', timeout=10).text
required = ['project context', 'account data', 'GitHub', 'own the code', 'keys', 'accountability']
for item in required:
    assert item in html, item
PY` exits 0.
  - [ ] `python - <<'PY'
import httpx
html = httpx.get('http://127.0.0.1:8000/docs/privacy-security', timeout=10).text.lower()
for forbidden in ['sla', 'soc 2', 'iso 27001', 'glm', 'gpt', 'opencode', 'whitelist', 'freelist']:
    assert forbidden not in html, forbidden
PY` exits 0.

  **QA Scenarios** (MANDATORY — task incomplete without these):
  ```
  Scenario: Privacy/Security page contains the approved trust posture
    Tool: Playwright
    Steps: Open `/docs/privacy-security`; verify visible mentions of project context, GitHub-authenticated access, user ownership/accountability, and a short limitations section; capture screenshot
    Expected: The page reflects the approved minimal trust language and ownership framing
    Evidence: .sisyphus/evidence/task-8-privacy-security.png

  Scenario: Docs-wide nav and forbidden-term sweep pass
    Tool: Bash
    Steps: Fetch every `/docs/*` page; assert each contains the shared docs nav labels and no forbidden trust/internal terms (`soc 2`, `iso 27001`, vendor/model names, beta terms); save results to .sisyphus/evidence/task-8-privacy-security-error.txt
    Expected: All docs pages are consistently linked and remain inside approved disclosure boundaries
    Evidence: .sisyphus/evidence/task-8-privacy-security-error.txt
  ```

  **Commit**: YES | Message: `docs(public): add privacy page and docs polish` | Files: `src/content/docs/privacy-security.md`, `src/templates/docs_page.html`, `src/templates/docs_index.html`, `src/templates/landing.html`

## Final Verification Wave (MANDATORY — after ALL implementation tasks)
> 4 review agents run in PARALLEL. ALL must APPROVE. Present consolidated results to user and get explicit "okay" before completing.
> **Do NOT auto-proceed after verification. Wait for user's explicit approval before marking work complete.**
> **Never mark F1-F4 as checked before getting user's okay.** Rejection or user feedback -> fix -> re-run -> present again -> wait for okay.
- [ ] F1. Plan Compliance Audit — oracle
- [ ] F2. Code Quality Review — unspecified-high
- [ ] F3. Real Manual QA — unspecified-high (+ playwright if UI)
- [ ] F4. Scope Fidelity Check — deep

## Commit Strategy
- Commit 1: foundation for markdown docs loading, explicit docs routing, and public docs layout
- Commit 2: hub plus Overview/Agents/Best Practices/FAQ content
- Commit 3: Pricing/Deployment and Privacy/Security content alignment
- Commit 4: docs entry-point polish and final QA fixes

## Success Criteria
- Public docs are reachable and navigable without authentication
- Every approved page exists at the exact route listed in this plan
- Content is editable from markdown files without changing Jinja templates for routine copy edits
- Public pricing, philosophy, ownership/risk, and support messaging match the decisions captured in this plan
- No forbidden details appear in rendered docs
