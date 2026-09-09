# `aq` command groups

Every `aq` command, group by group, with the daemon command behind it. This
page answers *"which command do I want, and what is this group for"*. For the
rules every one of them obeys — global options, the JSON envelope, exit codes,
who may run what — read [the CLI contract](README.md) first.

## How to read the tables

| Column | Meaning |
|---|---|
| **Command** | The leaf as you type it. |
| **Daemon command** | The `CommandHandler` command it dispatches, which is also its name over REST, MCP and in a playbook step. `—` means the leaf is hand-written and either does local work (`aq start`, `aq logs`) or composes more than one call. |
| **Kind** | `gen` — generated from a tool definition, so every argument is a `--kebab-case` option and there are no positional arguments. `hand` — hand-written, so it may take positional ids, prompt, or pass argv to a child program. |
| **What it does** | The command's own `--help` summary, trimmed. |

Three habits make this page much shorter in practice:

```bash
aq <group> --help              # the commands in one group
aq <group> <command> --help    # the current options, always authoritative
aq --help-all                  # every command's full help, recursively
```

`--help` is generated from the same schema the command dispatches with, so it
can never disagree with the daemon. When a table here and `--help` differ,
`--help` is right and this page needs a fix.

> **Not shown here.** Exact parameter lists, aliases, deprecation status and
> per-leaf test evidence live in the machine-readable
> [command inventory](../cli-command-inventory.json), regenerated from the live
> Click tree by `python scripts/generate-cli-command-inventory.py`.

## Top-level commands

| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq chat` | `—` | hand | Talk to a project's supervisor session. |
| `aq costs` | `get_costs` | hand | Show token spend rolled up into USD. |
| `aq doctor` | `doctor` | hand | Check whether this install is healthy, and what to do about it. |
| `aq handoff` | `task_handoff` | hand | Record a handoff note; request a session restart unless ``--auto`` (design §6.1). |
| `aq inbox` | `message_inbox` | hand | Show pending messages (alias for ``aq message inbox``). |
| `aq logs` | `—` | hand | Tail and filter daemon logs. |
| `aq prime` | `prime` | hand | Print this task's startup prime document (design §5). |
| `aq reply` | `—` | hand | Reply to a message (alias for `aq message reply`). |
| `aq restart` | `—` | hand | Restart the agent-queue daemon. |
| `aq schema` | `—` | hand | Print the system's enum catalog (task statuses, types, dependency types, gate types/statuses, ...) so scripts and agents never guess magic strings. |
| `aq start` | `—` | hand | Start the agent-queue daemon. |
| `aq status` | `get_status` | hand | Show system status overview. |
| `aq stop` | `—` | hand | Stop the agent-queue daemon and its agent sessions. |
| `aq test` | `—` | hand | Run pytest under the box-wide test semaphore. |

### `aq agent`

Agents and agent profiles. An **agent** is a durable worker row the daemon
can start a session for; a **profile** is the markdown definition — role,
rules, capabilities, harness, intelligence class — that decides how it
behaves. Profiles are global: one definition per agent type, shared across
projects, with `vault/agent-types/<id>/profile.md` as the source of truth.
Creating, editing and deleting global agents requires the global admin scope.
`aq agent message` is the only hand-written leaf here: it delivers guidance to
a *live* worker and reports the delivery status.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq agent check-profile` | `check_profile` | gen | Validate an agent profile's install dependencies. |
| `aq agent create` | `create_agent` | gen | Define a shared worker without launching it. |
| `aq agent create-profile` | `create_profile` | gen | Create a new agent profile. |
| `aq agent delete` | `delete_agent` | gen | Delete an idle shared worker from the flock while preserving task and session history. |
| `aq agent delete-profile` | `delete_profile` | gen | Delete an agent profile. |
| `aq agent edit` | `edit_agent` | gen | Edit an individual global worker. |
| `aq agent edit-profile` | `edit_profile` | gen | Edit an existing agent profile's properties. |
| `aq agent export-profile` | `export_profile` | gen | Export an agent profile as YAML. |
| `aq agent get` | `get_agent` | gen | Get a globally defined agent, current session, task, and individual settings. |
| `aq agent get-error` | `get_agent_error` | gen | Get the last error recorded for a task, including error classification, suggested fix, and agent summary. |
| `aq agent get-profile` | `get_profile` | gen | Get details of a specific agent profile. |
| `aq agent import-profile` | `import_profile` | gen | Import an agent profile from YAML text or a GitHub gist URL. |
| `aq agent install-profile` | `install_profile` | gen | Install missing npm/pip dependencies for a profile's install manifest. |
| `aq agent list` | `list_agents` | gen | List globally defined shared agents, including the supervisor. |
| `aq agent list-available-tools` | `list_available_tools` | gen | Discover available Claude Code tools and well-known MCP servers for use in agent profiles. |
| `aq agent list-profiles` | `list_profiles` | gen | List all agent profiles. |
| `aq agent message` | `agent_message` | hand | Send BODY to a live task, agent, or session. |
| `aq agent profile-audit` | `profile_audit` | gen | Report which agent profiles still derive their capabilities from the legacy allowed_tools list rather than an explicit ## Capabilities block. |
| `aq agent profile-drift` | `profile_drift` | gen | Report which vault system profiles have drifted from the defaults shipped in src/profiles/defaults/. |
| `aq agent profile-reseed` | `profile_reseed` | gen | Overwrite one vault system profile with the version shipped in src/profiles/defaults/, keeping a .bak-<epoch> copy of the old file. |
| `aq agent show-effective-profile` | `show_effective_profile` | gen | Run the orchestrator's profile resolution cascade for a (project_id, agent_type) pair and return the merged profile the next task launch would use. |
| `aq agent start-terminal` | `start_agent_terminal` | gen | Explicitly start or resume one agent's interactive terminal, without creating a task or sending a chat message. |

### `aq db`

