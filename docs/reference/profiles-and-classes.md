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

New non-pool agents created for task demand use the profile's display `name`.
When an enabled agent of the same profile already has that name, the new
agent gets the lowest free numeric suffix starting at `2`, such as
`Review Spec Ingest 2`. Existing agents keep their saved names and are reused
when they can serve the task. Installer refresh and profile reseeding do not
create agent rows.

## Agent profile format

A profile is one markdown file: YAML frontmatter, then sections. Sections whose
name is in the structured set are parsed from a fenced ```json``` block;
sections in the prompt set are captured as text. `## Role` and `## Rules`
reach `aq prime`; `## Reflection` is reserved for post-task reflection, not
the launch prompt. Anything else is ignored. Parsing is deterministic —
invalid JSON is a parse error, never a silent fallback
([`src/profiles/parser.py`](../../src/profiles/parser.py)).

| Section | Kind | Contents |
|---|---|---|
| frontmatter | YAML | `id`, `name`, `tags`, `extends`, `template`, `capability_sync`; unknown keys are preserved |
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
| `harness` | string | none | **The only** CLI selector: a registered harness id, such as `claude`, `codex`, `gemini` or `opencode`. The harness supplies the provider. |
| `default_class` | string | `""` | Intelligence class used when the task does not name one. Together with a harness, makes an enabled worker profile offerable for that class; stage, named, template and read-only profiles are excluded. |
| `lifecycle` | `task` \| `pool` \| `named` | `task` | Push one session per assigned task; pull work by claiming; or one long-lived named session. |
| `mode` | `always` \| `on_demand` | none | `named` only: keep resident, or wake on demand. |
| `wake_mode` | `resume` \| `fresh` | none | `named` only: resume the prior context or start clean. |
| `idle_timeout` | int (s) | none | `named`/`on_demand` only: sleep after this idle. |
| `max_session_age` | int (s) | none | `named` only, positive seconds: recycle a session older than this. |
| `needs_workspace` | bool | `true` | Whether the orchestrator acquires a workspace before launching. |
| `workspaces` | list | none | Parsed and validated, but currently not persisted by profile sync. Routing uses the task's workspace requirements; this is not a working attachment selector. |
| `read_only` | bool | `false` | A declaration of write *intent*. It no longer changes workspace acquisition; it gates the require-a-PR close check. |
| `enabled` | bool | `true` | Operator kill switch. `false` = hand this profile no new work. |
| `permission_mode` | enum | `""` | One of `default`, `plan`, `full`, `bypassPermissions`, `acceptEdits`, `auto`. |
| `codex_service_tier` | `default` \| `fast` | none | Codex-only per-launch override of the class slice's `service_tier`. Absent inherits; neither set means no tier flag. |
| `codex_full_auto` | bool | `false` | Codex-only autonomous permission opt-in. |
| `claude_dangerously_skip_permissions` | bool | `false` | Claude-only autonomous permission opt-in. |
| `max_tokens_per_task` | int | none | Per-task token ceiling. |
| `allow_base_checkout` | bool | `false` | Escape hatch letting a session run in a base checkout. Nothing shipped sets it. |
| `min_active` | int | none | `pool` only: fleet-wide warm floor. |
| `max_active` | int | none | `pool` only: fleet-wide ceiling. `null` = unlimited. |
| `min_per_project` | int | `0` | `pool` only: keep this many workers resident per eligible project. |
| `max_claims_per_session` | int | none | `pool` only: claims before a session retires. `null` = unlimited. |

Three keys were **removed** and are rejected at parse time:

* `runtime` — every agent, including the supervisor, is a harness session.
  All values are rejected; select the CLI with `harness`.
* `agent_name` — went with the retired `acpx` runtime.
* `model` — a per-profile model pin. Removed in favour of intelligence classes;
  [`src/profiles/model_pin_migration.py`](../../src/profiles/model_pin_migration.py)
  strips any left in a vault at startup, logging a mismatch so an operator can
  audit it.

### Inheritance and templates

