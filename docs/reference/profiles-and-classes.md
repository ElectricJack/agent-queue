# Profile and intelligence class reference

Every field of an agent profile's `## Config` block, every field of an
intelligence class file, and the exact order in which overrides win. For what
these things are *for*, read [agents and routing](../concepts/agents-and-routing.md)
first; this page is exhaustive rather than gentle.

## Where the files live

| Artefact | Path | Source of truth |
|---|---|---|
| Agent profile | `~/.agent-queue/vault/agent-types/<id>/profile.md` | The markdown. The `agent_profiles` table is a cache kept current by [`src/profiles/sync.py`](../../src/profiles/sync.py). |
| Shipped profile defaults | [`src/profiles/defaults/<id>/profile.md`](../../src/profiles/defaults/) | Copied into the vault **only if absent**. |
| Intelligence class | `~/.agent-queue/vault/intelligence-classes/<id>.md` | The markdown. There is no database table at all. |
| Shipped class defaults | [`src/prompts/default_intelligence_classes/<id>.md`](../../src/prompts/default_intelligence_classes/) | Copied into the vault **only if absent**. |
| Retirement tombstones | `~/.agent-queue/vault/agent-types/.retired-defaults` | Written by `aq agent delete-profile`; suppresses re-seeding. |

Both directories are watched, so a saved file takes effect without a daemon
restart.

## Agent profile format

A profile is one markdown file: YAML frontmatter, then sections. Sections whose
name is in the structured set are parsed from a fenced ```json``` block;
sections in the prompt set are captured as text and injected into the agent's
prompt. Anything else is ignored. Parsing is deterministic — invalid JSON is a
parse error, never a silent fallback
([`src/profiles/parser.py`](../../src/profiles/parser.py)).

| Section | Kind | Contents |
|---|---|---|
| frontmatter | YAML | `id`, `name`, `tags`; unknown keys are preserved |
| `## Config` | JSON | the table below |
| `## Tools` | JSON | `allowed` / `denied` tool name lists (legacy) |
| `## Capabilities` | JSON | `harness_tools`, `aq_commands`, `plugin_tools` |
| `## MCP Servers` | JSON | server **names**, resolved against the MCP registry |
| `## Install` | JSON | dependency manifest checked by `aq agent check-profile` |
| `## Role` | text | system prompt prefix |
| `## Rules` | text | behavioural guidance |
| `## Reflection` | text | post-task reflection instructions |

### `## Config` fields

| Key | Type | Default | Meaning |
|---|---|---|---|
| `harness` | string | none | **The only** selector for which CLI runs the agent: `claude`, `codex`, `gemini`. Implies the provider. |
| `default_class` | string | `""` | Intelligence class used when the task does not name one. Also what makes a `pool` profile offerable to routing. |
| `lifecycle` | `task` \| `pool` \| `named` | `task` | Push one session per assigned task; pull work by claiming; or one long-lived named session. |
| `mode` | `always` \| `on_demand` | none | `named` only: keep resident, or wake on demand. |
| `wake_mode` | `resume` \| `fresh` | none | `named` only: resume the prior context or start clean. |
| `idle_timeout` | int (s) | none | `named`/`on_demand` only: sleep after this idle. |
| `max_session_age` | int (s) | none | Recycle a named session older than this. |
| `needs_workspace` | bool | `true` | Whether the orchestrator acquires a workspace before launching. |
| `workspaces` | list | none | Workspace kinds to attach. Validated here, resolved by the session runtime. |
| `read_only` | bool | `false` | A declaration of write *intent*. It no longer changes workspace acquisition; it gates the require-a-PR close check. |
| `enabled` | bool | `true` | Operator kill switch. `false` = hand this profile no new work. |
| `permission_mode` | enum | `""` | One of `default`, `plan`, `full`, `bypassPermissions`, `acceptEdits`, `auto`. |
| `codex_full_auto` | bool | `false` | Codex-only autonomous permission opt-in. |
| `claude_dangerously_skip_permissions` | bool | `false` | Claude-only autonomous permission opt-in. |
| `max_tokens_per_task` | int | none | Per-task token ceiling. |
| `allow_base_checkout` | bool | `false` | Escape hatch letting a session run in a base checkout. Nothing shipped sets it. |
| `min_active` | int | none | `pool` only: fleet-wide warm floor. |
| `max_active` | int | none | `pool` only: fleet-wide ceiling. `null` = unlimited. |
| `min_per_project` | int | `0` | `pool` only: keep this many workers resident per eligible project. |
| `max_claims_per_session` | int | none | `pool` only: claims before a session retires. `null` = unlimited. |

