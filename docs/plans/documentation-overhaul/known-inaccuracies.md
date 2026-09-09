# Known inaccuracies in existing documentation

Recorded by the `foundation` ticket against implementation `HEAD`. Each entry
names a page, the claim it makes, what the code on `main` actually does, and
the evidence for that. This is a **ledger, not a fix list**: foundation does
not edit pages other tickets own. The `legacy` ticket resolves these while
recording each file's disposition, and the pages that replace them must not
reintroduce the claim — see
[documentation style](../../contributing/documentation-style.md).

Line numbers are as of the commit this ledger was written against; treat them
as pointers, not identifiers.

## 1. Automatic reviewer creation

| | |
|---|---|
| **Claim** | `README.md:42-45` — "The default pipeline creates a read-only reviewer for each completed task with a branch, gates downstream work on that verdict, and coalesces the branch into a final review." |
| **Also** | `README.md:104-106` — describes `default-pipeline.md` as dispatching "routing, review, and spec-ingest actions". |
| **Actually** | The shipped default pipeline ships three rules — spec ingest on `spec.approved`, a human gate on `proposal.ready`, and batch commit on `gate.resolved`. It states in its own text that it "does not create per-task reviewers, final branch reviewers, or review/PR gates on downstream work". Routing is a separate playbook, not part of this pipeline. |
| **Evidence** | [`src/prompts/default_playbooks/default-pipeline.md`](../../../src/prompts/default_playbooks/default-pipeline.md) lines 15-23. |
| **Owner** | `readme` for `README.md`; `playbooks` for the concept page; `legacy` for the disposition. |

The `reviewer` and `final-reviewer` profiles still ship
([`src/profiles/defaults/`](../../../src/profiles/defaults/)), and
`final-reviewer` remains the only shipped profile with merge authority. A
review stage is therefore something a project *can* run, not something the
default configuration produces. Documentation must say which.

## 2. Triage-task routing workflow

| | |
|---|---|
| **Claim** | `README.md:28-30` — "An unrouted task receives a routing gate. The default pipeline coalesces open gates into a reusable triage task, and triage pins a profile, intelligence class, and workspace requirement." |
| **Actually** | The orchestrator emits `task.route_needed` for a task missing an intelligence class or profile and decides nothing else. The shipped routing playbook calls `task_route_options`, asks the `playbook-compiler` profile to choose, and calls `task_route`. No triage task is created. |
| **Evidence** | [`src/prompts/default_playbooks/default-assignment-routing.md`](../../../src/prompts/default_playbooks/default-assignment-routing.md) lines 17-56. |
| **Nuance** | The triage shape is still *supported* as a compatibility path: [`uses_default_triage`](../../../src/playbooks/routing.py) returns true only when the active routing artifact ensures a task with `profile_id: triage` and `dedup_key: triage-open`, and [`src/orchestrator/triage.py`](../../../src/orchestrator/triage.py) reconciles those. Document it as an optional routing-playbook shape a project may adopt, never as the default. |
| **Owner** | `readme`, `routing`, `playbooks`. |

## 3. Discord as a control surface

| | |
|---|---|
| **Claim** | `docs/guides/getting-started.md:10` lists "A Discord bot token" as an installation prerequisite, and line 30 describes the config file as holding "your Discord bot token, guild ID, and project settings" as though they were required; line 116 offers "a complete reference of all available commands" behind a link to the Discord guide. |
| **Actually** | Discord is notification-only: one configured channel, a periodic activity digest and one thread per escalation. There are no AQ slash commands, task controls, gate buttons or general chat. `messaging_platform: none` is a supported configuration, so a bot token is not a prerequisite for running AQ. |
| **Evidence** | [`docs/guides/discord-commands.md`](../../guides/discord-commands.md) lines 5-16 (already correct); `messaging_platform` default and accepted values in [`src/config.py`](../../../src/config.py) around line 2403. |
| **Nuance** | `README.md:139` calls Discord "the supported chat transport" — the configuration key is real but "chat" overstates what the surface does. |
| **Owner** | `quickstart` replaces `getting-started.md`; `communications` owns the current statement; `readme` owns the root wording. |

