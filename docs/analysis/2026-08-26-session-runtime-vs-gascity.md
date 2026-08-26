---
tags: [analysis, comparison, sessions, tmux, hooks, gascity]
date: 2026-08-26
---

# Session Runtime vs. Gas City — tmux, Hooks, and Scanning

**Purpose.** A focused re-analysis of *how Agent Queue runs agents inside tmux*
against Gas City's equivalent machinery. Supersedes the session-runtime portions of
[[comparison-gascity-beads]] (2026-08-19), which predates `src/sessions/` entirely —
that document still describes our runtimes as `claude_sdk`/`acpx`/`supervisor` and
lists tmux sessions as a *recommendation*.

**Method and its limit.** The Agent Queue side is read fresh from source at
`main@9245da4d`. **The Gas City clone is no longer on disk** — nothing under
`/home/jkern` matches it. The Gas City side is therefore the code-grounded reading
recorded in `comparison-gascity-beads.md` (`gascity@b3f125f4b`, 2026-08-19) plus the
behaviors our own `docs/specs/design/session-runtime.md` cites from their post-mortem.
Where this document says "Gas City does X" it means "that reading records X." Claims
about their *current* HEAD are not available and are not made. Re-clone before acting
on any single GC claim here.

---

## 0. TL;DR

| Dimension | Agent Queue | Gas City |
|---|---|---|
| Provider contract | `SessionProvider` ABC + `Cap` gating; 3 providers (tmux, subprocess, fake) | `Provider` interface, no capability negotiation; 10 providers (tmux, subprocess, exec:, acp, k8s, ssh, hybrid, herdr, t3bridge, fake) |
| Harness definitions | markdown in the vault, hot-reloaded (3 shipped) | Go source, `internal/worker/builtin/profiles.go` (16) |
| Liveness | `/proc` env-marker scan + `is_running`/`process_alive` split | process-table scan for `GC_SESSION_ID`, same split |
| Kill fencing | instance token **and** re-read `/proc` start_ticks per signal | session token |
| Reconciliation | observe-only; tasks launch imperatively | full desired-state build + converge every 30 s tick |
| Named sessions / pools | lazy start on message delivery; drain-idle only | pools sized by `scale_check`, `min/max_active_sessions`, wake, drift recycle |
| Session state machine | frozenset + free assignment (advisory) | explicit reducer, `ErrIllegalTransition` → 409 |
| Completion signal | **explicit only**: `aq task close` + `aq session drain-ack` | `gc.outcome` metadata, agent-set |
| Hooks | 3 points, Claude only, declared in harness markdown | 3 points, provider-level, plus skills + overlays + fragments |
| Structured progress | transcript readers → events, token ledger, heartbeat (Claude only) | `gc session logs` parses transcripts; `gc costs` pricing |
| Deployment status | `sessions.enabled = false`, default provider `subprocess` | tmux is the default and runs at fleet scale |

**Headline:** the observation and safety layer is now *better than* the model we
borrowed it from — we implemented their post-mortem, not their code. The
orchestration layer above it is not built: we have no desired-state reconciliation,
so the whole named-session/pool half of the design is inert. And it is switched off.

---

## 1. What changed since the 2026-08-19 comparison

That document's Part III recommended, for the runtime specifically: adopt-on-restart
(§8.1), heartbeats and leases (§8.2), a stall ladder (§8.3), and per-profile caps with
demand-driven pools (§8.4). Status today:

- **§8.1 adopt-on-restart — done.** `SessionReconciler.adopt_on_start` re-binds live
  sessions and classifies dead ones. Epoch is provenance, not a validity test.
- **§8.2 heartbeats/leases — done.** `lease_ttl_seconds: 480`, fed by transcript
  `in-turn` activity and explicit `aq task heartbeat`.
- **§8.3 stall ladder — done.** `_step_stall_ladder`: nudge → backoff (×3) →
  interrupt+restart with `--resume` → quarantine, counters persisted on the row.
- **§8.4 pools — not done.** See §3.

Also landed and not anticipated by that document: the harness-as-markdown model, the
explicit-completion protocol, transcript readers, and the live pane stream.

---

## 2. Provider contract and liveness

`src/sessions/provider.py` is close to a method-for-method port of Gas City's
`Provider` interface, with the same distinctions and for the same stated reasons.
Three of them are direct post-mortem inheritances, cited in our own source:

- **`is_running` vs `process_alive`** (`provider.py:26-30`) — `remain-on-exit on`
  means a pane outlives its dead agent, so "the artifact exists" and "the agent is
  alive" are different facts. GC has the same split.
- **Unknown is not dead** (`provider.py:32-37`) — `PartialListError` carries whatever
  was enumerated so adoption and reaping defer for that prefix, mirroring GC's
  `ErrRuntimeUnavailable` making destructive arms defer. Our source names this "one of
  the Gas City post-mortem's most expensive bugs."
- **No status files** — `proctable.scan_by_env_marker` over `/proc/<pid>/environ` for
  `AQ_*` markers is the adoption ground truth; never PID files, never session names.

**Where we went further.** Two places:

1. **Capability gating.** `Cap` + `CapabilityUnsupported` means a provider that cannot
   peek raises rather than returning a plausible lie, and callers branch on
   `Cap.PEEK in provider.capabilities`, never on `provider.name`. GC's interface has no
   negotiation — every provider implements every method and degrades silently.
2. **Kill fencing.** `proctable.kill_tree` re-reads the target's `/proc` start_ticks
   immediately before *each* signal and compares `AQ_INSTANCE_TOKEN`. GC fences on the
   session token alone; ours additionally closes the window between deciding to kill
   and signalling, so a PID recycled in that window is never hit.

---

## 3. Reconciliation shape — the structural gap

Gas City's `CityRuntime` tick builds a **desired state** (named sessions, pools sized
by each agent's `scale_check`, bead-assigned work, waits) and calls
`reconcileSessionBeads` to start/stop/drain/wake toward it, bounded by
`max_wakes_per_tick`. Desired state is modeled explicitly:
`lifecycle_projection.go` separates persisted BaseState, DesiredState
(`undesired`/`desired-asleep`/`desired-running`/`desired-blocked`) and
RuntimeProjection (`alive`/`missing`/`fresh-creating`/`stale-creating`).

Ours does not do this. Task sessions launch imperatively from `_execute_task`
(`src/orchestrator/execution.py`), and `SessionReconciler.tick` only *observes*:
`_step_observe` → drain_ack → exits → orphans → stall ladder → named → backstop.
`_step_named` says so in its own docstring:

> v1 scope: *drain idle* sessions to `sleeping`. Starting and recycling named
> sessions needs the message routing that [[design/supervisor-agent]] owns, so this
> deliberately converges in one direction only rather than half-implementing wake
> semantics.

Named sessions are instead started lazily, on demand, by
`SessionLens.ensure_started` (`src/messages/session_lens.py:215`) when a message needs delivering.

**What this costs**, all of it specified in design §4.2 and absent from code:

| Specified | Implemented |
|---|---|
| `wake_mode: resume \| fresh` | no wake path at all — a session that sleeps stays asleep |
| `max_session_age` + jitter recycling via `aq handoff` | absent |
| desired-set convergence for `lifecycle: named` profiles | drain-only |
| config-drift recycle (GC: `ConfigFingerprint()` mismatch) | absent |
| pools, `min/max_active_sessions`, demand-driven scaling | absent |

**Root cause is representational.** There is nowhere to put "desired." We persist one
`state` column and derive `stalled` from the lease TTL. Without GC's
BaseState/DesiredState/RuntimeProjection split, a converging `_step_named` has nothing
to converge *toward* — which is exactly why it was scoped to drain-only. Modeling
desired state is the prerequisite for the rest of §4.2, not a follow-on.

**Session state machine.** `SESSION_STATES` is a frozenset and transitions are free
assignment. GC has an explicit reducer returning `ErrIllegalTransition` → HTTP 409.
This is the same advisory-state-machine weakness the 2026-08-19 document flagged for
`tasks` (§7.5), faithfully reproduced in a brand-new subsystem where enforcing it is
still cheap.

---

## 4. Hooks — feature by feature

Our hook wiring is declared in harness markdown (`supports_hooks` + a `hook_files` map
of destination path → template name), rendered at spec-build time by
`SessionSpecBuilder._hook_files`, written into the work_dir, and activated by the
harness's `settings_flag`. The only shipped template is
`src/prime/templates/hooks/claude.json`.