The operator's migration door, and nothing else. `aq db current` is the
read-only "am I behind?" answer and is always safe to run. `aq db upgrade`
runs Alembic against the configured database and is **daemon-host only** — a
worker inside a worktree slot is refused by
[`src/database/migration_guard.py`](../../../src/database/migration_guard.py)
and must report the refusal rather than upgrade around it. The whole group
refuses `--json`: it owns interactive safeguards and multi-step progress. See
[migrations](../../guides/migrations.md).


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq db current` | `—` | hand | Show the stamped revision(s) and this checkout's head. |
| `aq db import-sqlite` | `—` | hand | Copy a pre-PostgreSQL SQLite database into the configured PostgreSQL one. |
| `aq db upgrade` | `—` | hand | Run Alembic migrations against the configured database (operator only). |

### `aq digest`

The hourly activity digest that AQ posts to its one configured Discord
channel. Both leaves are read-only: `preview` renders the message the current
window *would* send (or the reason it would stay silent) and writes nothing;
`status` reports the configured destination, the schedule generation, the next
evaluation and delivery health. Sending is done by the daemon, never by these
commands.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq digest preview` | `digest_preview` | gen | Dry-run the current hourly digest window: the message that would be sent, or the reason it would stay silent. |
| `aq digest status` | `digest_status` | gen | Configured digest destination and schedule generation, next evaluation, recent windows and pending/unknown/failed delivery health. |

### `aq discord`

Explicit housekeeping on the configured shared channel. This is not a
control surface: AQ's Discord integration is notification-only. `purge-channel`
deletes historical messages and is the only Discord mutation the CLI exposes.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq discord purge-channel` | `discord_purge_channel` | gen | Delete messages from a Discord channel. |

### `aq escalation`

Durable human escalations — the record of a decision a human owes the
system, and the answer coming back. An escalation is created by a supervisor,
delivered as one thread, replied to by a human, and applied through the
service that owns the original question or gate. Identity is durable: a replay,
a reconnect or a restart never produces a second post. `escalation_reply` is
also the one command the inbound Discord path may call, and it is called with
a server-derived principal — no field in the message body is trusted.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq escalation apply-reply` | `escalation_apply_reply` | gen | Apply one bound verified human reply through the owning supervisor's exact question, human-gate, or task-recovery service. |
| `aq escalation create` | `escalation_create` | gen | Create or reuse a project-scoped human escalation by durable source incident. |
| `aq escalation get` | `escalation_get` | gen | Get one visible escalation with immutable messages, deliveries, and actions. |
| `aq escalation list` | `escalation_list` | gen | List visible escalations with current external-delivery status. |
| `aq escalation reply` | `escalation_reply` | gen | Append an authenticated human reply and atomically enqueue its owning supervisor. |
| `aq escalation update` | `escalation_update` | gen | CAS-update an owned escalation, including explicit terminal resolution. |

### `aq file`

Filesystem operations inside a project workspace, provided by the in-tree
`aq-files` plugin rather than the core handler. They exist so a session or a
playbook can read and write project files through the same command surface as
everything else. A worker with its own harness file tools has no reason to
prefer these.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq file count-project-memory` | `count_project_memory_files` | gen | Count files in a subdirectory of a project's system memory vault (``{data_dir}/vault/projects/<project_id>/memory/<path>``). |
| `aq file edit` | `edit_file` | gen | Perform targeted string replacement in a file. |
| `aq file glob` | `glob_files` | gen | Find files matching a glob pattern (e.g. |
| `aq file grep` | `grep` | gen | Search file contents using regex patterns (ripgrep-style). |
| `aq file list-directory` | `list_directory` | gen | List files and directories at a given path within a project workspace. |
| `aq file read` | `read_file` | gen | Read a file's contents. |
| `aq file read-project-memory` | `read_project_memory_file` | gen | Read a markdown file from a project's system memory vault. |
| `aq file record-inspection` | `record_file_inspection` | gen | Record that a file has been inspected by the codebase-inspector (or similar) workflow. |
| `aq file search` | `search_files` | gen | Search for files or content in a workspace. |
| `aq file select-for-inspection` | `select_files_for_inspection` | gen | Select a random sample of files from a project workspace for codebase inspection, using a weighted distribution across categories (source, specs, t… |
| `aq file write` | `write_file` | gen | Write content to a file. |

### `aq formula`

Reusable task-graph templates. A formula is markdown with an `aq-graph`
block under `vault/[projects/<pid>/]formulas/<name>.md`; project formulas
shadow system ones of the same name. `show` resolves the `extends` chain,
substitutes `--var k=v` values and validates — read-only, never writes. `cook`
does the same and then creates the resulting graph in one transaction.
`show` and `cook` are hand-written because Click has no built-in for a
repeatable `--var k=v`.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq formula cook` | `formula_cook` | hand | Resolve a formula, validate it, and create the resulting task graph in one transaction. |
| `aq formula list` | `formula_list` | gen | List formulas (reusable task-graph templates) visible to a project — system formulas plus any project overrides, with project entries shadowing sys… |
| `aq formula show` | `formula_show` | hand | Resolve a formula's extends chain, substitute its vars, and validate — read-only, never writes. |

### `aq git`