Two keys were **removed** and are now rejected at parse time with a pointer to
`harness`, rather than being silently ignored:

* `runtime` — the in-process Supervisor is gone; every agent is a session. The
  one surviving non-empty value is `"supervisor"`, for the daemon's own brain.
* `agent_name` — went with the retired `acpx` runtime.
* `model` — a per-profile model pin. Removed in favour of intelligence classes;
  [`src/profiles/model_pin_migration.py`](../../src/profiles/model_pin_migration.py)
  strips any left in a vault at startup, logging a mismatch so an operator can
  audit it.

### Capability namespaces

`## Capabilities` has exactly three namespaces and no wildcards — `"*"`,
`"**"` and `"mcp__github__*"` all fail at construction
([`src/profiles/capabilities.py`](../../src/profiles/capabilities.py)):

| Namespace | Contains | Enforced by |
|---|---|---|
| `harness_tools` | Names the CLI understands (`Bash`, `Read`, …) | The harness's own allowlist flag — best effort; a harness without one cannot be restricted. |
| `aq_commands` | `CommandHandler` command names | The server-side check at dispatch. This is the real boundary, because a session reaches these through `Bash`. |
| `plugin_tools` | Unprefixed plugin commands and fully-qualified MCP tools (`mcp__github__create_issue`) | Dispatch. |

Two rules that are easy to get backwards:

* **Empty means none.** An explicitly empty list denies everything in that
  namespace. `None` — the namespace not authored at all — is what means
  "unset", and it triggers the legacy adapter.
* **The legacy adapter never adds rights.** A profile with no `## Capabilities`
  block has its policy derived from `allowed_tools` and is marked
  `derived_from_legacy`, which routes it through the audit path rather than
  hard denial. List the migration backlog with `aq agent profile-audit
  --legacy-only`.

## Intelligence class format

A class file is YAML frontmatter plus **one** fenced ```json``` block mapping
provider name to a runtime config slice
([`src/intelligence_classes/__init__.py`](../../src/intelligence_classes/__init__.py)).

| Field | Where | Meaning |
|---|---|---|
| `id` | frontmatter | Class id. Defaults to the file name. |
| `name` | frontmatter | Display name. |
| `description` | frontmatter | What the class is for. Shown in operator surfaces. |
| `customized` | frontmatter | `true` opts the file out of shipped-default refresh (see below). Set by the editor on an explicit save. |
| `anthropic` | JSON | `{"model": …, "thinking": off\|low\|medium\|high\|xhigh\|max}` |
| `openai` | JSON | `{"model": …, "reasoning_effort": none\|minimal\|low\|medium\|high\|xhigh}` |
| `codex` | JSON | Optional. Overrides the `openai` slice **only** for the Codex CLI, whose account models are a separate namespace. |
| `google` | JSON | `{"model": …, "thinking_budget": <int>}` |

Resolution is `(class_id, provider) → slice`. A missing provider key yields
`{}`, which means "no class-driven override" and leaves the launch model unset
rather than guessing.

### Provider inference

There is no provider field anywhere. It comes from the harness id
([`_infer_provider_from_harness`](../../src/sessions/spec.py)):

