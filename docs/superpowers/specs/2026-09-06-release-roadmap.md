---
tags: [roadmap, release, install, onboarding, integration, discord, security, website]
status: draft
date: 2026-09-06
related:
  - 2026-08-24-usage-aware-concurrency.md
  - 2026-08-30-agent-question-routing.md
  - 2026-09-03-project-onboarding-design.md
  - 2026-09-04-hierarchical-integration-trains-design.md
  - ../plans/2026-09-04-hierarchical-integration-trains-implementation.md
  - ../plans/2026-09-05-project-onboarding-service.md
  - ../../specs/design/trust-and-ops.md
  - ../../specs/setup-wizard.md
---

# Release Roadmap — Tier 1 through Ongoing

This document captures the release task list as of 2026-09-06 and grounds each
item in the current state of the repository. Each area has three parts: **Where
we are** (verified against the checkout, with file paths), **What "done" means**
(the task list, restated as outcomes), and **Open decisions** where the list
itself asks a question. Items are not yet tasks in the queue; the intent is that
this document is the source for a spec-ingest pass once the tiers are agreed.

The overall goal, stated last in the original list and repeated here because it
orders everything else: **get users**. Every Tier 1 item exists to make the
first hour of a new user's experience work, and to make the system trustworthy
enough that the operator can stop developing Agent Queue and start using it.

---

## Tier 1 — Usable release

### 1. Install & Onboarding

**Where we are**

- `setup.sh` handles Python 3.12+ discovery (apt on Linux, Homebrew on macOS),
  venv creation, `pip install -e ".[dev,cli,gemini]"`, `npm install`,
  `@aq/ts-client` generation, symlinks into `~/.local/bin`, shell completion,
  then hands off to `src/setup_wizard.py`. Windows is unsupported by the script
  (`uname -s` other than Linux/Darwin exits) and there is no WSL guidance
  anywhere in `docs/` or `README.md`; the README says "the source setup
  currently targets Linux and macOS".
- **`setup.sh` is stale relative to the runtime model.** It still installs
  `acpx` globally and explains it as "used by profiles whose `runtime` is
  `acpx`", and it installs the `gemini` extra "for the default Supervisor
  profile". Per `CLAUDE.md`, the `runtime` profile key and the `acpx` runtime
  were removed; every agent now runs as a tmux session selected by
  `## Config.harness`. The acpx block (lines ~115–141) and the comment on the
  gemini extra should go.
- The wizard (`docs/specs/setup-wizard.md`) is **Claude-first**: step 3 is
  "Agent configuration (Claude Code)", `_check_claude_cli` looks for the
  `claude` binary and `~/.claude/.credentials.json`. There is no equivalent
  check or install offer for `codex` or `gemini`, although the session runtime
  ships harness definitions for all three (`src/sessions/default_harnesses/
  {claude,codex,gemini}.md`). The README already admits this gap.
- The wizard's database step (`_select_database`, `_select_postgresql`,
  `_boot_docker_postgres`) supports SQLite, an existing Postgres DSN, or
  booting the repo's `docker-compose.yml` (postgres:18-alpine on :5533), and
  offers `scripts/migrate_sqlite_to_pg.py` when an existing SQLite file is
  found. So both backends are already installable; the decision below is about
  which one we *recommend and test as the default*.
- Default profiles are seeded write-if-absent by `vault.ensure_default_profiles`
  from `src/profiles/defaults/`, with tombstones in
  `vault/agent-types/.retired-defaults` (`src/profiles/retired_defaults.py`).
  Shipped worker ids are provider-explicit (`worker-<tier>-<level>-<provider>`),
  so "one profile per installed platform" already has a naming scheme; what is
  missing is an enable/disable state keyed on which CLIs are present.
- There is no `aq config export` or profile-export command
  (`grep` over `src/commands` and `src/cli` finds none). `src/config_editor.py`
  is a round-trip writer, not an exporter. "Ship excellent default tuning"
  therefore means: capture the operator's current `~/.agent-queue/config.yaml`
  and `vault/agent-types/*` into the repo's shipped defaults by hand or by a new
  export command, **excluding** `vault/memory`, `vault/projects/*` and anything
  with project identity in it.

**What "done" means**