| Hook point | Agent Queue | Gas City |
|---|---|---|
| **SessionStart** | `aq prime --hook-json`, timeout 30s, **matcher `resume\|compact` only** | `gc prime --hook`, unconditional |
| **Initial priming** | argv bootstrap prompt; the hook is *suppressed* on first start | the hook is the priming path |
| **UserPromptSubmit** | `aq inbox --inject`, timeout 15s | mail injection |
| **PreCompact** | `aq handoff --auto` — writes the note, **no restart** | `gc handoff --auto` — mail-to-self **and restart** |
| **Stop** | deliberately absent | n/a |
| **Output envelope** | `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": …}}` for Claude; plain-text fallback for any other harness | Go-side |
| **Declaration site** | harness markdown in the vault, hot-reloaded | Go profiles + pack `overlay/`, `template-fragments/` |
| **Missing template** | logged and skipped; launch continues | not recorded |
| **Instructions file** | `instructions_file` per harness (`CLAUDE.md`/`AGENTS.md`/`GEMINI.md`) | overlays write `CLAUDE.md`/`AGENTS.md`; skills materialized into `.claude/skills/` |
| **Prompt composition** | rendered server-side by `src/prime/` (5-layer pipeline) | Go `text/template` + `{{define}}` fragments, `inject_fragments`/`append_fragments` |
| **Harness coverage** | **1 of 3** (`claude`); codex and gemini declare `supports_hooks: false` | 16 profiles, though the settings-file mechanism is Claude-shaped |

### Four differences worth arguing about

**1. Double-priming is defended twice.** Our SessionStart matcher is narrowed to
`resume|compact`, *and* `hook_envelopes.suppressed()` blanks the body whenever
`AQ_STARTUP_PROMPT_DELIVERED=1`. GC primes unconditionally from the hook. Two
mechanisms for one invariant is one more than needed for Claude — but the env-var
suppression is the portable half (it works for any harness whose matcher syntax we do
not control), so the redundancy earns its keep as harness coverage grows.

**2. PreCompact does not restart — a deliberate divergence**, recorded at design §200.
GC's handoff mails a summary to self and restarts the session. Ours writes the note and
lets the session continue into its compacted context. Ours is less disruptive and keeps
the pane's history; GC's guarantees the successor starts from a known-clean context. The
tradeoff is real and we have no live data on which is better, because the runtime has
never run a fleet.

**3. We inherited the per-turn tax the 2026-08-19 doc criticized.** That document lists
among Gas City's weaknesses: "many moving parts per agent turn (provider hooks shelling
out to `gc` on every prompt…)". `aq inbox --inject` on `UserPromptSubmit` is exactly the
same shape — a subprocess, a daemon round-trip, and a 15 s timeout budget on every
single prompt the agent submits. This was borrowed without the criticism being answered.
If message delivery can ride the transcript-tail or nudge paths that already exist for
hookless harnesses, the hook becomes an optimization rather than a dependency.