`extends: worker-claude` loads one parent before sync. Inheritance is one level:
a parent cannot itself extend another profile, and a missing or invalid parent
leaves the last valid database row in place. Config merges key by key with the
child winning. Nonempty child prompt, Tools, MCP and Install values replace
the parent's whole value. An authored Capabilities block replaces all three
namespaces together, including an explicitly empty policy; it is not appended.
A `template: true` profile supplies inheritance but has no runnable database row.

`capability_sync` controls additive shipped-grant updates (see
[local versus shipped policy](../concepts/agents-and-routing.md#local-versus-shipped-policy));
it does not change the inheritance merge rule.

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
| `codex` | JSON | Optional. Overrides `openai` **only** for the Codex CLI. Accepts `model`, `reasoning_effort`, and optional `service_tier` (`default` or `fast`). |
| `google` | JSON | `{"model": …, "thinking_budget": <int>}` |

Resolution is `(class_id, provider) → slice`. A missing provider key yields
`{}`, which means "no class-driven override" and leaves the launch model unset
rather than guessing.

### Provider resolution

The harness definition's explicit `provider` wins. Legacy definitions fall back
to the harness id or command (`claude` → `anthropic`, `codex` → `openai`,
`gemini` → `google`). When neither identifies a provider, launch looks for a
class slice keyed by the harness id. This supports operator-defined harnesses.
See [`_resolve_class_config`](../../src/sessions/spec.py).

The `codex` slice is consulted only when the harness id is `codex` and the
command basename is `codex` or `codex.exe`. Other OpenAI-backed harnesses use
`openai`. A mapping without a model supplies no launch override; routing
refuses a candidate that cannot map its required class to a model.

### Shipped classes

Seven classes ship. Their mappings live in
[`src/prompts/default_intelligence_classes/`](../../src/prompts/default_intelligence_classes/):

| Classes | `anthropic` | `openai` / `codex` | `google` |
|---|---|---|---|
| `fast-low`, `fast-high` | `claude-sonnet-5` | `gpt-5.6-luna` | `gemini-2.5-flash` |
| `standard-high` | `claude-opus-5` | `gpt-5.6-terra` | `gemini-2.5-flash` |
| `deep-low`, `deep-high` | `claude-fable-5` | `gpt-5.6-sol` | absent |
| `astra-low`, `astra-high` | absent | `gpt-6-astra` | absent |

The low/high suffix changes effort, not the model: `low` / `xhigh` for
Anthropic and OpenAI/Codex; Google's thinking budgets are 2048 / 24576 tokens.
Class existence and routing eligibility are separate: the bound policy must
include a class in `class_order`. The current default policy still omits
Astra; see [known gaps](../specs/systems/routing-and-profiles.md#5-known-gaps).

The older off/medium classes and `standard-low` were retired. Startup migrates
uncustomized class files to `.md.retired`, repoints generic profile defaults,
and disables rungs whose ids name a retired class. The database migration
handles old task class pins; workers must not run operator migrations.

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

1. `task.profile_id`, which only the project's router (`task_route_apply`), a
   failover, spill or undo move among the router's candidates, an audited
   override (`aq task route-override`) or a role creator writes
2. — nothing. There is no project default profile and no implicit system
   profile: an `unrouted` task resolves to no profile, is not claimable, and
   waits for its router ([agents and routing](../concepts/agents-and-routing.md)).

> **Note.** The profile is never evidence of which model ran. The model is
> recorded per attempt in `task_session_attempts`, and a live session reports
> its own frozen launch settings rather than today's edited profile.

## Worked examples

### A pool worker fixed on one class

Derived rungs inherit the shared worker definition rather than copying it:

````markdown
---
id: standard-high-claude
name: "Claude · Standard (High)"
extends: worker-claude
---

## Config

```json
{"default_class": "standard-high", "lifecycle": "pool", "min_active": 0, "max_active": 4}
```
````

This inherits the Claude harness, role, rules and capabilities from
`worker-claude`, then fixes the class and pool bounds. Its sessions pull only
compatible `standard-high` tasks. Bounds apply fleet-wide. Configure pool
state through `aq pool set-lifecycle` and `aq pool scale`; see
[worker pools §7b](../guides/worker-pools.md#7b-workers-are-derived-not-authored).

### A class that adds a provider

Adding `vault/intelligence-classes/spark-low.md` with a `google` slice and no
`anthropic` slice makes `spark-low` runnable on the `gemini` harness and not on
`claude`. The router never offers a `claude` candidate for it, and an override
onto one is refused — `aq task route-override` answers *"profile
'standard-high-claude' cannot run class 'spark-low': its provider 'anthropic'
maps no model for 'spark-low'"* rather than launching something else. The
router considers the class at all only once its policy's `class_order` names
it.

### Re-routing and overriding one task

```bash
aq task route --task-id demo.4 --intelligence-class deep-high --reason "needs deeper reasoning"
aq task route-override --task-id demo.4 --profile-id deep-high-codex \
  --reason "claude is exhausted and this blocks the release"
aq task route --task-id demo.4
```

The first stores a new class hint, clears the task's profile, class and route
record, sets it `unrouted`, and immediately emits `task.route_needed`.
The router clamps the hint to the kind's `max_class` and applies risk floors;
a first bugfix attempt always starts at `standard-high`, regardless of the hint.
`aq task edit --intelligence-class` on a queued, unclaimed task does the same. The second is
the emergency override (local operator or live supervisor only): it pins the
task to that profile, and `aq doctor --check routing.bypassed` lists it until
the third sends the task back to the router. None is accepted while a worker
holds the task, and no filing command takes a profile.

## Operator commands

| Command | Does |
|---|---|
| `aq agent list-profiles` / `get-profile` | Read the parsed profile rows. |
| `aq agent create-profile` / `edit-profile` / `delete-profile` | Manage profiles; deleting a shipped id writes a tombstone. |
| `aq agent import-profile` / `export-profile` | Portable YAML settings (or gist). Export omits lifecycle, inheritance, capabilities and pool state; back up the vault markdown for a complete profile. |
| `aq agent profile-drift` | Which vault system profiles diverge from `src/profiles/defaults/`, on the semantic fields (`read_only`, `harness`, `lifecycle`, `needs_workspace`), missing/extra sections, and `missing_grants` (per-namespace `## Capabilities` grant names the shipped default has that the vault copy lacks). |
| `aq agent profile-reseed --profile-id <id> --grants-only` | Additive repair: merge only the missing `## Capabilities` grants into the vault copy, keeping `.bak-<epoch>`; every other edit (e.g. `harness`) survives untouched. Refuses if the vault copy has no `## Capabilities` section. |
| `aq agent profile-reseed --profile-id <id>` | Restore one vault profile from the shipped version while preserving a nonempty display name in valid vault frontmatter, keeping `.bak-<epoch>`; also clears its tombstone. |
| `aq agent profile-audit --legacy-only` | Profiles still deriving capabilities from `allowed_tools`. |
| `aq agent check-profile` / `install-profile` | Validate and install a profile's `## Install` dependencies. |
| `aq agent show-effective-profile` | Resolve the explicitly named `--profile-id` in `--project-id` context. There is no project-default cascade. |
| `aq agent create` / `edit` / `delete` | Manage worker identities and their per-worker `harness` / `model` / `intelligence-class` overrides. Changes apply to the worker's **next** session. |
| `aq agent start-terminal` | Start a task-free interactive terminal for one worker. |
| `aq system list-intelligence-classes` / `edit-intelligence-class` / `delete-intelligence-class` | List mappings, loaded status, parse errors and raw-file revisions; edit or retire a class with `--expected-revision`. Editing/deleting requires a local operator or global admin session. Stale revisions return `revision_conflict`. Deletion refuses references from agents, profiles and non-terminal tasks; repoint them first. |
| `aq system reload-config` | Force a vault rescan. |
| `aq task route` / `route-override` | Send a queued task back to its router, optionally with a new class hint or kind; or, in an emergency, override its route (local operator or supervisor only, with a reason). An open `routing` gate resolves when the router, or an override, writes the route. |

Class edits update the live registry; running sessions keep their launch settings.
A save sets `customized: true`, preventing bundled-default refresh from replacing
the edited mappings. Class deletion retires the file and prevents reseeding.

Profile `## MCP Servers` lists are parsed and synced, but currently reach only
a launch log; they do not configure the harness session. See
[known gaps](../specs/systems/routing-and-profiles.md#5-known-gaps).

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