1. `setup.sh` runs clean on a fresh macOS machine and a fresh Windows machine
   inside WSL2 (Ubuntu). Each run is recorded as an evidence file under
   `docs/gates/` per `trust-and-ops.md` §8. Remove the `acpx` and `runtime:`
   references first.
2. `docs/guides/getting-started.md` and `README.md` are rewritten so both a
   human and an agent can follow them. Concretely: prerequisites are a single
   checklist (Python 3.12+, git, tmux, Node/npm, `gh`, at least one agent CLI),
   every step has the exact command and the expected output, and the
   agent-facing version is one fenced block per step with no prose in between.
   The current guide still leads with "A Discord bot token" as a prerequisite,
   which contradicts the README's `messaging_platform: none` path.
3. The wizard gains a step that detects `claude`, `codex`, and `gemini` on
   PATH, offers to install each missing one (npm/brew/pip as appropriate for
   the CLI), and runs the CLI's own login flow in the foreground for any that
   are installed but not authenticated. The existing `_check_claude_cli` is the
   pattern; extend it per harness. The wizard must remain idempotent
   (`setup-wizard.md` §1).
4. Shipped defaults are refreshed from the operator's tuned install. A new
   `aq config export --defaults` (or a documented manual procedure) writes
   config and agent-type profiles into `src/profiles/defaults/` and the
   config template, stripping project memory and project-scoped content.
5. Profiles are enabled per installed platform. Ship all `worker-*-<provider>`
   profiles; on first boot (and on `aq system reload-config`) mark profiles
   whose harness binary is missing as disabled with a doctor warning naming the
   CLI to install. Do not delete them, so installing the CLI later is enough.

**Open decision: SQLite, Postgres, or both**

Current facts: both backends work and are exercised in CI (`tests.yml` runs a
`postgres-integration` suite against postgres:18 and applies migrations to
both). The team's own memory says Postgres is production and SQLite is
deprecated for perf and concurrency work. The concern in the list is real on
both sides: Postgres via Docker Desktop has licensing terms for larger
companies, and SQLite's single-writer model is a known ceiling for a fleet with
hundreds of concurrent sessions and thousands of tasks (layout tiles, metrics
samples at 1 Hz, and the integration outbox are all write-heavy).

Recommendation: **support both, default to SQLite for a first install, and
make the wizard's Postgres path work without Docker.** Offer three Postgres
routes in the wizard: an existing DSN, `docker compose` when Docker is present,
and a native install hint (`brew install postgresql@18`, `apt install
postgresql`). Document a hard guidance number once measured: run
`scripts/e2e-smoke.sh` and the layout perf seed (`scripts/seed_layout_perf.py`)
on SQLite and record where it degrades, so the docs can say "above N
concurrent agents or M tasks, move to Postgres" and point at
`scripts/migrate_sqlite_to_pg.py`. Podman is a licensing-free fallback worth
one line in the docs.

### 2. Core Reliability & Speed

**Where we are**

- Stuck detection today is `_check_stuck_defined_tasks` and
  `_notify_stuck_chain` in `src/orchestrator/core.py`, plus stall recovery in
  the session reconciler. "Getting stuck" in the user's sense (a task that sits
  waiting on a gate, a question, or a reviewer) is queryable via
  `aq task explain`, but there is no aggregate "why is throughput low" view.
