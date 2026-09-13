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

## 3. One worker per harness

`worker-{fast,standard,deep}-{off,low,medium,high}-{claude,codex,gemini}` is
replaced by two shipped profiles:

| Profile | Harness | Fallback class |
|---|---|---|
| `worker-claude` | `claude` | `standard-high` |
| `worker-codex` | `codex` | `astra-high` |

`default_class` is only the fallback for a task that names no class of its
own. There is no shipped Gemini worker: its only usable classes would be the
two cheap ones, which is not a default anyone wants; a `gemini`-harness
profile remains authorable by hand.

`src/profiles/catalog.py` still gates each on its provider's login probe, so a
box with no Codex CLI never gets `worker-codex` seeded or routed to.
`src/profiles/default_selection.py` prefers `worker-claude`, then
`worker-codex`, and keeps the retired ladder's ids as trailing entries so a
vault seeded before this change still resolves to a worker.

### Consequence for pools

Pool bounds are per profile, so they now cap *harness* concurrency, not
*capability* concurrency. A fleet that wants a standing limit on expensive
runs authors a profile of its own pinned to a class — `docs/guides/worker-pools.md`
§7c is that recipe.

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
* `tests/test_profile_catalog.py` pins one worker per harness and its class.
* `tests/test_codex_fast_classes.py` follows `fast-high` through every launch
  shape now that it means `xhigh`.