## 4. Obsolete runtime / platform development instructions

| | |
|---|---|
| **Claim** | `docs/guides/runtime-development.md` and `docs/guides/runtime-development-guide.md` both describe adding an in-process "platform"/"runtime" backend that implements `start()` / `wait()` / `is_alive()` / `stop()` and is called by the orchestrator. |
| **Actually** | There are no in-tree runtime implementations. Every agent runs as an external CLI inside a tmux session, and a profile's `harness` field (`claude`, `codex`, `gemini`) is the only selector. [`src/runtimes/`](../../../src/runtimes/) keeps the `Runtime` ABC and a registry that registers nothing by default; it is an injection seam for tests and `sync_workflow`, not evidence that runtimes ship. |
| **Evidence** | [`src/runtimes/base.py`](../../../src/runtimes/base.py) and [`src/runtimes/__init__.py`](../../../src/runtimes/__init__.py); shipped harnesses in [`src/sessions/default_harnesses/`](../../../src/sessions/default_harnesses/). |
| **Extra** | These two pages are near-duplicates of each other, and `runtime-development-guide.md:3` links `[[platform-development]]`, a page that does not exist. |
| **Owner** | `architecture` states the current position; `legacy` dispositions both pages. |

## 5. Obsolete migration instructions

| | |
|---|---|
| **Claim** | `docs/guides/migrations.md:6-9` — "All migrations run in `Database.initialize()` on every startup… This document catalogs every migration and its removal criteria." |
| **Actually** | Schema changes are Alembic revisions under `migrations/versions/`, which is a squashed baseline plus a small number of revisions — not the long inventory the page's framing implies. The page's *second* half, on who may run migrations against which database, is current and correct. |
| **Evidence** | [`migrations/versions/`](../../../migrations/versions/); [`src/database/migration_guard.py`](../../../src/database/migration_guard.py). |
| **Owner** | `database`. Keep the scope/guard material; replace the technical-debt-inventory framing. |

## 6. SQLite described as the storage backend

| | |
|---|---|
| **Claim** | `docs/index.md:71` — "SQLite-backed state"; `docs/index.md:84` — "One Python process, SQLite… PostgreSQL supported for production deployments". |
| **Actually** | PostgreSQL is the only backend. There are no `dialect.name` branches and no `batch_alter_table`; a test guards the removal. |
| **Evidence** | `tests/test_sqlite_removal.py`; [`src/database/adapters/`](../../../src/database/adapters/) contains only `postgresql.py`. |
| **Owner** | `database` for the statement; `legacy` for `docs/index.md`'s disposition. |

## 7. Structural problems, not claims

These are not false statements, but they block the overhaul's publication
format or its coverage guarantee.

