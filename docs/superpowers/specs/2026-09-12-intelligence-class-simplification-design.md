# Intelligence-class simplification and one worker per harness

**Date:** 2026-09-12
**Status:** implemented
**Scope:** `src/prompts/default_intelligence_classes/`, `src/profiles/`, one alembic revision

## 1. The problem

The shipped intelligence classes were a full matrix: three tiers
(`fast`, `standard`, `deep`) x four thinking levels (`off`, `low`, `medium`,
`high`) = twelve classes. The shipped worker profiles mirrored it: one profile
per `worker-<tier>-<level>-<provider>`, generated for three providers.

Nothing chose the middle of that matrix deliberately. A routing playbook asked
to pick between `standard-low`, `standard-medium` and `standard-high` is being
asked a question with no evidence behind it, and an operator sizing pools
across nine near-identical worker profiles is maintaining a ladder rather than
a fleet. The two axes also duplicated each other: a task's
`intelligence_class` already selects the model and reasoning level for a run,
so a second *profile* that differs only in its `default_class` bought nothing.

## 2. The shipped set

Seven classes:

| Class | Anthropic | OpenAI / Codex | Google |
|---|---|---|---|
| `fast-low` | `claude-sonnet-5` / low | `gpt-5.6-luna` / low | `gemini-2.5-flash` / 2048 |
| `fast-high` | `claude-sonnet-5` / **xhigh** | `gpt-5.6-luna` / **xhigh** | `gemini-2.5-flash` / 24576 |
| `standard-high` | `claude-opus-5` / **xhigh** | `gpt-5.6-terra` / **xhigh** | `gemini-2.5-pro` / 24576 |
| `deep-low` | `claude-fable-5` / low | `gpt-5.6-sol` / low | — |
| `deep-high` | `claude-fable-5` / **xhigh** | `gpt-5.6-sol` / **xhigh** | — |
| `astra-low` | — | `gpt-6-astra` / low | — |
| `astra-high` | — | `gpt-6-astra` / **xhigh** | — |

Three deliberate properties:

* **A `-high` class buys the provider's *extra*-high effort**, not merely a lot
  of it: `thinking: xhigh` on Anthropic (`CLAUDE_CODE_EFFORT_LEVEL`) and
  `reasoning_effort: "xhigh"` on OpenAI/Codex. Both values were already
  accepted by `src/sessions/spec.py`; nothing in the launch path changed.
* **No `google` slice from the deep tier upward.** No Gemini model belongs in
  that bracket. A missing slice resolves to *no model* — a warning and the
  CLI's own default — rather than a quiet downgrade, so a Gemini-harness
  profile belongs on `fast-*` or `standard-*`.
* **`astra-*` is OpenAI-only.** Astra has no counterpart on another provider,
  so the class carries only the `openai` and `codex` slices and is reachable
  only through the `codex` harness.

`standard` keeps a single level because it is the implementation tier: the
answer to "this needs less thought" is a cheaper *model*, not a cheaper
setting on an expensive one.

## 3. Workers are derived, not authored

A pool session is welded at launch to one class: `pools.py` stamps
`session.intelligence_class` from the profile's `default_class`,
`_pool_claim_routing` refuses to claim anything else, and that becomes a hard
`tasks.intelligence_class = …` predicate in the claim frontier. A running CLI
cannot change model between claims, so pull-mode work needs one worker
identity **per class** — the ladder was the mechanism for that, not decoration.

So the ladder is not deleted; it is *generated*. `src/profiles/catalog.py`
produces the cross-product:

```
(intelligence class) x (harness whose CLI is installed and authenticated)
```

A class yields a rung for a harness only when it has a slice naming a model
for that harness's provider, so the two provider rules from §2 stop being code
and become consequences of the class files: `astra-*` yields no Claude rung,
nothing above `standard` yields a Gemini one. Gemini ships no template, so it
derives nothing at all; adding one entry to `WORKER_PROVIDERS` plus a
`worker-gemini` template is the whole change if that is ever wanted.

| Piece | Id | Holds |
|---|---|---|
| Template | `worker-claude`, `worker-codex` | role, rules, capabilities, harness, workspaces |
| Rung | `<class>-<harness>` | its class, and its pool state |

### Why a stub rather than a regenerated file

A rung is ~10 lines: frontmatter, `extends: worker-<harness>`, and a `## Config`
holding `default_class` plus whatever `aq pool scale` has written. Everything
else resolves through `src/profiles/inheritance.py` on the way into the
database.

