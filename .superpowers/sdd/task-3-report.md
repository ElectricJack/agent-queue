# Task 3 Report — cascade + inject hook + prime inbox

**Status:** DONE
**Commit:** 9f82cd90 (`feat(messages): cascade delivery pass, UserPromptSubmit inject hook, prime inbox`)
**Branch:** wave4/supervisor

## Decisions

1. **Cascade throttle exception handling.** The single try/except wraps
   both `run_delivery_pass()` and `check_reply_timeouts()`. Rationale:
   spec §5 treats a delivery *pass* as one logical unit; one wedged
   pass should not partially advance the reply-timeout sweep either.
   The test `test_engine_exception_does_not_propagate` pins that both
   counters stay in lockstep.
2. **`SessionLens` profile loader.** Added
   `Orchestrator._load_profile_for_lens(profile_id)` — the lens has no
   task in hand, so `_resolve_profile(task)` is the wrong shape. The new
   helper does the same project-override → system-profile lookup by id.
3. **Prime as a delivery method.** `PrimeRenderer.render_for_task` gained
   `mark_messages_delivered: bool = False`; `_cmd_prime` passes `True`
   iff `config.messages.enabled`. The mark is per-row CAS with
   `via="prime"` (matches spec §8), so a racing nudge or inject can never
   double-deliver. `build_messages_section` skips the messages
   sub-section entirely when the flag is off but still renders the
   handoff block — matches the pre-existing test that inspects the
   messages section for handoff content.
4. **`aq inbox` alias hook-safety.** The existing hook file
   (`src/prime/templates/hooks/claude.json`) already referenced
   `aq inbox --inject`, but no top-level `aq inbox` command existed —
   only `aq message inbox`, which uses `_handle_errors` and would exit 3
   on a downed daemon (blocking the agent's next prompt). Added a
   dedicated top-level `aq inbox` that swallows `DaemonNotRunningError`,
   `CommandError`, and generic exceptions to exit 0 with no stdout. Also
   auto-derives `task:<AQ_TASK_ID>` from the session env when neither
   `--to` nor `--to-kind/--to-id` are given, so the hook works without
   the harness having to build a recipient string.
5. **No harness JSON edit needed.** The `UserPromptSubmit` block was
   already present in `hooks/claude.json` from earlier work; only the
   Notes section of `claude.md` needed to document the no-op contract.

## Files changed

- `src/orchestrator/core.py` — SessionLens + MessageDeliveryEngine
  construction; `_load_profile_for_lens`; `_deliver_messages` body.
- `src/prime/sections.py` — rewrote `build_messages_section` to use the
  real messages query layer, added `config` + `mark_delivered` params.
- `src/prime/renderer.py` — plumbed `mark_messages_delivered` through.
- `src/commands/surface_commands.py` — `_cmd_prime` passes the flag.
- `src/cli/messages.py` — top-level `aq inbox` (hook-safe alias).
- `src/sessions/default_harnesses/claude.md` — Notes explaining the
  `UserPromptSubmit` hook and its no-op safety contract.
- `tests/test_message_delivery.py` — `TestCascadeWiring` (5 tests).
- `tests/test_surface_commands.py` — 2 new prime tests.

## Test command + output

```
$ python3 -m pytest tests/test_message_delivery.py tests/test_surface_commands.py tests/test_message_commands.py tests/test_session_spec.py tests/test_prime_renderer.py tests/test_prime_hook_envelopes.py tests/test_orchestrator.py tests/test_command_surface.py tests/test_cli_agent_surface.py -q
# 244 passed, 0 failed
```

Full suite (`pytest tests/ -n auto -q --ignore=tests/test_orchestrator.py`)
has 412 pre-existing failures (chat_eval, profile_integration,
playbook_paused_notification, test_cli auto-help,
test_emit_schema_compliance). Verified they reproduce on the pre-commit
tree via `git stash`; not touched by this change.

Ruff clean on all edited files.

## Concerns

- The `aq inbox` alias silently swallows every exception. That is the
  spec's hook-safety requirement, but it means a real bug in the CLI
  path becomes invisible in agent transcripts. Users invoking
  `aq inbox` interactively have `aq message inbox` for the noisy form;
  worth documenting in operator notes when the surface is finalized.
- The task-scope MCP allowlist is not part of this task
  (spec §11 Phase 1 note explicitly defers it), but once it lands,
  `message_inbox` should be on it so agents can trigger the same path
  without shelling out.

---

## Fix — surface session- and profile-addressed pending messages in prime

**Gap:** prime only queried `get_pending_messages("task", <task_id>)`. Spec
recipient kinds are `{session, task, profile, user}` — messages addressed to
the priming session's `s-…` name (`to_kind="session"`) or the task's profile
(`to_kind="profile"`) were silently invisible in prime.

### Changes

- `src/prime/sections.py::build_messages_section` — accepts new
  `profile_id` and `session_name` kwargs. Fetches up to three inboxes
  (`("task", task_id)`, `("profile", profile_id)` when set,
  `("session", session_name)` when resolvable), merges by message id
  (dedupe), sorts by `(priority asc, created_at asc)`, renders using the
  same envelope, and marks each row delivered via CAS with `via="prime"`
  when `mark_delivered` is true. Still gated on `config.messages.enabled`.
- `src/prime/renderer.py::render_for_task` — resolves the priming
  session's name via `db.get_session_for_task(task_id)` (best-effort;
  silently skips when the backend lacks the helper or no session row
  exists yet) and forwards it plus `task.profile_id` into
  `build_messages_section`. Profile inbox is fetched unconditionally
  when the task has a profile; session inbox is skipped silently when
  unresolvable, per spec.
- `tests/test_surface_commands.py` — three new tests plus one helper:
  - `test_profile_addressed_message_rendered_and_marked_delivered`
  - `test_session_addressed_message_rendered_when_session_resolvable`
  - `test_task_profile_session_merged_by_priority_no_dupes`
  - `_ensure_agent_profile` helper (minimal `agent_profiles` row for
    the `tasks.profile_id` FK).

### Test evidence

```
$ python3 -m pytest tests/test_surface_commands.py::TestPrime -q
10 passed
$ python3 -m pytest tests/test_message_delivery.py tests/test_message_commands.py tests/test_surface_commands.py tests/test_prime_renderer.py -q
129 passed
```

Ruff clean on `src/prime`, `src/commands/surface_commands.py`, and
`tests/test_surface_commands.py`.

### Concerns

- `get_session_for_task` returns the highest-ranked live-then-sleeping
  row, matching the `SessionQueryMixin` ranking used elsewhere. If a
  task's session was renamed mid-flight (an unusual scenario — session
  names are provider-tokens, not agent identity), the fetched name is
  whatever the current row says; older messages addressed to a prior
  name would still be pending under that older `to_id` and would not
  surface here. That is the same behaviour as any other session-name
  addressed inbox lookup and is out of scope for this fix.
- The per-inbox `limit=50` is applied per-recipient before merging;
  50+50+50 is 150 candidates max before dedupe/sort. If a real user hits
  the tail of that limit on the merged list, `get_pending_messages`
  would want a merged-limit path. Not a regression — the pre-fix single
  inbox had the same 50 cap.