| Finding | Detail | Owner |
|---|---|---|
| Wiki links do not render on GitHub | 869 `[[…]]` links across 89 Markdown files render as literal brackets in the GitHub file browser (measured over the pages that existed before this overhaul; the two further mentions in the overhaul's own pages are quoted examples inside code spans). Worst offenders: `docs/specs/design/roadmap.md` (275), `docs/specs/design/README.md` (43), `docs/guides/architecture.md` (19), `docs/index.md` (14). | `legacy`, per page |
| Broken relative links | 552 across four files. 549 are one off-by-one prefix: `docs/reports/integration-safeguards-2026-09-09/SOURCE-INDEX.md` links source as `../../../../src/…` from a directory three levels below the repository root. The other three are single dead links in `docs/superpowers/plans/2026-04-25-platforms-implementation.md`, `docs/superpowers/specs/2026-04-27-runtime-rename-and-acp-design.md` and `notes/github-actions-integration.md`. | `legacy` |
| Duplicate pages | `docs/guides/runtime-development.md` / `runtime-development-guide.md` (see §4). `docs/index.md` and `README.md` both act as a landing page, with different feature lists. | `legacy`, `readme` |
| Two publication formats | `mkdocs.yml` configures a Material site at `electricjack.github.io/agent-queue/`, while this overhaul publishes GitHub-rendered Markdown with relative links. Whether the MkDocs build is still produced needs a recorded decision either way. | `legacy` |
| Stale count in `CLAUDE.md` | `CLAUDE.md:144` says "4 internal plugins (files, git, notes, vibecop)". Discovery is dynamic and five internal plugins are found: `files`, `git`, `notes`, `vibecop` and the `inbox` package. | `contributing` |
| CLI inventory page has no forward link | `docs/reference/cli-command-inventory.md` is accurate but predates the CLI reference. It explains the JSON artifact and its acceptance statuses and stops there, so a reader who lands on it never reaches [`docs/reference/cli/`](../../reference/cli/README.md), which is the prose for the same surface. The new pages link *to* the artifact; the link back is missing and the page is `legacy`-owned. | `legacy` |

## 8. Shipped skill describes reviewer creation as the default pipeline

| | |
|---|---|
| **Claim** | `src/skills/aq-playbooks-and-gates/SKILL.md:84-95` — under the heading "Default pipeline (shipped)" it lists `task.created` → `task_route`, `task.completed` with `branch_name` → "create a per-task review task under the `reviewer` profile", and `task.completed` with `branch_name AND pr_url` → "create a per-branch final-review task under the `final-reviewer` profile". The skill's own `description:` frontmatter repeats it ("how the default pipeline routes task events into review + final-review + spec-ingest flows"). |
| **Actually** | The shipped default pipeline ships three rules — spec ingest, a proposal human gate, and batch commit on that gate resolving — and says in its own text that it "does not create per-task reviewers, final branch reviewers, or review/PR gates on downstream work". Routing is a separate playbook. This is entry 1 of this ledger, reproduced inside shipped *prompt* content rather than a documentation page. |
| **Evidence** | [`src/prompts/default_playbooks/default-pipeline.md`](../../../src/prompts/default_playbooks/default-pipeline.md) lines 15-23 against [`src/skills/aq-playbooks-and-gates/SKILL.md`](../../../src/skills/aq-playbooks-and-gates/SKILL.md) lines 81-95. |
| **Note** | This is shipped text that reaches an agent's context, so correcting it changes agent behaviour and belongs in a code change, not a documentation ticket. Skill installation is write-if-absent, so an already-installed copy also needs `aq doctor`'s `skills.installed_drift` check to notice. |
| **Owner** | `playbooks` for the wording; whoever fixes the skill file. Recorded by `cli`, which owns the shipped-skill coverage rows. |

## 9. Shipped skill describes a removed inbox hook

| | |
|---|---|
| **Claim** | `src/skills/aq-comms/SKILL.md:23-27` — "The `aq inbox --inject` hook the Claude harness runs at every prompt boundary rendering pending messages inline — you don't usually need to poll `aq message inbox` manually", repeated at lines 122-124. |
| **Actually** | The `UserPromptSubmit` hook was removed on 2026-08-27. The shipped Claude hook file wires `SessionStart` (matcher `resume\|compact`), `PreCompact` and the two sub-agent events, and nothing else. `aq inbox --inject` is still a supported command; it is simply not wired into a hook. Pending messages reach a session at the next prime or through the cascade's nudge when the session goes idle. |
| **Evidence** | [`src/prime/templates/hooks/claude.json`](../../../src/prime/templates/hooks/claude.json); the removal note in [`src/sessions/default_harnesses/claude.md`](../../../src/sessions/default_harnesses/claude.md) lines 186-190. |
| **Note** | `CLAUDE.md`'s "Messages" bullet carries the same stale claim ("`aq inbox --inject` hook in claude harness"). |
| **Owner** | `communications` for the messaging page; whoever fixes the skill file and `CLAUDE.md`. Recorded by `cli`. |

## How to use this ledger

* **Do not fix a page you do not own.** Add findings here instead; the map
  says who owns what.
* **Add to it as you go.** A subsystem ticket that finds its area's existing
  page wrong appends a numbered entry with the same four fields: claim,
  actual behaviour, evidence, owner.
* **Cite the code, not this file.** When you write the replacement page, link
  the module you verified against, so the next reader can check you without
  trusting a ledger entry.