The alternative — regenerate the whole file and merge the operator's state
forward — was rejected: a crash between read and write loses pool bounds, and
every template edit becomes a 130-line rewrite times N rungs of churn in a
version-controlled vault. With a stub there is nothing to regenerate when a
template changes, and the only thing that ever rewrites a rung is a change to
its own state.

Inheritance is deliberately **one level**: a template may not extend another.
That makes "where does this value come from" a lookup rather than a search,
and cycles impossible by construction. Config merges key by key; prompt
sections and the capability namespaces are whole-value, because a partial
capability merge is how a copied profile silently loses a command it needs.

A template carries `template: true` and is skipped by `sync_profile_to_db`, so
it never reaches `agent_profiles` and nothing can be routed to it. The
runnable set is exactly the rungs.

### Lifecycle, seeding and removal

* A fresh rung is `lifecycle: task`, so push behaviour is unchanged until an
  operator sizes it into a pool. Bounds are then per rung — and a rung is a
  class, so a pool bound is also a spend ceiling (`deep-high-claude max 2` is
  "at most two Fable sessions"), which the two-profile shape could not express.
* `derive_rungs_from_vault` runs at startup (no login probes) so adding a class
  file yields its rungs without waiting for `aq install`; `aq install` still
  writes the activation record that gates *routing* eligibility.
* An activation record naming none of the current rungs is treated as *no
  evidence* rather than "nothing is eligible" — otherwise an upgrade would
  leave every project with no default profile until the installer next ran.
* `aq agent delete-profile` tombstones a rung and derivation skips a tombstoned
  id, so a deleted rung stays deleted.
* A class that disappears leaves its rungs **disabled, not deleted**
  (`retire_orphaned_worker_rungs`): a rung can own a running pool session, an
  in-flight task and an agent row, and deleting the profile under a live worker
  orphans all three. An authored (non-`extends`) profile naming a vanished
  class is the operator's and gets a warning instead.

## 4. Migrating an existing install

Seeding is write-if-absent, so an install that already ran keeps every retired
file. Two migrations run once:

* **Vault** — `src/profiles/class_retirement.py`, called from
  `Orchestrator.initialize` beside the model-pin migration. It repoints every
  vault profile whose `default_class` names a retired class (the replacement
  stays inside the original tier), then moves each retired class file to
  `<id>.md.retired` — the operator's bytes survive a rename away. A class
  marked `customized: true` is left in place, and the ids already processed
  are recorded in `vault/intelligence-classes/.retired-classes` so a class an
  operator deliberately re-authors is not retired on every start. Profiles are
  repointed *before* the files move, so a crash between the two halves cannot
  leave a profile naming a class that is gone.
* **Database** — alembic `a0000000000f` repoints live pins in `tasks`,
  `agents`, `task_assignment_routes`, `integration_repair_stages` and
  `agent_profiles`. Historical evidence is deliberately untouched:
  `sessions`, `task_session_attempts` and `archived_tasks` record what
  actually ran, and model attribution reads them.

The replacement table (identical in both halves):

| Retired | Becomes |
|---|---|
| `fast-off` | `fast-low` |
| `fast-medium` | `fast-high` |
| `standard-off`, `standard-low`, `standard-medium` | `standard-high` |
| `deep-off` | `deep-low` |
| `deep-medium` | `deep-high` |

The shipped worker ladder is *not* auto-deleted from a vault: profile
directories are operator-owned, and `aq agent delete-profile` (which writes the
`.retired-defaults` tombstone) is the supported way to stand one down. The
migration runbook is `docs/guides/worker-pools.md` §7b.

## 5. Tests

* `tests/test_intelligence_classes.py` pins the shipped set, the per-class
  provider coverage, `gpt-6-astra`, and that every `-high` class is `xhigh`.
* `tests/test_class_retirement.py` covers both halves of the vault migration,
  including idempotence and the customized opt-out.
* `tests/test_profile_catalog.py` pins the cross-product, the two provider
  rules as consequences of the class files, the stub shape, and that a stage
  profile no longer blocks the rung of its own class.
* `tests/test_profile_inheritance.py` covers the merge, the one-level limit
  and the missing-template error.
* `tests/test_codex_fast_classes.py` follows `fast-high` through every launch
  shape now that it means `xhigh`.