- **Provider throttling** has a design but almost no implementation.
  `docs/superpowers/specs/2026-08-24-usage-aware-concurrency.md` (status:
  draft) inventories what exists: the `token_ledger` and `rate_limits` tables,
  `RateLimitWindow` in `src/tokens/tracker.py` (counts against a *configured*
  cap, not the provider's real state), the on-demand
  `_probe_claude_rate_limit`, and `_cmd_claude_usage` reading `~/.claude`
  local files. The spec's proposed `usage_samples` table does **not** exist in
  `src/database/tables.py` and no migration references it. Phases A–D
  (observe, understand, steer, multi-provider arbitrage) are all unstarted.
  No tasks for this were found in the repo; check the live queue with
  `aq task list -k throttl` before creating new ones.
- **Integration pipeline** is the active branch
  (`feat/hierarchical-integration-trains`). Design and adversarial review are
  in `docs/superpowers/specs/2026-09-04-hierarchical-integration-trains-*.md`;
  the 12-task implementation plan is in `docs/superpowers/plans/
  2026-09-04-hierarchical-integration-trains-implementation.md`. Commits show
  tasks 1–10b landed (`src/integration/` is ~13.9k lines across attestation,
  candidates, ci, hierarchy, main_promotion, outbox, ownership,
  parent_completion, promotion, repair, review_evidence, scheduler, service);
  the working tree carries uncommitted work on attestation publication
  (`migrations/versions/f0a1b2c3d4e5_attestation_publication_claims.py`, edits
  to `attestation.py`, `ci.py`, `main_promotion.py`, `.github/workflows/
  tests.yml`). Tasks 11 (operator status, controls, project cutover) and 12
  (cross-boundary failure tests, release evidence) remain.
- **Add projects** is further along than "nearly done" suggests for the
  backend and dashboard: the design is approved
  (`2026-09-03-project-onboarding-design.md`), `src/commands/
  project_onboarding_commands.py`, `src/api/models/project_onboarding.py`,
  `src/cli/projects.py`, and `dashboard/src/pages/project/onboarding/` all
  exist with tests. The follow-up plan `2026-09-05-project-onboarding-service.md`
  still has unchecked steps in Task 4 (command delegation/status tests) and
  Tasks 5–6 (`aq project onboard`, evidence). The getting-started guide already
  points at `aq project onboard`, so the CLI command must exist before the
  docs are true.
- **Skills degrading agents:** nothing in `src/sessions/` or the shipped
  harness definitions mentions skills at all. Claude Code loads the user's
  `~/.claude/skills` and project `.claude/skills` into every worker session
  today, including human-in-the-loop skills (brainstorming, plan-approval
  workflows) that block waiting for a person who is not there. `trust-and-ops.md`
  §3 already scrubs the session environment; skills are the same class of
  ambient input.

**What "done" means**

1. Throughput and stuck-rate become measurable before they are improved. Add
   two derived series to the metrics sampler (`src/metrics/sampler.py`): tasks
   completed per hour and median time a task spends in each non-running state.
   Then pick the top three stall causes from a week of real data.
2. Prove the system on a spread of project types: this repo (Python daemon +
   TS dashboard), Matter Engine (see Ongoing), one small greenfield repo, and
   one non-Python repo. Record each as a `docs/gates/` evidence file.
3. Provider throttling: implement Phases A–C of the usage-aware-concurrency
   spec. Phase A is the `usage_samples` table plus header capture off real
   responses; Phase B is the series endpoint and dashboard panel; Phase C is
   the scheduler consuming headroom to choose which provider's worker profile
   to launch next. Review the UX of the existing `aq system claude-usage`
   output before adding panels. Acceptance: a soak run that never trips a
   five-hour or weekly limit on any configured provider while balancing across
   them without violating a profile's routing policy.
4. Integration pipeline: finish plan Tasks 11–12, commit the attestation
   publication work, and run the train against Matter Engine with a realistic
   day's load. The `check-integration-attestation.py` decision job in CI is
   the release gate. Target from the design: hundreds of tasks per day without
   every leaf merging to `main`.
5. Add projects: finish plan Tasks 4–6, so the dashboard wizard and
   `aq project onboard` both produce a project with a `project-repo` workspace
   and vault structure.
6. Skills: measure first. Run the same task set with and without the
   operator's personal skills visible and compare stall rate. If they degrade
   agents, add a harness-level allowlist (`## Config.skills` or a harness
   `env` scrub of the skills path) so AQ workers see only approved skills.

### 3. Documentation

**Where we are**

- Docs are MkDocs (`mkdocs.yml`, `scripts/generate-docs.sh`), with
  `docs/specs/` as the source of truth and `docs/guides/` as the operator
  layer. Several guides are visibly out of date: `getting-started.md` treats
  Discord as required, `docs/specs/database.md` §1 still describes a single
  `aiosqlite` connection with no pooling, and `docs/guides/discord-commands.md`
  documents a deprecated hooks section and chat-only file/shell capabilities.

**What "done" means**

- One sweep, tracked as a checklist per file under `docs/guides/` and
  `docs/specs/`, that marks each as current, rewritten, or deleted. The
  invariant tests in `trust-and-ops.md` §6 (docs-sync tests) should be
  extended to cover anything the sweep decides must stay true, so docs do not
  rot again before the next release.

### 4. Discord Simplification

**Where we are**

- `src/discord/` is about 6,200 lines: `bot.py`, `slash_commands.py`,
  `views.py`, `embeds.py`, `gate_view.py`, `notification_handler.py`,
  `notifications.py`, `agent_questions.py`, `rate_guard.py`, `adapter.py`.
  The commands guide lists slash-command coverage for projects, tasks, agents,
  workspaces, git, file browsing, playbooks, hooks, notes, memory, and system
  control. The dashboard now covers all of that.
- The escalation path the list describes already has an approved design:
  `2026-08-30-agent-question-routing.md` (supervisor-first, five-minute
  timeout to human, Discord question card with a Reply button, durable
  answers delivered back to the original session). `notification_handler.py`
  already handles `notify.task_blocked` with a `TaskBlockedView`. So the
  Discord plugin's two remaining roles are already partially built; the work
  is removal, not addition.

**What "done" means**

1. Inventory: a table of every slash command and view in `src/discord/` with a
   keep/remove verdict. Keep: the configurable notification stream
   (`notifications.py`, `notification_handler.py`, `embeds.py`) and the
   question/blocked escalation cards (`agent_questions.py`, `gate_view.py`,
   the blocked view). Remove: task/project/agent/git/file/memory slash
   commands and the chat-only file and shell operations.
2. The escalation flow is verified end-to-end against the question-routing
   spec: blocked task → supervisor → Discord card → user reply → supervisor
   unblocks the task. Add it to `scripts/e2e-smoke.sh` with a fake Discord
   transport.
3. A community Discord server exists, with an invite in the README and on the
   website. This is an operator action, not code.

### 5. Playbook Confidence

**Where we are**

- Playbook V2 already has more inspection surface than the list assumes:
  `aq playbook health`, `_cmd_inspect_playbook_run` in
  `src/commands/playbook_commands.py`, `src/playbooks/explanation.py` (pure,
  contract-derived explanations of each node), `receipts.py`,
  `run_overlay.py`, and the dashboard run inspector pane
  (`2026-08-22-pane-playbook-run-inspector-design.md`). What does not exist is
  any *judgement* of whether a run did the right thing, or a step-through
  debugger for a decision node.

**What "done" means**

- A brainstorming session, output as a short design note, that picks between
  (a) an offline "run audit" playbook that reads receipts and explanations for
  the last N runs and reports anomalies, (b) a replay/step mode for the engine
  that pauses at each LLM or transition node and shows inputs and outputs, and
  (c) metrics only. The note should decide which to build for the release.

### 6. Security Plan

**Where we are**

- `docs/specs/design/trust-and-ops.md` already states the trust model
  (operator code trusted, all data untrusted, trust follows authorship), the
  session environment scrub, the skip-permissions posture inside worktrees,
  and a current-state audit dated 2026-08-19. It does not enumerate external
  ingress vectors one by one.
- Known ingress points in the code: Discord messages and replies
  (`src/discord/`), the Gmail/inbox extra (`inbox` in `pyproject.toml`),
  GitHub issues and PR comments read through `gh` (`src/git/`,
  `src/orchestrator/pr_polling.py`), the dashboard and REST API
  (`src/api/`, with `ApiAuthConfig.require_session_token` still a reserved
  hook defaulting to `False`), MCP clients, and agent-authored task
  comments and questions (already marked untrusted in the question-routing
  spec).

**What "done" means**

- A written plan, not code: extend `trust-and-ops.md` with a §2.6 vector table
  (source, what text reaches an LLM, who can write it, current mitigation,
  gap) and a prioritised list of mitigations. Thinking only for this release.

---

## Tier 2 — Wider release

### 7. Website

**Where we are**

- No website exists in the repo. The README already positions against Gas
  City ("a local software factory", independent implementation) and names the
  differentiators: durable work records, capability-based routing across
  Claude Code, Codex, and Gemini CLI, isolated worktrees, review in the graph,
  humans steering via the Command Center.

**What "done" means**

1. Analyse Gas City's site and write the information architecture and copy
   first, in `docs/website/` as markdown. Positioning to nail: ease of setup,
   multi-agent 24-hour software factory, well tuned out of the box,
   customisable.
2. Only then build: a dynamic one-page site (Three.js), with optional 3D
   scenes rendered from Blender via its CLI.
3. Decide on a blog. Recommendation: no blog at launch; publish the install
   tutorial and one architecture post as pages, and add a blog when there is
   a second author.
4. Host Outrider's information on the same site.

### 8. Videos

**What "done" means**

1. A ranked list of demo-worthy features, drawn from the README's "How work
   moves" section and the Command Center panes.
2. Bullet-point scripts for each.
3. The install tutorial video, recorded against the finished Tier 1 install on
   a clean machine. Highest priority.
4. Decide long-form-first versus short-form-first. Recommendation: record one
   long-form install walkthrough and cut clips from it, since the clips need
   the same clean install anyway. YouTube and social strategy is help the
   operator has asked for and is out of scope for this document.

### 9. Planning & Ingestion

**Where we are**

- Spec ingest exists: the default pipeline hands approved specs to a
  spec-ingest agent that proposes a task batch for human approval (README,
  `src/playbooks/proposal.py`, `aq task create --from-spec`). The supervisor
  design (`docs/specs/design/supervisor-agent.md` §8–9) describes the planner
  flow replacing `break_plan_into_tasks()`. Epics are containers in the task
  hierarchy (`hierarchy_queries.py`, `settle_containers`).

**What "done" means**

- A dedicated planning agent profile plus a playbook that turns a planning
  document into a full task graph up front, with epic checkboxes in the
  document driving order. This document is the first input to that process.

---

## Tier 3 — Later

### 10. Dashboard Mobile & Remote Access

**Where we are**

- The dashboard is a Vite/React app under `dashboard/`; nothing in it is
  mobile-specific. Remote access today is loopback-only with
  `api_auth.require_session_token` reserved but not enforced, and no Tailscale
  guidance exists in `docs/`.

**What "done" means**

1. A simplified mobile task view, the sidebar fix, and agent consoles usable
   on a phone.
2. Document the Tailscale approach first (daemon bound to the tailnet address,
   dashboard origin added to `trusted_dashboard_origins`). Add real
   authentication for LAN access afterwards by finishing
   `require_session_token`.

### 11. Outrider Integration

**Where we are**

- Outrider is not referenced anywhere in this repository.

**What "done" means**

- Outrider is a standalone app that works with any agent and auto-plugs into
  Agent Queue through the plugin system (`src/plugins/`,
  `docs/specs/plugin-system.md`). Videos for Outrider follow the same plan as
  §8.

---

## Ongoing

- **Matter Engine:** add it as a project via the finished onboarding flow and
  resume its tasks. It is also the load test for the integration train (§2.4)
  and one of the project types in §2.2. Matter Engine is not referenced in
  this repo today.
- **Shift from developing to using:** once Tier 1 lands, Agent Queue
  development itself should run through Agent Queue with the operator tuning
  profiles and playbooks rather than writing code. The reflection loop and
  `aq costs` are the feedback channels.
- **More CLIs:** Gemini CLI has a shipped harness but no wizard support or
  soak evidence; a local-model CLI has none. The third CLI in the list
  ("rock CLI") could not be identified from the transcript; confirm the name
  before creating a task.
- **Get users:** the number one goal. Every item above is ordered by how much
  it shortens a new user's path to a first merged PR.

---

## Discrepancies found while grounding this list

These are places where the repo and the task list, or two parts of the repo,
disagree. Each should become a task or be resolved in the sweep.

1. `setup.sh` still installs `acpx` and references `runtime: acpx` profiles;
   both were removed from the codebase.
2. `docs/guides/getting-started.md` lists a Discord bot token as a
   prerequisite; the README documents `messaging_platform: none`.
3. `docs/guides/getting-started.md` references `aq project onboard`, which the
   onboarding-service plan has not yet delivered.
4. `docs/specs/database.md` §1 describes a single `aiosqlite` connection and
   no pooling; the adapter layer supports PostgreSQL via asyncpg.
5. The usage-aware-concurrency spec's `usage_samples` table is described as
   the Phase A deliverable but has no table, migration, or code.
6. `api_auth.require_session_token` is documented as a reserved hook and
   defaults to off; remote access (§10) depends on it.