Git operations against a project's workspaces, provided mostly by the
in-tree `aq-git` plugin. A worker's authority here is deliberately narrow: a
session token may read status and diffs and may push or open a PR **for its own
task's branch only**, verified against persisted state
([`src/api/scope.py`](../../../src/api/scope.py)). `git merge` and `pr-merge`
are not worker capabilities — merging belongs to the reviewing authority. Five
leaves (`checkout-branch`, `commit-changes`, `create-branch`, `merge-branch`,
`push-branch`) are alias spellings of a neighbour and say so in their help.
`ci-baseline-status` is core, not plugin: it reads the CI verdict for a
project's default branch head.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq git branch` | `git_branch` | gen | List branches or create a new branch. |
| `aq git changed-files` | `git_changed_files` | gen | List files changed compared to a base branch. |
| `aq git checkout` | `git_checkout` | gen | Switch to an existing branch. |
| `aq git checkout-branch` | `checkout_branch` | gen | Check out an existing branch (alias for git_checkout). |
| `aq git ci-baseline-status` | `ci_baseline_status` | gen | Read the CI verdict for a project's default branch head (or ``ref``): green / red / pending / unknown, the failing checks and pytest node ids, and… |
| `aq git commit` | `git_commit` | gen | Stage all changes and create a commit. |
| `aq git commit-changes` | `commit_changes` | gen | Stage all changes and commit (alias for git_commit). |
| `aq git create-branch` | `create_branch` | gen | Create and switch to a new branch (alias for git_create_branch). |
| `aq git create-github-repo` | `create_github_repo` | gen | Create a new GitHub repository via the gh CLI. |
| `aq git create-pr` | `git_create_pr` | gen | Create a GitHub pull request using the gh CLI. |
| `aq git diff` | `git_diff` | gen | Show diff of the working tree or against a base branch. |
| `aq git generate-readme` | `generate_readme` | gen | Generate a README.md from project metadata and commit it. |
| `aq git get-status` | `get_git_status` | gen | Get git status for all workspaces in a project. |
| `aq git log` | `git_log` | gen | Show recent commit log. |
| `aq git merge` | `git_merge` | gen | Merge a branch into the default branch. |
| `aq git merge-branch` | `merge_branch` | gen | Merge a branch into the default branch (alias for git_merge). |
| `aq git pr-merge` | `pr_merge` | gen | Merge a GitHub pull request via ``gh pr merge``. |
| `aq git pull` | `git_pull` | gen | Pull (fetch + merge) from the remote origin. |
| `aq git push` | `git_push` | gen | Push a branch to the remote origin. |
| `aq git push-branch` | `push_branch` | gen | Push the current or specified branch to origin (alias for git_push). |
| `aq git remote-url` | `git_remote_url` | gen | Get the git remote URL (e.g. |

### `aq graph`

Server-side spatial layout for the task graph the dashboard draws. Both
leaves are synchronous jobs: `layout-rebuild` recomputes a project's layout,
`tidy` enqueues a tidy pass. The layout is published atomically and the
dashboard reads it; nothing here changes tasks.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq graph layout-rebuild` | `graph_layout_rebuild` | gen | Rebuild the server-side task graph layout for a project (both variants) synchronously. |
| `aq graph tidy` | `graph_tidy` | gen | Enqueue a Tidy layout job for a project. |

### `aq integration`

Inspect and control hierarchical integration trains — how finished task
branches are validated and published. `status` is the read every diagnosis
starts from: rollout mode, readiness, active work and cleanup for one project.
Every other leaf is a control verb, and control verbs are **local operator
only**: a session token is refused with `out of scope: integration control
requires local operator`. The one exception is
`resolve-candidate-member`, which a live repair session may run for the
candidate it was assigned. All leaves are hand-written because they take
positional identities and explicit compare-and-set fences (`enable` takes the
generation `status` reported, so a stale operator cannot flip a mode that has
moved).


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq integration abort` | `—` | hand | Abort a safe, human-required integration OPERATION_ID. |
| `aq integration adopt` | `—` | hand | Record already-delivered work without replaying old repair checkpoints. |
| `aq integration cancel-preserving` | `—` | hand | Cancel obsolete repair scheduling while retaining refs and attached workspaces. |
| `aq integration develop` | `—` | hand | Use automatic development batches with explicit local validation. |
| `aq integration enable` | `—` | hand | CAS PROJECT_ID to MODE using the generation reported by status. |
| `aq integration flush` | `—` | hand | Request an immediate eligibility pass or train sweep for PROJECT_ID. |
| `aq integration reconcile-unmaterialized` | `—` | hand | Bind safe pre-rollout tasks and reserve their hierarchy origins. |
| `aq integration recover-candidate-member` | `—` | hand | Resolve one pushed frozen candidate-member repair reservation. |
| `aq integration resolve-candidate-member` | `—` | hand | Resolve the candidate member assigned to this repair session. |
| `aq integration resume` | `—` | hand | Resume a safe, human-required integration OPERATION_ID. |
| `aq integration retry-cleanup` | `—` | hand | Requeue the exact safe cleanup items for BATCH_ID. |
| `aq integration status` | `—` | hand | Show rollout, readiness, active work, and cleanup for PROJECT_ID. |
| `aq integration sweep` | `—` | hand | Build and publish a development batch now. |
| `aq integration waive-history` | `—` | hand | Waive only the exact historical blockers reported for PROJECT_ID. |

### `aq mcp`

The MCP server registry and its probed tool catalog. Registry entries are
markdown under `vault/[projects/<pid>/]mcp-servers/*.md` — these commands read
and write those files, and profiles reference servers by name. `probe-server`
re-probes one server and overwrites its cached tool catalog entry.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq mcp create-server` | `create_mcp_server` | gen | Add an MCP server registry entry by writing its vault markdown. |
| `aq mcp delete-server` | `delete_mcp_server` | gen | Remove an MCP server's vault file and registry/catalog entry. |
| `aq mcp edit-server` | `edit_mcp_server` | gen | Update fields on an existing MCP server registry entry. |
| `aq mcp get-server` | `get_mcp_server` | gen | Return one MCP server's full config (for editing). |
| `aq mcp list-servers` | `list_mcp_servers` | gen | List MCP servers visible to a scope. |
| `aq mcp list-tool-catalog` | `list_mcp_tool_catalog` | gen | Return the cached tool list for one or more servers. |
| `aq mcp probe-server` | `probe_mcp_server` | gen | Re-probe a single MCP server and overwrite its tool catalog entry. |

### `aq memory`

Semantic memory, provided by the external `aq-memory` plugin. Both leaves
are advertised by the core surface but implemented by that plugin, so they are
present in the command tree whether or not the plugin is installed. When the
memory subsystem is paused they return a `paused` payload and exit `0`, so an
agent loop does not fail on them.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq memory save` | `memory_save` | gen | Save a fact to memory. |
| `aq memory search` | `memory_search` | gen | Search memory. |

### `aq message`

The inter-agent and user message queue. A message is addressed to a *kind*
and an *id* — a session, a task, a profile or a user — and delivery policy
differs per kind. `aq message send --to user:dashboard` is the canonical way a
worker reports a blocker to a human. Every leaf is hand-written so that all of
them route through the same JSON envelope. `aq inbox` and `aq reply` are
top-level aliases of `inbox` and `reply` here; `aq inbox` additionally has
hook-safe error semantics.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq message inbox` | `message_inbox` | hand | Show a recipient's pending (undelivered) messages. |
| `aq message list` | `message_list` | hand | List messages, newest first. |
| `aq message reply` | `—` | hand | Reply to a message by id. |
| `aq message send` | `message_send` | hand | Queue a message to a session, task, profile, or user. |
| `aq message status` | `message_status` | hand | Show a message's queued, delivered, or acknowledged state. |

### `aq note`

Project notes — free-form markdown a project accumulates, provided by the
in-tree `aq-notes` plugin. `promote` incorporates a note into the project
profile; `compare-specs` lists spec files and note files side by side.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq note append` | `append_note` | gen | Append content to an existing note, or create a new note if it doesn't exist. |
| `aq note compare-specs` | `compare_specs_notes` | gen | List all spec files and note files for a project side by side. |
| `aq note delete` | `delete_note` | gen | Delete a project note by title. |
| `aq note list` | `list_notes` | gen | List all notes for a project. |
| `aq note promote` | `promote_note` | gen | Explicitly incorporate a note's content into the project profile. |
| `aq note read` | `read_note` | gen | Read a note's full contents. |
| `aq note write` | `write_note` | gen | Create or overwrite a project note. |

### `aq playbook`

Playbooks V2 — the event-triggered markdown workflows that carry policy.
The lifecycle is: author markdown in the vault, `v2-propose` or
`update-source` to turn it into an immutable artifact, `v2-validate` it
against the strict model and the live command contracts, then `activate` one
validated artifact hash. Runs are inspected with `list-runs` /
`inspect-run` / `run-overlay`, and a paused human-in-the-loop run is continued
with `resume`. `pending-events` lists events held because no artifact could
run them, which is the first place to look when a trigger appears to do
nothing.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq playbook activate` | `playbook_activate` | gen | Activate one validated Playbook V2 artifact hash. |
| `aq playbook activation-health` | `playbook_activation_health` | gen | List playbook V2 activations with their computed health (ready, question_required, invalid, disabled, stale_contract, unavailable), the active arti… |
| `aq playbook artifact-diff` | `playbook_artifact_diff` | gen | Semantically diff two Playbook V2 artifacts before activation. |
| `aq playbook artifacts` | `playbook_artifacts` | gen | List the stored Playbook V2 artifacts of one playbook, newest version first, with the currently active one flagged. |
| `aq playbook cancel-run` | `cancel_playbook_run` | gen | Cancel a playbook run that is running or paused. |
| `aq playbook delete` | `playbook_delete` | gen | Delete an exact disabled playbook catalog entry. |
| `aq playbook dry-run` | `dry_run_playbook` | gen | Simulate playbook execution with a mock event, producing no side effects. |
| `aq playbook get-source` | `get_playbook_source` | gen | Return the raw markdown of a playbook plus its content hash. |
| `aq playbook graph-layout-save` | `playbook_graph_layout_save` | gen | Persist user-arranged grid coordinates for the nodes of one immutable playbook artifact. |
| `aq playbook graph-view` | `playbook_graph_view` | gen | Get structured graph view data for dashboard rendering of a playbook. |
| `aq playbook health` | `playbook_health` | gen | Compute health metrics for playbook runs: tokens per node, run duration statistics, transition paths, and failure rates. |
| `aq playbook inspect-run` | `inspect_playbook_run` | gen | Inspect a playbook run in detail. |
| `aq playbook list` | `list_playbooks` | gen | List all playbooks across scopes with status, triggers, and last run info. |
| `aq playbook list-runs` | `list_playbook_runs` | gen | List recent playbook runs with status and path taken through the graph. |
| `aq playbook pending-event-action` | `playbook_pending_event_action` | gen | Dispatch or discard held playbook pending events. |
| `aq playbook pending-events` | `playbook_pending_events` | gen | List events held because no artifact could run them -- a stale contract, an invalid artifact, a disabled activation, an unavailable artifact file,… |
| `aq playbook resume` | `resume_playbook` | gen | Resume a paused (human-in-the-loop) playbook run. |
| `aq playbook run` | `run_playbook` | gen | Manually trigger a playbook run. |
| `aq playbook run-overlay` | `playbook_run_overlay` | gen | Return one Playbook V2 run's execution overlay, pinned to the artifact the run actually executed -- never the playbook's current activation, so an… |
| `aq playbook set-enabled` | `set_playbook_enabled` | gen | Pause or resume a playbook's activation. |
| `aq playbook show-graph` | `show_playbook_graph` | gen | Render a compiled playbook graph as an ASCII diagram or Mermaid flowchart syntax. |
| `aq playbook update-source` | `update_playbook_source` | gen | Write new playbook markdown to the vault atomically and compile synchronously. |
| `aq playbook v2-graph` | `playbook_v2_graph` | gen | Get the Playbook V2 semantic graph for one immutable artifact: rules grouped by triggering event, one node per typed step with a contract-derived e… |
| `aq playbook v2-import` | `playbook_v2_import` | gen | Import one approved Playbook V2 review bundle from inside the vault. |
| `aq playbook v2-propose` | `playbook_v2_propose` | gen | Turn an agent-produced rules/steps JSON body into a reviewable Playbook V2 proposal using server-owned source identity and live contract fingerprints. |
| `aq playbook v2-shadow-compile` | `playbook_v2_shadow_compile` | gen | Read every installed vault playbook source, deterministically lower supported kinds, and return validation diagnostics. |
| `aq playbook v2-validate` | `playbook_v2_validate` | gen | Validate an immutable Playbook V2 JSON artifact inside the vault against the strict model, registered command contracts, profiles, and event schemas. |

### `aq plugin`

Plugin lifecycle. Every leaf is hand-written and several use direct
database and filesystem access, because installing a plugin is a git clone and
a pip install rather than a daemon command. Mutations that would prompt
require `--yes` under `--json` and otherwise return a usage error rather than
hanging. `aq plugin logs` is deprecated: plugin hook execution history no
longer exists — use `aq playbook list-runs`.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq plugin config` | `—` | hand | View or set plugin configuration using optional KEY=VALUE pairs. |
| `aq plugin diff-prompts` | `—` | hand | Diff instance prompts vs source defaults. |
| `aq plugin disable` | `—` | hand | Disable a plugin without removing it. |
| `aq plugin enable` | `—` | hand | Enable a disabled plugin. |
| `aq plugin info` | `—` | hand | Show detailed plugin info. |
| `aq plugin install` | `—` | hand | Install a plugin from a git repository. |
| `aq plugin list` | `—` | hand | List installed plugins. |
| `aq plugin logs` | `—` | hand | Removed - plugin hook execution history no longer exists. |
| `aq plugin prompts` | `—` | hand | List prompts provided by a plugin. |
| `aq plugin reload` | `—` | hand | Reload a plugin module. |
| `aq plugin remove` | `—` | hand | Remove an installed plugin. |
| `aq plugin reset-prompts` | `—` | hand | Reset instance prompts to source defaults. |
| `aq plugin update` | `—` | hand | Update a plugin (git pull + reinstall). |

### `aq pool`

Worker pool sizing. A profile with `lifecycle: pool` pulls work with
`aq task claim` instead of having tasks pushed to it. Sizing is **global per
profile**: bounds, supply and demand are fleet-wide sums, and a separate
placement step decides which project each start lands in. `aq pool status` is
one row per profile with the per-project breakdown nested inside it. Pools are
off by default (`swarm.enabled`). See
[worker pools](../../guides/worker-pools.md).


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq pool scale` | `pool_scale` | gen | Set a pool profile's min/max active-session bounds on the system profile (they apply to every project; each project's max_concurrent_agents still c… |
| `aq pool set-enabled` | `pool_set_enabled` | gen | Turn a pool profile on or off on the system profile (it applies to every project). |
| `aq pool set-lifecycle` | `pool_set_lifecycle` | gen | Set a profile's lifecycle to task or pool on the system profile (it applies to every project). |
| `aq pool status` | `pool_status` | gen | Supply/demand/bounds snapshot for every worker pool (one row per profile, fleet-wide, with the per-project placement detail nested in `projects`). |

### `aq project`

Projects, their workspaces and their scheduling state. A **project** binds
a Git repository to a queue; a **workspace** is a directory of a declared kind
that tasks acquire. `aq project onboard` is the hand-written wizard that links,
initialises or clones a repository and registers it — it and the six other
onboarding reads require the global admin scope, because they authorise
filesystem and GitHub access on the daemon host rather than acting inside one
project. `workspace-doctor` and `workspace-reap` are the recovery pair for
orphaned worktree slots.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq project add-workspace` | `add_workspace` | gen | Add a workspace directory for a project. |
| `aq project browse-root` | `browse_project_root` | gen | List the child directories of a root-relative path beneath a configured project root. |
| `aq project create` | `create_project` | gen | Create a new project. |
| `aq project delete` | `delete_project` | gen | Delete a project and all associated data (tasks, repos, results, token ledger). |
| `aq project details` | `—` | hand | Show detailed information about a project with task breakdown. |
| `aq project edit-workspace` | `edit_workspace` | gen | Edit a workspace's path, source_type, name, or enabled state. |
| `aq project find-merge-conflict-workspaces` | `find_merge_conflict_workspaces` | gen | Scan project workspaces to find which ones have branches with merge conflicts against the default branch (main). |
| `aq project get` | `get_project` | gen | Get full details for a single project, including repo/GitHub URL, workspace path, default branch, token usage, and configuration. |
| `aq project get-github-auth-status` | `get_github_auth_status` | gen | Report whether the daemon host's `gh` CLI is installed and authenticated. |
| `aq project get-onboarding` | `get_project_onboarding` | gen | Read the durable status of an onboarding request by request_id: its status, current phase, safe result or structured error. |
| `aq project list` | `list_projects` | gen | List all projects in the system. |
| `aq project list-github-owners` | `list_github_owners` | gen | List the GitHub owners (the authenticated user and their organisations) a new repository may be created under via the daemon host's `gh` session. |
| `aq project list-roots` | `list_project_roots` | gen | List the configured project roots (config `project_roots`) an operator may browse and onboard projects beneath, with readable/writable flags. |
| `aq project list-workspace-kinds` | `list_workspace_kinds` | gen | List workspace kinds visible to a project (system + project-scoped overrides). |
| `aq project list-workspaces` | `list_workspaces` | gen | List workspaces, optionally filtered by project. |
| `aq project onboard` | `onboard_project` | hand | Link, initialize, or clone a repository and register its AQ project. |
| `aq project pause` | `pause_project` | gen | Pause a project so no new tasks are scheduled. |
| `aq project queue-sync-workspaces` | `queue_sync_workspaces` | gen | Queue a high-priority Sync Workspaces task that orchestrates a full workspace synchronization workflow. |
| `aq project ready` | `project_ready` | gen | The project's ready frontier plus the tasks withheld from it and why. |
| `aq project release-constraint` | `release_project_constraint` | gen | Release (remove) a scheduling constraint from a project. |
| `aq project release-workspace` | `release_workspace` | gen | Force-release a stuck workspace lock. |
| `aq project remove-workspace` | `remove_workspace` | gen | Delete a workspace from a project. |
| `aq project resume` | `resume_project` | gen | Resume a paused project. |
| `aq project search-github-repositories` | `search_github_repositories` | gen | Search GitHub repositories visible to the daemon host's `gh` session. |
| `aq project set` | `—` | hand | Set a project property. |
| `aq project set-constraint` | `set_project_constraint` | gen | Set a temporary scheduling constraint on a project. |
| `aq project workspace-doctor` | `workspace_doctor` | gen | Diagnose worktree inventory. |
| `aq project workspace-reap` | `workspace_reap` | gen | Explicitly reap a retired worktree slot (removes its directory + row). |

### `aq question`

Worker questions and their answers. A question is recorded from a live
worker's own transcript and carries a durable identity; a reply targets that
identity and reaches the original live session. `answer` replies without
restarting or reassigning the worker; `escalate` hands the decision to a human
through the escalation surface.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq question answer` | `—` | hand | Answer without restarting or reassigning the worker. |
| `aq question escalate` | `—` | hand | Ask a human to decide a question that the supervisor cannot answer. |
| `aq question list` | `—` | hand | List questions waiting for a supervisor, human, or safe answer delivery. |

### `aq session`

Sessions — a coding-agent CLI running inside a tmux terminal. This group
is how an operator sees and steers them: `list` and `show` for state, `peek`
for the visible pane, `logs` for the recorded transcript, `nudge` to type into
one, `kill` (instance-token fenced) to end one. `drain-ack` is the agent-facing
half of the completion protocol — a worker says "I am finished, you may reap
me". `token` mints a fresh bearer token for a session and is a development and
end-to-end facility: it is excluded from the MCP surface entirely, because a
credential minter is never something an MCP client should reach.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq session attach` | `—` | hand | Print the command that attaches a terminal to this session. |
| `aq session drain-ack` | `—` | hand | Agent-facing: declare this session finished so it can be reaped. |
| `aq session kill` | `—` | hand | Kill a session (instance-token fenced). |
| `aq session list` | `—` | hand | List sessions with state, task, harness and idle time. |
| `aq session logs` | `—` | hand | Read recorded output, optionally restricted to one task attempt. |
| `aq session nudge` | `—` | hand | Inject TEXT into a session and submit it. |
| `aq session peek` | `—` | hand | Last N lines of a session's visible output. |
| `aq session show` | `—` | hand | Full detail for one session. |
| `aq session sleep` | `—` | hand | Mark a session as not wanted running. |
| `aq session token` | `—` | hand | Mint a fresh API bearer token for SESSION_ID (dev / e2e facility). |
| `aq session wake` | `—` | hand | Mark a sleeping named session as wanted again. |

### `aq stream`

The streamable-command registry behind the dashboard's console pane.
`start` runs a command and buffers its output (`aq stream start -- pytest
tests/ -x`), `tail` polls that buffer, `kill` escalates SIGTERM to SIGINT to
SIGKILL. `start` is a passthrough command, so global options must be given in
prefix position.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq stream kill` | `—` | hand | Kill a running stream (SIGTERM -> SIGINT -> SIGKILL escalation). |
| `aq stream start` | `—` | hand | Start a streamable command: ``aq stream start -- pytest tests/ -x``. |
| `aq stream tail` | `—` | hand | Poll buffered output since ``--after-seq`` (non-SSE fallback). |

### `aq subagent`

Native sub-agent telemetry. `aq subagent event --hook-json` reads a
harness `SubagentStart` / `SubagentStop` payload on stdin and records one row
against the calling session. It runs on the agent's own critical path, so its
two hard rules are never block and never fail: a delivery error prints nothing
and exits `0`. Set `AQ_SUBAGENT_HOOK_DEBUG` to see the swallowed reason.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq subagent event` | `subagent_event` | hand | Record one native sub-agent start/stop for this session. |

### `aq system`

The catch-all operator group: configuration, diagnostics, prompts,
workflows, and the integration primitives that the delivery train is built
from. Two things are worth knowing before reading the table. First, `aq system
config` is a hand-written subgroup that edits `~/.agent-queue/config.yaml`
directly — `get`, `set`, `schema` and an `$EDITOR` round trip that validates
before it swaps the file. Second, everything named `integration-*` here is a
*primitive*, called by playbooks and by the daemon rather than by a person; the
human-facing controls are in [`aq integration`](#aq-integration). A generated
command with no category lands in this group as a fallback, which is why the
group is large.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq system advance-workflow-stage` | `advance_workflow_stage` | gen | Advance a workflow to its next stage. |
| `aq system claude-usage` | `claude_usage` | gen | Get Claude Code usage stats from live session data. |
| `aq system config edit` | `—` | hand | Open the full config in $EDITOR; on save, validate + apply. |
| `aq system config get` | `get_config` | hand | Print the raw YAML config (optionally one section). |
| `aq system config schema` | `get_config_schema` | hand | Print the JSON Schema describing all config fields. |
| `aq system config set` | `—` | hand | Set one key by dotted path, e.g. |
| `aq system create-workflow` | `create_workflow` | gen | Create a new coordination workflow record. |
| `aq system db-preflight-hierarchy` | `db_preflight_hierarchy` | gen | Dry-run the hierarchy canonicalisation used by migration revision b2c3d4e5f6a7 (spec §17): pick one canonical parent per task from the current pare… |
| `aq system delivery-promote` | `delivery_promote` | gen | Prepare and lease-push one reviewed squash to its immediate parent. |
| `aq system delivery-receipts` | `delivery_receipts` | gen | Read repository-qualified delivery evidence for one source task. |
| `aq system doctor` | `doctor` | gen | Run the health-check catalog for this install and return one result per check: id, severity (ok/info/warn/error), detail, whether it is fixable, an… |
| `aq system edit-intelligence-class` | `edit_intelligence_class` | gen | Edit an existing global intelligence class in the vault. |
| `aq system find-applicable-tool` | `find_applicable_tool` | gen | Search for tools by describing what you want to do. |
| `aq system get-chat-analyzer-metrics` | `get_chat_analyzer_metrics` | gen | Aggregated metrics for chat-analyzer suggestion outcomes. |
| `aq system get-config` | `get_config` | gen | Return the raw YAML configuration as written on disk, preserving ${ENV_VAR} placeholders. |
| `aq system get-config-schema` | `get_config_schema` | gen | Return a JSON Schema describing every AppConfig field. |
| `aq system get-costs` | `get_costs` | gen | Roll the token ledger up into USD using the 'pricing:' table from config.yaml. |
| `aq system get-recent-events` | `get_recent_events` | gen | Get recent system events (task completions, failures, agent questions, budget warnings, etc.) from the event database. |
| `aq system get-schema` | `get_schema` | gen | Return the system's enum catalog — task statuses, task types, dependency types, gate types and gate statuses — so callers never guess magic strings. |
| `aq system get-stuck-tasks` | `get_stuck_tasks` | gen | Return tasks stuck in ASSIGNED or IN_PROGRESS beyond their per-status threshold. |
| `aq system get-token-usage` | `get_token_usage` | gen | Get token usage breakdown by project or task. |
| `aq system get-workflow` | `get_workflow` | gen | Fetch a single workflow by ID. |
| `aq system integration-build-candidate` | `integration_build_candidate` | gen | Build exact root candidate |
| `aq system integration-checkpoint-parent` | `integration_checkpoint_parent` | gen | Pin the parent head and generation before waiting for child deliveries. |
| `aq system integration-ci-evidence` | `integration_ci_evidence` | gen | Observe exact root candidate CI |
| `aq system integration-cleanup` | `integration_cleanup` | gen | Materialize and advance bounded cleanup for one terminal root batch. |
| `aq system integration-complete-parent` | `integration_complete_parent` | gen | Execute the integration_complete_parent command. |
| `aq system integration-delivery-readiness` | `integration_delivery_readiness` | gen | Execute the integration_delivery_readiness command. |
| `aq system integration-file-children` | `integration_file_children` | gen | Reserve child origins and advance the parent integration generation atomically. |
| `aq system integration-mutate-hierarchy` | `integration_mutate_hierarchy` | gen | Apply a guarded hierarchy change and invalidate affected parent generations. |
| `aq system integration-parent-verify` | `integration_parent_verify` | gen | Execute the integration_parent_verify command. |
| `aq system integration-promote-main` | `integration_promote_main` | gen | Reconcile and fast-forward main to the exact trusted green candidate. |
| `aq system integration-push-conflict-resolution` | `integration_push_conflict_resolution` | gen | Push a frozen conflict resolution under the current repair writer fence. |
| `aq system integration-reconcile-promotion` | `integration_reconcile_promotion` | gen | Compare a durable prepared intent with the remote and finalize its receipt. |
| `aq system integration-record-repair` | `integration_record_repair` | gen | Execute the integration_record_repair command. |
| `aq system integration-recover-unwritten-resolution` | `integration_recover_unwritten_resolution` | gen | Supersede a malformed reservation only after an operator proves no remote write occurred. |
| `aq system integration-release` | `integration_release` | gen | Release terminal root train |
| `aq system integration-repair-dispatch` | `integration_repair_dispatch` | gen | Execute the integration_repair_dispatch command. |
| `aq system integration-repair-start` | `integration_repair_start` | gen | Execute the integration_repair_start command. |
| `aq system integration-repair-timeout` | `integration_repair_timeout` | gen | Execute the integration_repair_timeout command. |
| `aq system integration-resolve-conflict` | `integration_resolve_conflict` | gen | Freeze an active repair session's exact conflict resolution before push. |
| `aq system integration-schedule-due` | `integration_schedule_due` | gen | Coalesce a periodic or manual trigger into one durable sweep request. |
| `aq system integration-seal` | `integration_seal` | gen | Atomically snapshot the full eligible integration frontier. |
| `aq system integration-transfer-owner` | `integration_transfer_owner` | gen | Stop and detach the current branch writer before granting a fresh fence. |
| `aq system list-event-triggers` | `list_event_triggers` | gen | List event types that are valid playbook triggers, grouped by category (e.g. |
| `aq system list-intelligence-classes` | `list_intelligence_classes` | gen | List the intelligence classes seeded under ``vault/intelligence-classes`` — id, name, description, and the provider→config mapping each one resolve… |
| `aq system list-prompts` | `list_prompts` | gen | List all prompt templates for a project. |
| `aq system list-workflows` | `list_workflows` | gen | List workflows with optional filters. |
| `aq system migrate-profiles` | `migrate_profiles` | gen | Migrate DB-only profiles to vault markdown files. |
| `aq system orchestrator-control` | `orchestrator_control` | gen | Pause, resume, or check the status of the orchestrator (task scheduler). |
| `aq system provide-input` | `provide_input` | gen | Provide a human reply to an agent question (WAITING_INPUT → READY). |
| `aq system provider-usage-probe` | `provider_usage_probe` | gen | Ask a provider's own CLI what is left of the account's limit windows and record the reading. |
| `aq system read-logs` | `read_logs` | gen | Read and filter the daemon's structured JSONL log file. |
| `aq system read-prompt` | `read_prompt` | gen | Read a prompt template's full content and metadata. |
| `aq system reload-config` | `reload_config` | gen | Manually trigger a config hot-reload from disk. |
| `aq system render-prompt` | `render_prompt` | gen | Render a prompt template with variable substitution. |
| `aq system scan-stub-staleness` | `scan_stub_staleness` | gen | Scan vault reference stubs to detect staleness. |
| `aq system session-input` | `session_input` | gen | Type directly into a live terminal. |
| `aq system task-route-options` | `task_route_options` | gen | Report whether the task is routed, whether its class is explicit, and which class, provider and profile combinations could execute it. |
| `aq system token-audit` | `token_audit` | gen | Comprehensive token usage audit over a time range. |
| `aq system update-config` | `update_config` | gen | Replace one top-level section in the YAML config and trigger a hot reload for hot-reloadable sections. |
| `aq system vault-rebuild-index` | `vault_rebuild_index` | gen | Rebuild vault hub files with optional LLM-generated summaries. |
| `aq system workflow-pipeline-view` | `workflow_pipeline_view` | gen | Return structured pipeline view data for dashboard rendering of a workflow. |

### `aq task`

The task lifecycle — the group most sessions and most operators live in.
The hand-written leaves are the ones with ergonomics that matter: `show`,
`set`, `comment`, `comments`, `claim`, `close`, `heartbeat` and `create` take
a positional task id (or none at all, defaulting to the session's own task),
and the claim-fenced mutators read `--claim-epoch` from
`<work_dir>/.aq/claim.json` so a stale session cannot overwrite a reclaimed
task. Everything else is generated and takes `--task-id`. `explain` is the
command to reach for when a task is not running: it returns the ordered list
of reasons. `gate-*` are the human-in-the-loop gates; `batch-*` are the staged
proposal flow a spec ingest produces.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq task add-dependency` | `add_dependency` | gen | Add a dependency between two tasks: task_id will depend on depends_on (i.e. |
| `aq task archive` | `archive_task` | gen | Archive tasks. |
| `aq task archive-settings` | `archive_settings` | gen | Return the current auto-archive configuration. |
| `aq task batch-commit` | `task_batch_commit` | gen | Atomically materialise a proposal into the live work graph: creates every task, then every dependency edge, stamping the proposal's source as prove… |
| `aq task batch-discard` | `task_batch_discard` | gen | Discard a pending proposal without creating anything. |
| `aq task batch-propose` | `task_batch_propose` | gen | Propose a batch of tasks and their dependency edges as one reviewable graph, without creating anything live. |
| `aq task batch-update` | `task_batch_update` | gen | Replace a pending proposal's tasks and edges, re-running the same shape, reference and cycle checks as task_batch_propose. |
| `aq task children` | `task_children` | gen | List the children of a task (direct, or the whole subtree with recursive=true). |
| `aq task claim` | `task_claim` | hand | Claim a ready task for this session (pull-based work selection, §10). |
| `aq task close` | `task_close` | hand | Close TASK_ID with an outcome; only way a session-run task reaches COMPLETED. |
| `aq task comment` | `task_comment` | hand | Append an attributed comment to a task's durable history. |
| `aq task comment-delete` | `task_comment_delete` | gen | Delete one task comment. |
| `aq task comment-edit` | `task_comment_edit` | gen | Replace the text of an existing task comment. |
| `aq task comments` | `task_comments` | hand | Read a task's comments, newest first. |
| `aq task create` | `—` | hand | Create a new task (interactive wizard or via flags). |
| `aq task delete` | `delete_task` | gen | Delete a task. |
| `aq task deps` | `task_deps` | gen | Return upstream dependencies and downstream dependents for a task. |
| `aq task details` | `—` | hand | Alias of `aq task show` (kept for backward compatibility). |
| `aq task edit` | `edit_task` | gen | Edit a task's properties: project_id, title, description, priority, task_type, status, max_retries, verification_type, profile_id, integration_mode… |
| `aq task ensure` | `ensure_task` | gen | Find-or-create a task by (project_id, dedup_key). |
| `aq task explain` | `explain_task` | gen | Return the ordered list of reasons *task_id* isn't running. |
| `aq task gate-create` | `gate_create` | gen | Open a gate, and block the tasks waiting on it until it resolves. |
| `aq task gate-list` | `gate_list` | gen | List gates, optionally filtered by project/status/type. |
| `aq task gate-resolve` | `gate_resolve` | gen | Resolve an open gate and unblock every task waiting on it. |
| `aq task gate-show` | `gate_show` | gen | Return one gate + its waiter task ids. |
| `aq task get` | `get_task` | gen | Get full details of a specific task including its description. |
| `aq task get-chain-health` | `get_chain_health` | gen | Check dependency chain health. |
| `aq task get-dependencies` | `get_task_dependencies` | gen | Get the full dependency graph for a specific task: what it depends on (upstream) and what it blocks (downstream). |
| `aq task get-downstream` | `get_downstream_tasks` | gen | List tasks transitively downstream of a task via dependency edges (blocks, waits-for, conditional-blocks, parent-child). |
| `aq task get-result` | `get_task_result` | gen | Retrieve a task's output: summary, files changed, error message, tokens used. |
| `aq task get-tree` | `get_task_tree` | gen | Get the subtask hierarchy for a specific parent task, rendered as a tree with box-drawing characters. |
| `aq task heartbeat` | `task_heartbeat` | hand | Refresh this task's agent lease so the stall ladder doesn't climb. |
| `aq task list` | `list_tasks` | hand | List tasks. |
| `aq task list-active-all-projects` | `list_active_tasks_all_projects` | gen | List active tasks across ALL projects, grouped by project. |
| `aq task list-archived` | `list_archived` | gen | List archived tasks. |
| `aq task pause` | `pause_task` | gen | Pause a queued or running task until explicit Resume. |
| `aq task progress` | `task_progress` | gen | Computed progress for a container: counts, Kahn waves, max parallelism. |
| `aq task recent-activity` | `task_recent_activity` | gen | Show what was worked on recently and by which models. |
| `aq task recover` | `task_recover` | gen | Decide a supervisor recovery incident: retry safely within existing budgets or hold with a recorded diagnosis. |
| `aq task remove-dependency` | `remove_dependency` | gen | Remove a dependency between two tasks: task_id will no longer depend on depends_on. |
| `aq task reopen-with-feedback` | `reopen_with_feedback` | gen | Reopen a completed or failed task with feedback. |
| `aq task reparent` | `reparent_task` | gen | Move a task under another container (parent_id) or to the root (root=true). |
| `aq task restart` | `restart_task` | hand | Restart a failed or stopped task. |
| `aq task resume` | `resume_task` | gen | Resume a paused task, respecting its existing gates and approval state. |
| `aq task route` | `task_route` | gen | Route a task: assign its agent profile, optional intelligence class, and optional workspace, then resolve any open 'routing' gates on the task. |
| `aq task search` | `list_tasks` | hand | Search tasks by title or description. |
| `aq task select` | `—` | hand | Interactively select a task and show its details. |
| `aq task set` | `task_set` | hand | Work-state writes: findings, branch, PR URL, work dir, notes, labels, metadata. |
| `aq task set-status` | `set_task_status` | gen | Directly set a task's status. |
| `aq task show` | `task_show` | hand | Show full task detail: fields, dependencies, context, labels. |
| `aq task skip` | `skip_task` | gen | Skip a BLOCKED or FAILED task to unblock its dependency chain. |
| `aq task spec-approve` | `spec_approve` | gen | Flip a spec's frontmatter ``status`` to ``approved`` in place, preserving comments and key order. |
| `aq task stop` | `stop_task` | hand | Stop a running task. |

### `aq vault`

Vault maintenance. `migrate` runs every vault migration and is idempotent.
`reset-harness` restores one `vault/harnesses/<name>.md` from the shipped
default — worth knowing when a locally edited harness file is masking a
shipped fix. The group refuses `--json`: it is a local operator workflow with
interactive safeguards.


| Command | Daemon command | Kind | What it does |
|---|---|---|---|
| `aq vault migrate` | `—` | hand | Run all vault migrations (idempotent, safe to run multiple times). |
| `aq vault reset-harness` | `—` | hand | Restore vault/harnesses/<NAME>.md from the shipped default. |

## Related pages

* [The CLI contract](README.md) — options, output, exit codes, authority.
* [`aq prime` and the session hooks](prime.md) — the commands a worker session
  runs that no operator ever types.
* [Agent-facing tools](agent-tools.md) — the same commands as tool definitions.
* [Module catalog: CLI](../modules/cli.md) — the modules these leaves live in.

## Source and tests

Registration [`src/cli/app.py`](../../../src/cli/app.py); generation
[`src/cli/auto_commands.py`](../../../src/cli/auto_commands.py); the
hand-written modules are listed in the [module catalog](../modules/cli.md);
inventory generation [`src/cli/inventory.py`](../../../src/cli/inventory.py).

```bash
aq test tests/test_cli_conformance.py tests/test_cli_inventory.py tests/test_guidance_docs.py
python scripts/generate-cli-command-inventory.py --check
```