| Harness | Provider key |
|---|---|
| `claude` | `anthropic` |
| `codex` | `openai` (plus the optional `codex` slice) |
| `gemini` | `google` |
| anything else | `""` — no class-driven model |

The Codex slice is only consulted when the harness id is `codex` **and** its
command basename is `codex`, so another OpenAI-backed harness does not pick up
Codex account models.

### Shipped classes

The twelve shipped classes are the cross product of three tiers with four
thinking levels. Their current mappings are in the files themselves — read
them rather than trusting a copy:

```bash
grep -H -o '"anthropic": {"model": "[^"]*"' src/prompts/default_intelligence_classes/*.md
```

```text
src/prompts/default_intelligence_classes/deep-high.md:"anthropic": {"model": "claude-fable-5"
src/prompts/default_intelligence_classes/deep-low.md:"anthropic": {"model": "claude-fable-5"
…
src/prompts/default_intelligence_classes/standard-high.md:"anthropic": {"model": "claude-opus-5"
```

The shape, as of the files in this checkout:

| Class | `anthropic` | `openai` / `codex` | `google` |
|---|---|---|---|
| `fast-*` | `claude-sonnet-5` | `gpt-5.6-luna` | `gemini-2.5-flash` |
| `standard-*` | `claude-opus-5` | `gpt-5.6-terra` | `gemini-2.5-pro` |
| `deep-*` | `claude-fable-5` | `gpt-5.6-sol` | `gemini-2.5-pro` |

The `-off` / `-low` / `-medium` / `-high` suffix sets the thinking or reasoning
field, not the model. `google` expresses it as a `thinking_budget` in tokens
(2048 / 8192 / 24576, and absent for `-off`).

### Refreshing a stale shipped mapping

Seeding is write-if-absent, so a vault seeded a year ago would keep a year-old
model forever. Instead, on load, a class whose provider slice is **byte-for-byte
one of the historical bundled slices** is upgraded from the in-tree default —
per provider, independently, and without rewriting the file
([`_upgrade_legacy_provider_defaults`](../../src/intelligence_classes/__init__.py)).
The historical slices it recognises are the `fast`/`standard`/`deep` →
`claude-haiku-4-5` / `claude-sonnet-5` / `claude-opus-5` mapping and its OpenAI
counterpart.

A slice you edited is not one of those, so it is left alone. `customized: true`
in the frontmatter opts the whole file out. A custom `openai` slice never
acquires an inferred `codex` entry.

## Override precedence

This is the part people get wrong. Three separate resolutions run at launch,
each with its own order.

**Intelligence class** ([`_resolve_class_config`](../../src/sessions/spec.py)):

1. the worker's own saved `intelligence_class`
2. the task's `intelligence_class`
3. the profile's `default_class`

**Model** ([`_resolve_model`](../../src/sessions/spec.py)):

1. the worker's own saved `model` — a fixed worker model wins outright
2. otherwise, the model the selected class maps for the provider

There is no third rung: a class that resolves to nothing leaves the launch
model unset, and the CLI's own default applies. That is deliberate — an
unknown class must not silently fall back to a pin.

**Harness** ([`apply_agent_overrides`](../../src/agents/configuration.py)):

1. the worker's own saved `harness`
2. the profile's `harness`

Changing the harness drops an inherited model that belonged to the old CLI,
so a worker moved from `claude` to `codex` does not carry an Anthropic model
name across.

**Profile** for a task
([`resolve_task_profile`](../../src/agents/routing.py)):

1. `task.profile_id`
2. `project.default_profile_id`
3. — nothing. There is no implicit system profile at this rung; the deterministic
   fallback in [`src/profiles/default_selection.py`](../../src/profiles/default_selection.py)
   is applied earlier, by stamping `project.default_profile_id`.

> **Note.** The profile is never evidence of which model ran. The model is
> recorded per attempt in `task_session_attempts`, and a live session reports
> its own frozen launch settings rather than today's edited profile.

