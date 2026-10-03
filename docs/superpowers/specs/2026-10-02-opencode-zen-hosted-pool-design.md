# OpenCode Zen hosted pool (Space Bunny) — design

Date: 2026-10-02. Task: `noble-delta-40`. Status: implemented (code on the task
branch; vault configuration staged, disabled until the routing artifact is
active).

## Goal

Run a dedicated OpenCode worker pool on OpenCode Zen's free
`opencode/space-bunny-free` model (OpenCode's catalog lists it as "Space Bunny
Free"; CommandCode's `stealth/space-bunny-alpha` is a separate alias and is not
asserted to be the same backend). The constraints:

- Start at `min_active: 0`, `max_active: 1`.
- Keep the existing local Qwen/Ollama OpenCode rungs and their class slices
  exactly as they are.
- Route it only narrow, test-verified coding work through the normal router.
  Integration repairs never reach it.
- Keep hosted availability separate from local Ollama capacity.
- Use no profile model pin, no third-party plugin, no credentials and no paid
  fallback.

## What AQ already had

- **Model resolution** is the intelligence-class slice keyed by the harness's
  `provider` (`SessionSpecBuilder._resolve_class_config`). The local OpenCode
  harness declares `provider: "ollama"`, so every OpenCode rung reads the
  `ollama` slice. Profiles cannot pin a model (`Config 'model' was removed`).
- **Availability** is keyed `harness.base or harness.id` (provider-failover D0).
  A `base:` variant shares its parent's row.
- **Routing.** The shipped routing policy names harnesses, not rungs, and a
  harness a narrow lane names is reachable only through a narrow lane
  (`src/routing/policy.py`). Every other harness is a *general* candidate for
  every class it has a rung at. A new OpenCode rung that the policy does not
  name would therefore take integration repairs and ordinary standard-high work.
- **CLI-specific behaviour keyed on the id.** Two behaviours keyed on the
  literal id `opencode`:
  - the prime addendum: no optional questions, continue after compaction
    (`src/prime/sections.py`);
  - the read of OpenCode's native question store
    (`src/sessions/native_questions.py`).

  A second OpenCode harness id would have lost both, and a question dialog
  would then block the session with nobody told.

## Decisions

1. **A standalone harness `opencode-zen`** (operator vault): the same command
   and launch recipe as `opencode`, but `provider: "opencode"` and **no
   `base:`**.
   - The provider key keeps the hosted model off the `ollama` slice. Putting it
     there would label it local capacity, and editing that slice would move the
     Qwen rungs.
   - Leaving out `base:` gives it its own availability row (`opencode-zen`,
     vendor `opencode`). A Zen rate limit and an Ollama outage then fail
     independently.
   - Rejected: `base: opencode`, because it shares the availability row.
2. **An additive class slice.** `standard-high` gains
   `"opencode": {"model": "opencode/space-bunny-free"}`, and no existing slice
   changes. Only a harness declaring `provider: "opencode"` reads it.
   - Rejected: a dedicated class. Routing reaches only classes on
     `class_order`, and putting a model-named class there would offer it to the
     classifier and to filers' class hints as a capability tier.
3. **A narrow lane of its own** in the shipped policy:

   ```yaml
   narrow-hosted:
     harnesses: [opencode-zen]
     classes: {standard-high: standard-high}
     requires: [narrow, test_verified]
     prefer: true
   ```

   `opencode-zen` is also added to `balance.harness_weights` (1.0) and last on
   `tie_order`. Naming the harness in a narrow lane is what keeps it off general
   candidates, so `origins.integration_repair` / `development_repair`
   (`narrow: false`) never reach it. With equal weights, the local lane is
   declared first and has more slots, so hosted OpenCode takes narrow work when
   local OpenCode is full or unavailable, ahead of Claude and Codex. A lane of
   its own can be tightened alone, for example by adding
   `independent_verifier`.
   - Rejected: adding the harness to the existing `narrow` lane. It would route
     the same way, but the lanes could not be tuned separately and route
     reasons would not say "hosted".
