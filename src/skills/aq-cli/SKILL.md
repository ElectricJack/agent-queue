---
name: aq-cli
description: Orients an agent in the aq (agent-queue) command-line interface — the command groups, the discovery workflow (`--help`, `--help-all`, `aq schema`), global output flags (`--json`, `--brief`, `--save-output`), token scope and `out of scope` errors, profiles and intelligence classes, and which focused aq-* skill covers a workflow. Use when looking for an aq command, checking a command's flags or an enum value before running it, reading JSON output, or when another aq skill points here. Also covers how shipped skills are installed and refreshed.
allowed-tools:
  - Bash
---

# aq CLI — Orientation & Discovery

`aq` is the command-line surface for the agent-queue daemon. Everything the
daemon can do — task management, workspaces, sessions, playbooks, gates,
messages, projects, plugins, knowledge records — is reachable through it. Prefer
`aq` over any MCP tool that duplicates its function; the CLI is the
source of truth.

## Command groups

`aq --help` is the authoritative list; this map groups the ones agents use
most and drifts before the CLI does. Confirm a shape with `--help` before you
run something new.

| Purpose | Groups |
|---|---|
| Your task and its loop | `aq prime`, `aq task`, `aq handoff`, `aq wait`, `aq schema` |
| Messages and decisions | `aq message` (`aq inbox`, `aq reply` are aliases), `aq question`, `aq collaboration`, `aq cron`, `aq ask`, `aq escalation` |
| Code and delivery | `aq git`, `aq test`, `aq job`, `aq ci`, `aq integration`, `aq batch` |
| Documents and knowledge | `aq review`, `aq knowledge`, `aq record`, `aq note`, `aq file` |
| Workflows | `aq playbook`, `aq formula`, `aq graph` |
| Fleet and diagnostics | `aq session`, `aq pool`, `aq agent`, `aq provider`, `aq status`, `aq logs`, `aq doctor`, `aq costs`, `aq system` |
| Operator only | `aq start` / `stop` / `restart` / `update`, `aq db`, `aq install`, `aq service`, `aq supervisor`, `aq vault` |

## Durable knowledge

Use **aq-knowledge** when asked to save or recall a reference, finding, decision
or procedure. `aq knowledge` writes the canonical PostgreSQL records;
`aq record` discovers and links them. Check `aq record capabilities` for the
explicit project and search before creating a duplicate. Confirm a save with its
record identity, revision and canonical readback. Legacy notes, vault files and
Markdown exports do not establish graph ingestion. Optional semantic memory is
separate from core storage and lexical search; it is not the durable-save path.

## Discovery workflow

Start with a narrow reference; load broad inventory only when needed:

1. **`aq <group> <cmd> --help`** — prints detail on one specific command.
   Use this before invoking anything you haven't run before.
2. **`aq --help-all`** — prints the full command reference for the entire
   CLI (every group, every subcommand, every flag). Emit this to a temp
   file (`aq --help-all > /tmp/aq-help.txt`) and read/grep it when you
   need a broad view.

Example:

```bash
aq task --help                    # subcommands of `aq task`
aq task close --help              # arguments and flags for `aq task close`
aq --help-all | grep -A5 gate     # every gate-related command
```

Never guess an enum value. `aq schema` prints the catalog the daemon
actually validates against — task statuses, `outcome` (`pass` / `fail`),
`claim_result`, `hierarchy_error`, dependency types.

## Output formats

`aq` prints human-friendly tables by default. These flags change that:

- `--json` — the versioned envelope on stdout:
  `{"schema_version": …, "data": …, "pagination"?: …}` on success and
  `{"schema_version": …, "error": …, "data": null}` on failure. Read
  `.data`, not the top level. (`AQ_JSON_LEGACY=1` restores the
  pre-envelope raw payload for one release; don't rely on it.)
- `--brief` — trims each entity to a compact projection (still readable).
  Composes with `--json`.
- `--save-output PATH` — for emit-based CLI results, writes the complete
  unprojected envelope to a new private JSON file and prints its path, byte count,
  SHA-256 and shape. Page relevant keys/lines from it instead of repeating large
  success output. Failures, warnings, gates, claim outcomes and instructions remain
  visible. Existing files are refused; no evidence is overwritten.

These are **global**: they mean the same thing wherever you put them, so
`aq task list --json`, `aq task --json list` and `aq --json task list` are
the same command. Two exceptions: after a passthrough command the flag
belongs to the child program (`aq test tests/x.py --json` hands `--json` to
pytest — write `aq --json test …` instead), and a command that declares its
own `--json` keeps its local meaning (`aq doctor`, `aq logs`,
`aq system config get`). `--api-url` has the same global placement behavior;
`--help-all` remains top-level-only.

Example:

```bash
aq --json task list | jq '.data[] | select(.status=="READY") | .id'
```

## Scope + authentication

The CLI talks to the daemon on `127.0.0.1:8081` (or `AQ_API_URL` /
`--api-url`). `AQ_API_TOKEN`, injected into every daemon-started session,
is sent as `Authorization: Bearer <token>` and **is enforced**:

- **No bearer, on loopback** — the trusted local operator path. Every
  command is allowed.
- **A session token** — restricted to the agent command set (task reads
  and writes for your own task, comments, close, claim, heartbeat,
  handoff, messages, granted knowledge/record operations, `prime`, `schema`,
  `session drain-ack`), and
  to your own `task_id` / `project_id`. Anything else answers
  `out of scope: <command>` or `out of scope: <field> mismatch`.
- **A supervisor token** — *elevated*, within its session scope and grants.
  A project supervisor stays in its project. A global supervisor chooses an
  explicit project for targeted record operations without changing its session
  scope; global records require explicit global scope and separate enablement.

Don't work around scope errors and don't retry them — they are a property
of your token, not a transient failure. If you legitimately need a broader
command, message the human (`aq message send --to user:dashboard`).

## Profiles and intelligence classes

A task runs under an **agent profile** (which worker: tools, prompt,
`harness`, lifecycle) and an **intelligence class** (which model and how
much reasoning). Both are markdown in the vault, so the current set is
whatever this install has — read it, never hard-code it:

```bash
aq agent list-profiles                    # the profiles that exist here
aq system list-intelligence-classes       # the classes that exist here
```

Nobody files queued work on a profile: the project's router picks every
task's profile, provider and model, and a filing that names one is refused
(`routing.choice_forbidden`). A filer gives the kind (`--type`) and, when it
matters, an intelligence-class hint (`--intelligence-class`); see the aq-tasks
skill. Worker profile IDs are installation-specific, so never infer one from an ID
prefix or an example in documentation. The shipped intelligence classes are
`fast-{low,high}`, `standard-high`, `deep-{low,high}` and `astra-{low,high}` —
Astra is OpenAI-only, and nothing from the deep tier upward runs on Gemini — but
an install can retire or add classes, so the list command above is the answer.

## When to reach for a specialized aq-* skill

For common workflows there are focused skills that document the exact
command shapes and gotchas. Use them instead of re-deriving:

- **aq-tasks** — creating / closing / reopening / editing tasks, working
  with results, dependencies, the pool claim loop.
- **aq-comms** — messages, inbox handling, and blocker reporting.
- **aq-knowledge** — canonical durable saves, scoped search, revision-guarded
  edits, receipts and informational links.
- **aq-workspaces-and-git** — workspace ops + git via CLI.
- **aq-playbooks-and-gates** — playbook runs and human-in-the-loop
  gates.
- **aq-reviews** — document reviews: submitting, revising, reviewing
  when dispatched, and the cross-family adversarial review recipe.
- **aq-mini-projects** — explicitly requested quick ideation in one task:
  frozen brief, proposal/critique/synthesis and a roadmap for human plan review.
- **aq-bazel-upgrade** — adding reproducible Bazel test support to an
  AQ-managed Python or JS/TS repository.

If a workflow needs a command none of those cover, come back here and
use the discovery workflow above.

## Keeping these skills current (operators)

These skills ship in the repo as `src/skills/<name>/SKILL.md`, plus any
`reference/<topic>.md` files a skill links to. `src.vault.ensure_default_aq_skills`
copies them into every harness skill directory (`~/.claude/skills/`,
`~/.gemini/skills/`, `~/snap/gemini-cli/common/.gemini/skills/`,
`~/.codex/skills/`) at daemon start. The copy is **write-if-absent per file**:
an installed file is never overwritten, so operator edits survive upgrades — and
so does a stale copy after the in-tree source is fixed. A reference file added
to an already-installed skill is copied in beside it.

`aq doctor --check skills.installed_drift` reports installed files that differ
from the shipped source and reference files missing beside an installed skill;
`--fix` backs each drifted file up next to itself as `<file>.bak`, re-copies the
shipped version and copies in missing references. Review the reported drift
before applying the fix and reconcile custom edits from the backups afterwards:

```bash
aq doctor --check skills.installed_drift          # what has drifted
aq doctor --check skills.installed_drift --fix    # back up + re-copy
```

An agent inside a task worktree cannot do this: the installed copies live
outside the workspace and re-seeding is a daemon/operator action. Ship the fix
in `src/skills/` and tell the operator to run the check. New skills are
discovered by directory scan and seeded when absent for Claude, Codex and both
Gemini installations with the applicable harness homes present.
Profile text has a separate refresh path (grants-only reseeding does not refresh
role or rules); see `docs/guides/knowledge-records.md` in the agent-queue
repository.