## Worked examples

### A pool worker fixed on one class

````markdown
---
id: standard-high-claude
name: "Claude · Standard (High)"
tags: [profile, agent-type, worker, generic]
---

# Claude · Standard (High)

## Role
You are a generic coding worker. …

## Config

```json
{
  "harness": "claude",
  "lifecycle": "pool",
  "needs_workspace": true,
  "default_class": "standard-high",
  "workspaces": ["project-repo"],
  "min_active": 0,
  "max_active": 4
}
```
````

Read as: run the `claude` CLI, therefore provider `anthropic`; think at
`standard-high`, therefore model `claude-opus-5`; pull work by claiming rather
than waiting to be assigned; keep between zero and four of these alive
fleet-wide; each needs a `project-repo` workspace.

Because `default_class` is set, this profile offers routing exactly one class
row. Because `lifecycle` is `pool`, its workers only ever claim tasks whose
class is `standard-high`.

### A class that adds a provider

Adding `vault/intelligence-classes/spark-low.md` with a `google` slice and no
`anthropic` slice makes `spark-low` routable on the `gemini` harness and
refused on `claude` — `task_route` answers *"intelligence class 'spark-low' has
no model mapping for provider 'anthropic'"* rather than launching something
else.

### Pinning and unpinning one task

```bash
aq task edit --task-id demo.4 --intelligence-class deep-high
aq task edit --task-id demo.4 --intelligence-class null --profile-id null
```

The first freezes the class; the next routing run only picks a profile to serve
it. The second clears both fields, which makes the task a `task.route_needed`
candidate again on the next orchestrator cycle. Neither is accepted while a
worker holds the task.

## Operator commands

| Command | Does |
|---|---|
| `aq agent list-profiles` / `get-profile` | Read the parsed profile rows. |
| `aq agent create-profile` / `edit-profile` / `delete-profile` | Manage profiles; deleting a shipped id writes a tombstone. |
| `aq agent import-profile` / `export-profile` | YAML round-trip. |
| `aq agent profile-drift` | Which vault system profiles diverge from `src/profiles/defaults/`, on the semantic fields only (`read_only`, `harness`, `lifecycle`, `needs_workspace`). |
| `aq agent profile-reseed --profile-id <id>` | Overwrite one vault profile with the shipped version, keeping `.bak-<epoch>`; also clears its tombstone. |
| `aq agent profile-audit --legacy-only` | Profiles still deriving capabilities from `allowed_tools`. |
| `aq agent check-profile` / `install-profile` | Validate and install a profile's `## Install` dependencies. |
| `aq agent show-effective-profile` | Run the resolution cascade for a `(project, agent-type)` pair. |
| `aq agent create` / `edit` / `delete` | Manage worker identities and their per-worker `harness` / `model` / `intelligence-class` overrides. Changes apply to the worker's **next** session. |
| `aq agent start-terminal` | Start a task-free interactive terminal for one worker. |
| `aq system list-intelligence-classes` / `edit-intelligence-class` | Read and edit classes. Edits are revision-checked and refuse a stale write. |
| `aq system reload-config` | Force a vault rescan. |
| `aq task route` | The only way to resolve a `routing` gate by hand. |

## Related pages

* [Agents and routing](../concepts/agents-and-routing.md) — the concepts and
  the routing flow.
* [Harness reference](harnesses.md) — what a harness file contains.
* [Glossary](glossary.md) — the vocabulary.
* [Routing module catalog](modules/routing.md) — every module behind this page.

## Tests

```bash
aq test tests/test_profile_parser.py tests/test_profile_session_fields.py \
        tests/test_capability_policy.py tests/test_intelligence_classes.py \
        tests/test_intelligence_class_editing.py tests/test_profile_intelligence.py \
        tests/test_profile_model_pin_migration.py
```