4. **CLI identity follows the executable.** `runs_cli(cli, harness_id,
   registry)` (`src/sessions/harness_registry.py`) is true for the harness named
   after the CLI, and for any registered harness whose `command` has that file
   name. The prime renderer (`PrimeRenderer(harness_registry=…)`, wired from
   `_cmd_prime`) and the question service
   (`AgentQuestionService(harness_registry=…)`, wired by the orchestrator) use
   it, so `opencode-zen` sessions get the addendum and the question store.
   Without a registry, behaviour is unchanged (id match).
5. **The rung** `standard-high-opencode-zen` (operator vault) extends
   `worker-opencode`, overrides `harness: opencode-zen`, and sets
   `default_class: standard-high`, pool `min_active: 0`, `max_active: 1`. It is
   **staged with `enabled: false`**: until the new routing artifact is active, an
   enabled rung on an un-named harness would be a general candidate.
   (`max_active: 0` is rejected by the profile parser.)

## Verified (before delivery)

- **Launch command.** `SessionSpecBuilder.build_pool_spec` over the live vault
  launches `standard-high-opencode-zen` with
  `--model opencode/space-bunny-free` (`llm_provider` `opencode`). The local
  rungs still launch `ollama/qwen3.8:27b` and `ollama/qwen3.5:9b`.
- **Synthetic read/edit/test task.** Run with `opencode run --model
  opencode/space-bunny-free --auto` in a throwaway repo. The model read both
  files, ran the failing unittest, fixed `median` in the module only, re-ran
  the tests to green and checked `git status`.
  - All assistant messages, and OpenCode's title agent, used
    `opencode/space-bunny-free` at cost 0.
  - `opencode auth list` shows 0 credentials.
- **Catalog metadata** (`opencode models opencode --verbose`): endpoint
  `https://opencode.ai/zen/v1`; limits context 1,048,576, input 524,288,
  output 524,288; tool calls supported. No limit is set in AQ or
  `opencode.json`.
- **Daemon state** after staging: the profile row is synced (disabled, 0/1),
  and the availability row `opencode-zen` (vendor `opencode`) sits beside
  `opencode` (vendor `ollama`).

## Activation (operator, after this lands on `main`)

1. Restart the daemon onto the new `main` (`aq restart --no-dashboard`). The
   reviewed `default-assignment-routing` bundle is re-seeded into the vault. A
   healthy older activation is **not** re-pointed automatically (see
   `src/playbooks/required.py`), so activate the new artifact from the main
   checkout and confirm it:

   ```bash
   aq playbook artifacts --playbook-id default-assignment-routing
   aq playbook activate --playbook-id default-assignment-routing \
     --artifact-sha256 "$(cat src/prompts/reviewed_playbooks/default-assignment-routing/artifact.sha256)"
   aq playbook activation-health
   ```
2. Enable the pool:
   `aq pool set-enabled --profile-id standard-high-opencode-zen --enabled`.
3. Watch for the first route that names lane `narrow-hosted`
   (`aq task explain`), and its claim and close by a
   `standard-high-opencode-zen` session.

## Product gaps (documented, not built)

- **No Zen usage or rate-limit evidence.** Nothing probes OpenCode Zen's free
  tier, and a 429 inside the TUI is not classified as availability evidence.
  The `opencode-zen` row reacts only to session-side evidence such as startup
  failures. A throttled preview model shows up as slow or failed tasks, not as
  `exhausted`.
- **No GPU capacity model.** AQ does not model Ollama VRAM either. For both
  OpenCode lanes, the only capacity signal is the pool's `max_active` and the
  availability row.
- **Display surfaces.** `src/profiles/intelligence.py` (semantic-graph AI cards)
  maps only `claude` / `codex` / `gemini` to a provider, so the existing
  OpenCode rungs already show no model there, and the hosted one is no
  different.
- **The harness config is a copy.** `opencode-zen.md` and `opencode.md` must be
  kept in step by hand. A future "variant with its own availability" key would
  remove the copy, but it would change D0.