**4. Hook coverage is the real feature gap, not hook design.** Two of our three shipped
harnesses have no hook path at all. For codex and gemini that means: no prime on resume,
no compaction handoff, and no prompt-boundary inbox injection — messages reach them only
by nudge (keystrokes into the pane) or transcript-tail fallback. Both harness files
document the reason honestly (`codex.md`: "Codex has no settings-file hook mechanism like
Claude's `--settings`"), which is the right call versus declaring `supports_hooks: true`
and lying. But GC's 16 profiles face the same constraint with far more coverage built out.

---

## 5. Scanning — pane, stdout, and transcript

### 5.1 There is no stdout to scan under tmux

Worth stating plainly because it shapes everything below: under the tmux provider the
harness owns a **pty**, and `capture-pane` returns the *rendered screen*, not the byte
stream. We never see raw stdout. This is why the live-pane-streaming design rejected
`tmux pipe-pane`: the raw bytes of a full-screen TUI are mostly cursor-addressing
escapes, and rendering them faithfully would require a terminal emulator client-side.
Gas City is in the identical position.

The one place real stdout exists is `SubprocessProvider`, which redirects
stdout+stderr to `<state_dir>/out.log` (`subprocess.py:89-110`) and implements `peek`
as a tail of that file. That provider advertises only `{ACTIVITY, PEEK}` — no nudge, no
attach — so nothing in the stall ladder depends on parsing it.

### 5.2 Channels, in descending order of trust

| Rank | Channel | Agent Queue | Gas City |
|---|---|---|---|
| 1 | **Explicit declaration** | `aq task close` + `aq session drain-ack`; exit-with-open-task is a *failure* | `gc.outcome` / `gc.failure_class` / `gc.work_outcome` metadata, agent-set |
| 2 | **Transcript files** | `transcripts/claude.py` + `TranscriptWatcher` (2 s poll): `notify.task_message` events, token ledger rows, `in-turn` heartbeat | `gc session logs` parses provider transcripts; feeds `gc costs` |
| 3 | **Process table** | `/proc` env-marker scan | same |
| 4 | **Pane text** | explicitly demoted to "a hint"; 4 uses only | readiness, peek, stall detection |

Our rank-1 is stricter than theirs. GC infers outcome from metadata an agent may
simply fail to set; we require two explicit CLI calls and route process exit with an
open task through `exit_classifier.py`. `claude.md` refuses a `Stop` hook for precisely
this reason: "A Stop hook would re-introduce exit-as-success."

### 5.3 The four sanctioned uses of pane text

Design §4 confines pane scraping to readiness, startup dialogs, nudge-submit
confirmation, and the rate-limit hint. The implementation holds that line.

| Purpose | Agent Queue | Gas City |
|---|---|---|
| **Readiness** | phase 1: poll `#{pane_current_command}` until it leaves the shell set; phase 2: poll `ready_prompt_prefix` every 200 ms; budget `ready_delay_ms + 5 s` clamped to [5 s, 60 s]; timeout non-fatal with a live pane, dead pane → `start-stderr.log` + `SessionDiedDuringStartup` | `ready_delay_ms` per provider config |
| **Startup dialogs** | data-driven rules from harness markdown (substring or regex, `once`, `quarantine`); **one shared 8 s budget** across every pass, interleaved before and after the readiness wait | 9 dialogs × 8 s **per-dialog** budgets — which blew the start deadline; our shared budget exists because of that failure (`dialogs.py:8-14`) |
| **Nudge landed** | the marker must *render* in the pane before Enter is pressed, else `NotSubmitted` — a TUI mid-turn swallows typed keys entirely | not recorded |
| **Submit confirmed** | `_submit_pending` anchors to the **last prompt-prefixed line**, because harnesses echo the submitted prompt into the transcript and "marker anywhere on screen" is a false negative; 3 Enter attempts × 4 polls | "busy-indicator poll" |
| **Rate limit** | 8 regex patterns against the final capture, used *only* to choose pause-with-cooldown vs. treat-as-crash — both safe | pane-capture rate-limit text |
| **Activity** | max over `#{window_activity}` (never `#{session_activity}`, which goes stale when detached) + **poke discounting**: output within ±3 s of our own send returns the pre-send value | `GetLastActivity` |
| **Human view** | `capture-pane -p [-e]`, SSE change-only pane stream (one poll loop per *watched* session), Discord `/peek` | dashboard, `gc session peek/attach` |

Two tmux-specific hazards we handle that the GC reading does not record: copy-mode is
cancelled before sending keys (a parked pane swallows them), and a `resize-pane ±1`
SIGWINCH wakes detached TUIs that otherwise drop pastes.

### 5.4 The scanning gap that matters

**Only Claude has a transcript reader.** For codex and gemini, rank-2 does not exist —
there is no structured channel at all, so every observation collapses onto pane text.
Concretely, on those harnesses: no token-ledger rows, no `notify.task_message` streaming,
and the stall ladder's heartbeat rides on pane activity alone, which is the exact signal
the design says not to trust. `codex.md` documents why (sessions are filed under
`~/.codex/sessions/YYYY/MM/DD/` by date, not by work_dir, and no reader exists), and
correctly declines to list `transcript_paths` it cannot read rather than implying
support. Honest, but it means two of three harnesses run half-blind.

---

## 6. Scorecard

**Where we are ahead**

1. Capability gating with a typed `CapabilityUnsupported`, versus silent degradation.
2. Kill fencing that survives PID recycling inside the decide→signal window.
3. Nudge delivery as a typed contract: landed-check, prompt-anchored submit
   confirmation, `NotSubmitted` rather than an optimistic assumption of delivery.
4. Shared dialog budget — their bug, our fix.
5. Poke discounting and the `window_activity` choice, both fixes for observed GC issues.
6. Explicit-only completion; exit-as-success structurally excluded.
7. `TmuxStateCache` collapsing per-tick tmux calls; GC does per-session `display-message`.
8. Harness definitions as hot-reloaded vault markdown rather than Go source.
9. Live pane streaming costed at O(watched sessions), not O(sessions × viewers).

**Where Gas City is ahead**

1. Desired-state reconciliation, and the projection model that makes it expressible (§3).
2. An enforced session state machine.
3. Config-drift detection and recycling.
4. Pools and demand-driven scaling.
5. Harness and provider breadth: 16 profiles and 10 providers versus 3 and 3.
6. Transcript coverage across harnesses, feeding cost accounting.
7. **It runs.** Their tmux path is the default and has been operated at fleet scale —
   the 2026-08-19 reading recorded 169 session dirs and a visible crash-loop pathology,
   which is the kind of finding only production produces. Ours has never run a fleet.

---

## 7. Findings from this pass

### 7.1 The implementation checklist is stale and misleading

`docs/specs/implementation/session-runtime.md:446` leaves `TmuxProvider` unchecked, and
the whole Phase S3 block unchecked. All of it exists: `src/sessions/tmux.py` (729 lines,
with real-tmux integration tests), `src/sessions/transcripts/{base,claude,watcher}.py`,
`hook_files` shipping in `claude.md`, and the SSE stream endpoint covered by
`tests/test_session_stream_api.py`. Someone working from that checklist would rebuild
working code. Fix the checkboxes.

### 7.2 The tmux integration tests leak servers, and it is masking a real failure

`tests/test_tmux_integration.py:83` derives the tmux socket from `tmp_path.name`, which
is **stable across pytest runs** (`test_swallowed_input_raises_no0`), and the fixture has
no `kill-server` teardown. Combined with `remain-on-exit on`, sessions survive the run.
Observed 2026-08-26:

```
/tmp/tmux-1000/ → 47 sockets, 3 live tmux servers
aq-test-test_swallowed_input_raises_no0 → s-tm1: created Tue Aug 25 15:08:14
```

That leftover is why `TestNudge::test_swallowed_input_raises_not_submitted` fails with
`duplicate session: s-tm1`. It has been carried in the "pre-existing failures" baseline
during recent work — incorrectly: it is not unrelated noise, it is our leak, and it will
fail on any machine that has run the suite twice. Fix: `kill-server` in the `provider`
fixture teardown, plus a per-run suffix on the socket name.

---

## 8. Recommendations, in order

1. **Fix the test leak.** Cheap, and it restores a real signal currently written off.
2. **Model desired state before flipping `sessions.enabled`.** Add the
   BaseState/DesiredState/RuntimeProjection split; then `_step_named` can converge in
   both directions and wake/recycle/pool work becomes writable. Turning the runtime on
   with drain-only convergence means sessions that sleep and never wake.
3. **Enforce the session state machine** while the subsystem is young — this is the one
   Gas City lesson we borrowed the diagnosis of and not the cure.
4. **Write the codex transcript reader.** It unblocks resume-by-uuid for codex (see
   `codex.md`) *and* gives two of three harnesses a structured channel. Highest
   feature-per-line item on this list.
5. **Re-examine `aq inbox --inject` on every prompt.** We adopted the per-turn tax this
   analysis's predecessor criticized in Gas City. If the existing nudge and
   transcript-tail delivery paths suffice, demote the hook to an optimization.
6. **Update the implementation checklist.**

---

## Appendix — files read (Agent Queue, `main@9245da4d`)

`src/sessions/`: `provider.py`, `tmux.py`, `reconciler.py`, `proctable.py`,
`dialogs.py`, `exit_classifier.py`, `subprocess.py`, `spec.py`, `harness_parser.py`,
`state_cache.py`, `pane_broadcaster.py`, `transcripts/{base,claude,watcher}.py`,
`default_harnesses/{claude,codex,gemini}.md`.
`src/prime/`: `hook_envelopes.py`, `templates/hooks/claude.json`.
`src/messages/session_lens.py`, `src/config.py` (`SessionsConfig`),
`src/cli/agent_surface.py`, `src/orchestrator/{core,execution}.py`.
Specs: `docs/specs/design/session-runtime.md`,
`docs/specs/implementation/session-runtime.md`.
Tests: `tests/test_tmux_integration.py` (executed).
